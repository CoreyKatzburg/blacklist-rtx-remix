#include "proxy_diagnostics.h"

#include "lightpub.h"
#include "proxy_log.h"

#include <psapi.h>
#include <tlhelp32.h>

#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <map>
#include <tuple>
#include <unordered_map>
#include <utility>
#include <vector>

namespace proxy {
namespace {

bool g_frameTrace = false;
UINT g_frameTraceMax = 36000;
bool g_callTiming = false;
UINT g_frameTiming = 0;
bool g_hitchSampler = false;
UINT g_sampleMs = 5;
double g_hitchMs = 50.0;
bool g_texStats = false;

wchar_t g_frameTracePath[MAX_PATH] = {};
wchar_t g_callTracePath[MAX_PATH] = {};
wchar_t g_gateTracePath[MAX_PATH] = {};
LONGLONG g_qpcFrequency = 0;
SRWLOCK g_traceLock = SRWLOCK_INIT;

const char* const kCallKindNames[kCallKindCount] = {
    "createTex", "createVB", "createIB", "texLock", "texUnlock", "bufLock", "bufUnlock",
    "queryGetData", "draw", "present", "clear", "updateSurface", "updateTexture",
    "getRTData", "stretchRect"};
volatile LONG g_callCounts[kCallKindCount] = {};
volatile LONG64 g_callTicks[kCallKindCount] = {};

// --- HitchSampler state ---
struct SampledThread {
    DWORD id;
    HANDLE handle;
    char role;           // 'R' render (calls Present), 'D' calls D3D, 'o' other
    volatile LONG call;  // CallKind it is inside, or -1
    ULONGLONG lastCpu;   // 100 ns units, kernel + user, at the previous sample
};

struct ThreadSample {
    LONGLONG qpc;
    SHORT slot;
    SHORT call;       // CallKind or -1
    DWORD cpuUs;      // CPU time the thread used since its previous sample
    DWORD eip;
    DWORD frames[8];  // likely return addresses found on the stack, innermost first
};

struct ModuleRange {
    uintptr_t base;
    uintptr_t end;
};

struct HitchWindow {
    LONGLONG start;
    LONGLONG end;
    UINT frame;
};

constexpr LONG kMaxSampledThreads = 128;
constexpr size_t kSampleRing = 1 << 17;

SampledThread g_sampledThreads[kMaxSampledThreads] = {};
volatile LONG g_sampledThreadCount = 0;
SRWLOCK g_sampledThreadLock = SRWLOCK_INIT;
DWORD g_samplerThreadId = 0;
thread_local LONG t_sampledSlot = -1;

ThreadSample* g_sampleRing = nullptr;
size_t g_sampleCount = 0;
ModuleRange g_modules[512] = {};
size_t g_moduleCount = 0;
HitchWindow g_hitches[64] = {};
volatile LONG g_hitchHead = 0;
LONG g_hitchTail = 0;
HANDLE g_hitchEvent = nullptr;
UINT g_hitchesWritten = 0;
DWORD g_stackCopy[4096] = {};

// --- Main-loop timer state ---
constexpr uintptr_t kLoopTimerCall = 0x006FE4B7;
constexpr BYTE kLoopTimerCallBytes[6] = {0xFF, 0x15, 0x7C, 0x92, 0x93, 0x02};  // CALL [QPC]
constexpr size_t kLoopTraceBufferSize = 64 * 1024;
LONGLONG g_loopFrequency = 0;
LONGLONG g_loopLastReal = 0;
LONGLONG g_loopLastOut = 0;
double g_loopAverage = 0.0;
UINT g_loopFrames = 0;
wchar_t g_loopTracePath[MAX_PATH] = {};
char* g_loopTraceBuffer = nullptr;
size_t g_loopTraceLength = 0;
LONGLONG g_loopLastFlush = 0;

// --- TexStats state ---
struct TexCreateStat {
    unsigned long long count = 0, released = 0, lifetimeFrames = 0, locks = 0;
    double lockMs = 0.0, createMs = 0.0;
    void* stack[6] = {};
};
using TexCreateKey = std::tuple<void*, UINT, UINT, UINT, DWORD, DWORD, DWORD>;
struct TexLockStat {
    unsigned long long count = 0;
    double ms = 0.0;
};
using TexLockKey = std::tuple<void*, UINT, DWORD, int, int>;
struct LiveTexture {
    LONG createFrame;
    TexCreateStat* stat;
};

SRWLOCK g_texStatsLock = SRWLOCK_INIT;
std::map<TexCreateKey, TexCreateStat>* g_texCreates = nullptr;
std::map<TexLockKey, TexLockStat>* g_texLocks = nullptr;
std::unordered_map<void*, LiveTexture>* g_liveTextures = nullptr;
unsigned long long g_lifetimeHistogram[7] = {};
UINT g_lastAvailableTextureMem = 0xFFFFFFFF;
LONGLONG g_lastTexStatsDump = 0;

// --- SkinTrace state ---
struct SkinFrame {
    uint32_t gbuffer;
    uint32_t forward;
    uint32_t forwardOther;
    uint32_t blended;
    uint32_t fade;
    uint32_t dissolve;
    uint32_t triangles;
    uint32_t shaders;
};
bool g_skinTrace = false;
SkinFrame g_skinFrame = {};
SkinFrame g_skinLast = {};

// --- CaptureDraws state ---
bool g_captureDraws = false;
bool g_captureDrawsPrevious = false;
LONG g_captureDrawSequence = 0;
std::vector<char> g_dumpBuffer;
LONG g_dumpFrame = 0;

const char* CameraSourceName(const UploadInfo* upload) {
    if (upload && upload->pureCameraUpload) {
        return upload->callerRva == kGameplayCameraUploadReturnRva
            ? "gameplay-camera-vp"
            : "pure-camera-vp";
    }
    return "world-wvp-diagnostic";
}

LONG RegisterSampledThread(DWORD id, char role) {
    if (id == g_samplerThreadId) return -1;
    AcquireSRWLockExclusive(&g_sampledThreadLock);
    LONG slot = -1;
    for (LONG i = 0; i < g_sampledThreadCount; ++i) {
        if (g_sampledThreads[i].id == id) { slot = i; break; }
    }
    if (slot < 0 && g_sampledThreadCount < kMaxSampledThreads) {
        HANDLE handle = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT |
                                   THREAD_QUERY_INFORMATION, FALSE, id);
        if (handle) {
            slot = g_sampledThreadCount;
            g_sampledThreads[slot].id = id;
            g_sampledThreads[slot].handle = handle;
            g_sampledThreads[slot].role = role;
            g_sampledThreads[slot].call = -1;
            g_sampledThreads[slot].lastCpu = 0;
            InterlockedIncrement(&g_sampledThreadCount);
        }
    }
    if (slot >= 0 && (role == 'R' || (role == 'D' && g_sampledThreads[slot].role == 'o'))) {
        g_sampledThreads[slot].role = role;
    }
    ReleaseSRWLockExclusive(&g_sampledThreadLock);
    return slot;
}

BOOL WINAPI LoopQueryPerformanceCounter(LARGE_INTEGER* counter) {
    LARGE_INTEGER now;
    const BOOL ok = QueryPerformanceCounter(&now);
    if (!ok || !counter) {
        if (counter) *counter = now;
        return ok;
    }
    LONGLONG out = now.QuadPart;
    if (g_loopLastReal != 0 && g_frameTiming >= 2) {
        const LONGLONG real = now.QuadPart - g_loopLastReal;
        if (real > 0 && real < g_loopFrequency / 10) {
            g_loopAverage = g_loopAverage > 0.0 ? g_loopAverage + 0.1 * (static_cast<double>(real) - g_loopAverage)
                                                : static_cast<double>(real);
            const double behind = static_cast<double>(now.QuadPart - g_loopLastOut) - g_loopAverage;
            double step = g_loopAverage + 0.1 * behind;
            step = (std::max)(step, 0.25 * g_loopAverage);
            out = g_loopLastOut + static_cast<LONGLONG>(step);
            const LONGLONG limit = static_cast<LONGLONG>(2.0 * g_loopAverage);
            out = (std::min)((std::max)(out, now.QuadPart - limit), now.QuadPart + limit);
            out = (std::max)(out, g_loopLastOut + 1);
        }
    }
    TraceLoopFrame(now.QuadPart, out);
    g_loopLastReal = now.QuadPart;
    g_loopLastOut = out;
    counter->QuadPart = out;
    return ok;
}

void RefreshModules() {
    HMODULE modules[512];
    DWORD needed = 0;
    if (!K32EnumProcessModules(GetCurrentProcess(), modules, sizeof(modules), &needed)) return;
    const size_t count = (std::min)(static_cast<size_t>(needed / sizeof(HMODULE)),
                                    ARRAYSIZE(modules));
    size_t written = 0;
    for (size_t i = 0; i < count; ++i) {
        MODULEINFO info;
        if (K32GetModuleInformation(GetCurrentProcess(), modules[i], &info, sizeof(info))) {
            g_modules[written].base = reinterpret_cast<uintptr_t>(info.lpBaseOfDll);
            g_modules[written].end = g_modules[written].base + info.SizeOfImage;
            ++written;
        }
    }
    g_moduleCount = written;
}

bool LooksLikeReturnAddress(DWORD value) {
    const uintptr_t address = value;
    bool inModule = false;
    for (size_t i = 0; i < g_moduleCount; ++i) {
        if (address >= g_modules[i].base + 0x1000 && address < g_modules[i].end) {
            inModule = true;
            break;
        }
    }
    if (!inModule) return false;
    __try {
        const BYTE* code = reinterpret_cast<const BYTE*>(address);
        if (code[-5] == 0xE8) return true;
        if (code[-2] == 0xFF && (code[-1] & 0x38) == 0x10) return true;
        if (code[-3] == 0xFF && (code[-2] & 0x38) == 0x10) return true;
        if (code[-6] == 0xFF && (code[-5] & 0x38) == 0x10) return true;
        if (code[-7] == 0xFF && (code[-6] & 0x38) == 0x10) return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
    }
    return false;
}

void SampleThread(LONG slot, LONGLONG now) {
    SampledThread& thread = g_sampledThreads[slot];
    FILETIME created, exited, kernel, user;
    ULONGLONG cpu = 0;
    if (GetThreadTimes(thread.handle, &created, &exited, &kernel, &user)) {
        cpu = (static_cast<ULONGLONG>(kernel.dwHighDateTime) << 32 | kernel.dwLowDateTime) +
              (static_cast<ULONGLONG>(user.dwHighDateTime) << 32 | user.dwLowDateTime);
    }
    if (SuspendThread(thread.handle) == static_cast<DWORD>(-1)) return;
    CONTEXT context = {};
    context.ContextFlags = CONTEXT_CONTROL | CONTEXT_INTEGER;
    size_t copied = 0;
#if defined(_M_IX86)
    if (GetThreadContext(thread.handle, &context)) {
        const uintptr_t stackTop = context.Esp;
        size_t bytes = sizeof(g_stackCopy);
        MEMORY_BASIC_INFORMATION info;
        if (VirtualQuery(reinterpret_cast<void*>(stackTop), &info, sizeof(info))) {
            const uintptr_t regionEnd = reinterpret_cast<uintptr_t>(info.BaseAddress) + info.RegionSize;
            bytes = (std::min)(bytes, static_cast<size_t>(regionEnd - stackTop));
        }
        __try {
            memcpy(g_stackCopy, reinterpret_cast<void*>(stackTop), bytes);
            copied = bytes / sizeof(DWORD);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            copied = 0;
        }
    }
#endif
    ResumeThread(thread.handle);
    ThreadSample& sample = g_sampleRing[g_sampleCount % kSampleRing];
    sample.qpc = now;
    sample.slot = static_cast<SHORT>(slot);
    sample.call = static_cast<SHORT>(thread.call);
    sample.cpuUs = thread.lastCpu && cpu >= thread.lastCpu
        ? static_cast<DWORD>((cpu - thread.lastCpu) / 10) : 0;
    thread.lastCpu = cpu;
#if defined(_M_IX86)
    sample.eip = context.Eip;
#else
    sample.eip = 0;
#endif
    size_t found = 0;
    for (size_t i = 0; i < copied && found < ARRAYSIZE(sample.frames); ++i) {
        if (LooksLikeReturnAddress(g_stackCopy[i])) sample.frames[found++] = g_stackCopy[i];
    }
    for (; found < ARRAYSIZE(sample.frames); ++found) sample.frames[found] = 0;
    ++g_sampleCount;
}

void DiscoverThreads() {
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return;
    THREADENTRY32 entry = {sizeof(entry)};
    const DWORD process = GetCurrentProcessId();
    for (BOOL more = Thread32First(snapshot, &entry); more;
         more = Thread32Next(snapshot, &entry)) {
        if (entry.th32OwnerProcessID == process) RegisterSampledThread(entry.th32ThreadID, 'o');
    }
    CloseHandle(snapshot);
}

void WriteHitch(const HitchWindow& hitch) {
    wchar_t path[MAX_PATH];
    if (!CombinePath(ExecutableDirectory(), L"d3d9_proxy_hitches.txt", path)) return;
    FILE* file = nullptr;
    if (_wfopen_s(&file, path, L"a") != 0 || !file) return;
    const double ms = QpcToMilliseconds(hitch.end - hitch.start);
    fprintf(file, "HITCH frame %u start %.3f ms dt %.1f ms\n", hitch.frame,
            QpcToMilliseconds(hitch.start), ms);
    const LONG threads = g_sampledThreadCount;
    const size_t first = g_sampleCount > kSampleRing ? g_sampleCount - kSampleRing : 0;
    char where[160];
    for (LONG slot = 0; slot < threads; ++slot) {
        unsigned samples = 0, cpuUs = 0, inCall[kCallKindCount + 1] = {};
        struct Signature { DWORD key[4]; unsigned count; } signatures[16] = {};
        size_t signatureCount = 0;
        for (size_t n = g_sampleCount; n > first; --n) {
            const ThreadSample& sample = g_sampleRing[(n - 1) % kSampleRing];
            if (sample.qpc < hitch.start) break;
            if (sample.slot != slot || sample.qpc > hitch.end) continue;
            ++samples;
            cpuUs += sample.cpuUs;
            inCall[sample.call >= 0 ? sample.call : kCallKindCount]++;
            const DWORD key[4] = {sample.eip, sample.frames[0], sample.frames[1], sample.frames[2]};
            size_t s = 0;
            while (s < signatureCount && memcmp(signatures[s].key, key, sizeof(key)) != 0) ++s;
            if (s == signatureCount && signatureCount < ARRAYSIZE(signatures)) {
                memcpy(signatures[s].key, key, sizeof(key));
                ++signatureCount;
            }
            if (s < signatureCount) signatures[s].count++;
        }
        const char role = g_sampledThreads[slot].role;
        if (!samples || (role == 'o' && cpuUs < 2000)) continue;
        fprintf(file, "  thread %lu %c samples %u cpu %.1f ms |", g_sampledThreads[slot].id,
                role, samples, cpuUs / 1000.0);
        for (int kind = 0; kind <= kCallKindCount; ++kind) {
            if (inCall[kind]) {
                fprintf(file, " %s=%u", kind < kCallKindCount ? kCallKindNames[kind] : "noD3D",
                        inCall[kind]);
            }
        }
        fprintf(file, "\n");
        std::sort(signatures, signatures + signatureCount,
                  [](const Signature& a, const Signature& b) { return a.count > b.count; });
        for (size_t s = 0; s < signatureCount && s < 4; ++s) {
            fprintf(file, "    %3u x", signatures[s].count);
            for (DWORD address : signatures[s].key) {
                if (!address) break;
                DescribeAddress(reinterpret_cast<void*>(static_cast<uintptr_t>(address)), where,
                                sizeof(where));
                fprintf(file, " %s", where);
            }
            fprintf(file, "\n");
        }
    }
    fclose(file);
}

DWORD WINAPI HitchSamplerThread(void*) {
    RefreshModules();
    DiscoverThreads();
    UINT tick = 0;
    while (true) {
        WaitForSingleObject(g_hitchEvent, g_sampleMs);
        LARGE_INTEGER now;
        QueryPerformanceCounter(&now);
        const bool slowTick = tick++ % 10 == 0;
        if (tick % 200 == 0) { RefreshModules(); DiscoverThreads(); }
        const LONG threads = g_sampledThreadCount;
        for (LONG slot = 0; slot < threads; ++slot) {
            if (g_sampledThreads[slot].role != 'o' || slowTick) SampleThread(slot, now.QuadPart);
        }
        while (g_hitchTail != g_hitchHead) {
            const HitchWindow hitch = g_hitches[g_hitchTail % ARRAYSIZE(g_hitches)];
            ++g_hitchTail;
            if (g_hitchesWritten++ < 2000) WriteHitch(hitch);
        }
    }
}

void EnsureTexStatsTables() {
    if (!g_texCreates) {
        g_texCreates = new std::map<TexCreateKey, TexCreateStat>();
        g_texLocks = new std::map<TexLockKey, TexLockStat>();
        g_liveTextures = new std::unordered_map<void*, LiveTexture>();
    }
}

void DumpAppend(const char* format, ...) {
    char text[2048];
    va_list args;
    va_start(args, format);
    const int n = vsnprintf_s(text, sizeof(text), _TRUNCATE, format, args);
    va_end(args);
    const size_t length = n < 0 ? strlen(text) : static_cast<size_t>(n);
    g_dumpBuffer.insert(g_dumpBuffer.end(), text, text + length);
}

template <typename Shader>
uint32_t HashAndDumpShader(Shader* shader, const wchar_t* folder, const wchar_t* prefix) {
    if (!shader) return 0;
    UINT size = 0;
    if (FAILED(shader->GetFunction(nullptr, &size)) || !size || size >= (1u << 20)) return 0;
    std::vector<uint8_t> code(size);
    if (FAILED(shader->GetFunction(code.data(), &size))) return 0;
    uint32_t h = 2166136261u;
    for (uint8_t b : code) { h ^= b; h *= 16777619u; }
    wchar_t dir[MAX_PATH];
    DumpFolder(folder, dir, ARRAYSIZE(dir));
    wchar_t file[MAX_PATH];
    swprintf_s(file, L"%s\\%s_%08X.bin", dir, prefix, h);
    if (GetFileAttributesW(file) == INVALID_FILE_ATTRIBUTES) {
        HANDLE out = CreateFileW(file, GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (out != INVALID_HANDLE_VALUE) {
            DWORD written = 0;
            WriteFile(out, code.data(), size, &written, nullptr);
            CloseHandle(out);
        }
    }
    return h;
}

void DumpSurface(const char* name, IDirect3DSurface9* surface) {
    if (!surface) { DumpAppend("\t%s=0", name); return; }
    static const GUID kBaseTexture9 = {0x580ca87e, 0x1d3c, 0x4d54, {0x99, 0x1d, 0xb7, 0xd3, 0xe3, 0xc2, 0x98, 0xce}};
    D3DSURFACE_DESC d = {};
    surface->GetDesc(&d);
    void* texture = nullptr;
    IDirect3DBaseTexture9* parent = nullptr;
    if (SUCCEEDED(surface->GetContainer(kBaseTexture9, reinterpret_cast<void**>(&parent))) && parent) {
        texture = parent;
        parent->Release();
    }
    DumpAppend("\t%s=%p,%ux%u,f%u,u%lX,ms%u,tex%p", name, surface, d.Width, d.Height, static_cast<UINT>(d.Format),
               d.Usage, static_cast<UINT>(d.MultiSampleType), texture);
}

void DumpFloats(const char* name, const float* values, int count) {
    DumpAppend("\t%s=", name);
    for (int i = 0; i < count; ++i) DumpAppend(i ? ",%.6g" : "%.6g", values[i]);
}

void CaptureDrawFull(IDirect3DDevice9* device,
                     LONG sequence,
                     const char* kind,
                     UINT vertices,
                     UINT primitives,
                     void* caller,
                     uint32_t vsHash,
                     const DrawCaptureContext& ctx) {
    IDirect3DPixelShader9* ps = nullptr;
    device->GetPixelShader(&ps);
    const uint32_t psHash = HashAndDumpShader(ps, L"psdump", L"ps");
    DumpAppend("draw\tseq=%ld\tkind=%s\tverts=%u\tprims=%u\tcaller=%p\tvs=%08X\tps=%08X", sequence, kind, vertices,
               primitives, caller, vsHash, psHash);
    for (DWORD i = 0; i < 4; ++i) {
        IDirect3DSurface9* rt = nullptr;
        char name[8];
        sprintf_s(name, "rt%lu", i);
        if (FAILED(device->GetRenderTarget(i, &rt))) rt = nullptr;
        DumpSurface(name, rt);
        if (rt) rt->Release();
    }
    IDirect3DSurface9* depth = nullptr;
    if (FAILED(device->GetDepthStencilSurface(&depth))) depth = nullptr;
    DumpSurface("ds", depth);
    if (depth) depth->Release();
    D3DVIEWPORT9 vp = {};
    device->GetViewport(&vp);
    DumpAppend("\trtback=%d\tvp=%lu,%lu,%lu,%lu,%.4f,%.4f", ctx.rt0IsBackbuffer ? 1 : 0, vp.X, vp.Y, vp.Width, vp.Height,
               vp.MinZ, vp.MaxZ);
    static const struct { const char* name; D3DRENDERSTATETYPE id; } kStates[] = {
        {"zen", D3DRS_ZENABLE}, {"zwr", D3DRS_ZWRITEENABLE}, {"zfn", D3DRS_ZFUNC}, {"cull", D3DRS_CULLMODE},
        {"at", D3DRS_ALPHATESTENABLE}, {"aref", D3DRS_ALPHAREF}, {"afn", D3DRS_ALPHAFUNC},
        {"bl", D3DRS_ALPHABLENDENABLE}, {"src", D3DRS_SRCBLEND}, {"dst", D3DRS_DESTBLEND}, {"bop", D3DRS_BLENDOP},
        {"sab", D3DRS_SEPARATEALPHABLENDENABLE}, {"cw", D3DRS_COLORWRITEENABLE}, {"cw1", D3DRS_COLORWRITEENABLE1},
        {"st", D3DRS_STENCILENABLE}, {"stfn", D3DRS_STENCILFUNC}, {"stref", D3DRS_STENCILREF},
        {"stpass", D3DRS_STENCILPASS}, {"sc", D3DRS_SCISSORTESTENABLE}, {"srgbw", D3DRS_SRGBWRITEENABLE},
        {"fill", D3DRS_FILLMODE}, {"bias", D3DRS_DEPTHBIAS}, {"fog", D3DRS_FOGENABLE},
    };
    DWORD blend = 0, dst = 0, alphaTest = 0;
    for (const auto& state : kStates) {
        DWORD value = 0;
        device->GetRenderState(state.id, &value);
        if (state.id == D3DRS_ALPHABLENDENABLE) blend = value;
        if (state.id == D3DRS_DESTBLEND) dst = value;
        if (state.id == D3DRS_ALPHATESTENABLE) alphaTest = value;
        DumpAppend("\t%s=%lu", state.name, value);
    }
    for (DWORD i = 0; i < 8; ++i) {
        IDirect3DBaseTexture9* texture = nullptr;
        if (FAILED(device->GetTexture(i, &texture)) || !texture) continue;
        const D3DRESOURCETYPE type = texture->GetType();
        D3DSURFACE_DESC d = {};
        if (type == D3DRTYPE_TEXTURE) static_cast<IDirect3DTexture9*>(texture)->GetLevelDesc(0, &d);
        else if (type == D3DRTYPE_CUBETEXTURE) static_cast<IDirect3DCubeTexture9*>(texture)->GetLevelDesc(0, &d);
        DWORD srgb = 0;
        device->GetSamplerState(i, D3DSAMP_SRGBTEXTURE, &srgb);
        DumpAppend("\tt%lu=%p,ty%u,%ux%u,f%u,u%lX,l%lu,srgb%lu", i, texture, static_cast<UINT>(type), d.Width, d.Height,
                   static_cast<UINT>(d.Format), d.Usage, texture->GetLevelCount(), srgb);
        texture->Release();
    }
    {
        IDirect3DVertexDeclaration9* decl = nullptr;
        D3DVERTEXELEMENT9 el[MAXD3DDECLLENGTH + 1] = {};
        UINT n = 0;
        if (SUCCEEDED(device->GetVertexDeclaration(&decl)) && decl) {
            n = ARRAYSIZE(el);
            if (FAILED(decl->GetDeclaration(el, &n))) n = 0;
            decl->Release();
        }
        DumpAppend("\tdecl=");
        for (UINT i = 0; i < n && el[i].Stream != 0xFF; ++i)
            DumpAppend("%ss%u/o%u/t%u/u%u/i%u", i ? " " : "", el[i].Stream, el[i].Offset, el[i].Type, el[i].Usage,
                       el[i].UsageIndex);
        IDirect3DVertexBuffer9* vb = nullptr;
        UINT offset = 0, stride = 0;
        if (SUCCEEDED(device->GetStreamSource(0, &vb, &offset, &stride)) && vb) vb->Release();
        DWORD fvf = 0;
        device->GetFVF(&fvf);
        DumpAppend("\tstride0=%u\tfvf=%08lX", stride, fvf);
    }
    const char* action = "sent";
    const auto inList = [](const std::vector<uint32_t>* list, uint32_t h) {
        return list && std::find(list->begin(), list->end(), h) != list->end();
    };
    if (ctx.skipShadowMapDraws && ctx.rt0IsShadowMap) action = "skip-shadowmap";
    else if (ctx.drawIsWasted) action = "skip-lighting";
    else if (inList(ctx.skipVs, vsHash)) action = "skip-vs";
    else if (inList(ctx.skipVsAdditive, vsHash) && blend && dst == D3DBLEND_ONE) action = "skip-vs-additive";
    else if (ctx.rt0IsBackbuffer && lightpub::HidePostFx() && ctx.stageIsRt) {
        for (DWORD i = 0; i < ctx.trackedStages; ++i)
            if (ctx.stageIsRt[i]) action = "skip-postfx";
    }
    const bool texkill = ps && ctx.lookupPsTraits && ctx.lookupPsTraits(ps).hasTexkill;
    const bool hinted = texkill && ctx.alphaTestFromTexkill && lightpub::RaytracingOn() && !alphaTest;
    DumpAppend("\taction=%s\ttexkill=%d\talphahint=%d\tmenuvs=%d", action, texkill ? 1 : 0, hinted ? 1 : 0,
               inList(ctx.menuVs, vsHash) ? 1 : 0);
    float constants[64] = {};
    if (SUCCEEDED(device->GetVertexShaderConstantF(0, constants, 16))) DumpFloats("vsc0-15", constants, 64);
    if (SUCCEEDED(device->GetVertexShaderConstantF(16, constants, 16))) DumpFloats("vsc16-31", constants, 64);
    if (SUCCEEDED(device->GetPixelShaderConstantF(0, constants, 16))) DumpFloats("psc0-15", constants, 64);
    {
        static float wide[152 * 4];
        if (SUCCEEDED(device->GetPixelShaderConstantF(16, wide, 152))) DumpFloats("psc16-167", wide, 152 * 4);
    }
    {
        BOOL switches[16] = {};
        if (SUCCEEDED(device->GetPixelShaderConstantB(0, switches, 16))) {
            DumpAppend("\tpsb0-15=");
            for (int i = 0; i < 16; ++i) DumpAppend("%d", switches[i] ? 1 : 0);
        }
    }
    DumpAppend("\tpassfilter=%d\tscene=%d\tbracket=%d\thandoffdone=%d\tdecal=%d", ctx.passFilter ? 1 : 0,
               ctx.drawIsScene ? 1 : 0, ctx.bracketOpen ? 1 : 0, ctx.markerDone ? 1 : 0, ctx.drawIsDecal ? 1 : 0);
    DumpAppend("\n");
    if (ps) ps->Release();
}

}  // namespace

void ConfigureDiagnostics(DiagnosticsConfig* cfg) {
    if (!CombinePath(ExecutableDirectory(), L"d3d9_proxy_frames.csv", g_frameTracePath) ||
        !CombinePath(ExecutableDirectory(), L"d3d9_proxy_calls.csv", g_callTracePath) ||
        !CombinePath(ExecutableDirectory(), L"d3d9_proxy_gate.csv", g_gateTracePath)) {
        cfg->frameTrace = false;
        cfg->callTiming = false;
    }
    g_frameTrace = cfg->frameTrace;
    g_frameTraceMax = cfg->frameTraceMax;
    g_callTiming = cfg->callTiming;
    g_frameTiming = cfg->frameTiming;
    g_hitchSampler = cfg->hitchSampler;
    g_sampleMs = cfg->sampleMs;
    g_hitchMs = cfg->hitchMs;
    g_texStats = cfg->texStats;
}

void InitDiagnosticsQpc() {
    LARGE_INTEGER frequency;
    QueryPerformanceFrequency(&frequency);
    g_qpcFrequency = frequency.QuadPart;
}

double QpcToMilliseconds(LONGLONG ticks) {
    return g_qpcFrequency ? static_cast<double>(ticks) * 1000.0 / static_cast<double>(g_qpcFrequency) : 0.0;
}

bool IsFrameTraceEnabled() { return g_frameTrace; }
bool IsCallTimingEnabled() { return g_callTiming; }
bool IsHitchSamplerEnabled() { return g_hitchSampler; }
bool IsTexStatsEnabled() { return g_texStats; }
UINT FrameTimingMode() { return g_frameTiming; }

void HitchNoteCall(LONG kind) {
    if (!g_hitchSampler) return;
    if (t_sampledSlot == -1) {
        const LONG slot = RegisterSampledThread(GetCurrentThreadId(), 'D');
        t_sampledSlot = slot >= 0 ? slot : -2;
    }
    if (t_sampledSlot >= 0) g_sampledThreads[t_sampledSlot].call = kind;
}

bool BeginTimedCall(CallKind kind, LARGE_INTEGER* startOut) {
    HitchNoteCall(kind);
    if (!g_callTiming) return false;
    QueryPerformanceCounter(startOut);
    return true;
}

void EndTimedCall(CallKind kind, LARGE_INTEGER start, bool timed) {
    HitchNoteCall(-1);
    if (!timed) return;
    LARGE_INTEGER end;
    QueryPerformanceCounter(&end);
    InterlockedExchangeAdd64(&g_callTicks[kind], end.QuadPart - start.QuadPart);
    InterlockedIncrement(&g_callCounts[kind]);
}

void TraceLoopFrame(LONGLONG real, LONGLONG out) {
    if (g_loopFrames >= g_frameTraceMax) return;
    if (!g_loopTraceBuffer) {
        g_loopTraceBuffer = static_cast<char*>(HeapAlloc(GetProcessHeap(), 0, kLoopTraceBufferSize));
        g_loopLastFlush = real;
        if (!g_loopTraceBuffer) return;
    }
    const double scale = 1000.0 / static_cast<double>(g_loopFrequency);
    const int written = _snprintf_s(g_loopTraceBuffer + g_loopTraceLength,
                                    kLoopTraceBufferSize - g_loopTraceLength, _TRUNCATE,
                                    "%u,%.3f,%.3f\r\n", g_loopFrames,
                                    static_cast<double>(real) * scale,
                                    static_cast<double>(out) * scale);
    if (written > 0) g_loopTraceLength += static_cast<size_t>(written);
    ++g_loopFrames;
    if (kLoopTraceBufferSize - g_loopTraceLength < 256 || real - g_loopLastFlush >= g_loopFrequency ||
        g_loopFrames == g_frameTraceMax) {
        AppendToFile(g_loopTracePath, g_loopTraceBuffer, &g_loopTraceLength);
        g_loopLastFlush = real;
    }
}

void InstallLoopTimer(uintptr_t executableBase) {
    if (!g_frameTiming) return;
    BYTE* site = reinterpret_cast<BYTE*>(kLoopTimerCall);
    if (executableBase != 0x00400000u || memcmp(site, kLoopTimerCallBytes, sizeof(kLoopTimerCallBytes)) != 0) {
        WriteLog("frame timing disabled: main loop timer call not found at %p (exe base %p)",
                 site, reinterpret_cast<void*>(executableBase));
        g_frameTiming = 0;
        return;
    }
    LARGE_INTEGER frequency;
    QueryPerformanceFrequency(&frequency);
    g_loopFrequency = frequency.QuadPart;
    if (!CombinePath(ExecutableDirectory(), L"d3d9_proxy_loop.csv", g_loopTracePath)) {
        g_loopTracePath[0] = L'\0';
    }
    BYTE patch[6] = {0xE8, 0, 0, 0, 0, 0x90};
    const int32_t relative = static_cast<int32_t>(
        reinterpret_cast<uintptr_t>(&LoopQueryPerformanceCounter) - (kLoopTimerCall + 5));
    memcpy(patch + 1, &relative, sizeof(relative));
    DWORD oldProtection;
    if (!VirtualProtect(site, sizeof(patch), PAGE_EXECUTE_READWRITE, &oldProtection)) {
        WriteLog("frame timing disabled: VirtualProtect failed (%lu)", GetLastError());
        g_frameTiming = 0;
        return;
    }
    memcpy(site, patch, sizeof(patch));
    VirtualProtect(site, sizeof(patch), oldProtection, &oldProtection);
    FlushInstructionCache(GetCurrentProcess(), site, sizeof(patch));
    WriteLog("frame timing %u installed at %p -> %p", g_frameTiming, site,
             reinterpret_cast<void*>(&LoopQueryPerformanceCounter));
}

void LogCameraResult(DeviceHook* hook,
                     IDirect3DDevice9* device,
                     bool accepted,
                     const D3DMATRIX* world,
                     const D3DMATRIX* projection,
                     HRESULT worldResult,
                     HRESULT viewResult,
                     HRESULT projectionResult,
                     const CameraRejection* rejection,
                     const UploadInfo* upload,
                     const float constants[12][4]) {
    if (!LoggingEnabled()) return;
    const DWORD now = GetTickCount();
    DWORD* last = accepted ? &hook->lastAcceptedLogTick : &hook->lastRejectedLogTick;
    uint32_t* suppressed = accepted ? &hook->suppressedAcceptedLogs : &hook->suppressedRejectedLogs;
    uint32_t suppressedCount;

    AcquireSRWLockExclusive(&hook->constantsLock);
    if (*last != 0 && now - *last < 5000) {
        ++*suppressed;
        ReleaseSRWLockExclusive(&hook->constantsLock);
        return;
    }
    *last = now;
    suppressedCount = *suppressed;
    *suppressed = 0;
    ReleaseSRWLockExclusive(&hook->constantsLock);

    if (accepted) {
        WriteLog("camera decomposition accepted for device %p "
                 "(suppressed %u; source=%s callerRva=0x%08IX; "
                 "world translation %.3f %.3f %.3f; "
                 "projection %.5f %.5f %.5f %.5f; SetTransform HRESULTs "
                 "0x%08lX 0x%08lX 0x%08lX)",
                 device, suppressedCount,
                 CameraSourceName(upload),
                 upload ? upload->callerRva : 0,
                 world->m[3][0], world->m[3][1], world->m[3][2],
                 projection->m[0][0], projection->m[1][1],
                 projection->m[2][2], projection->m[3][2],
                 static_cast<unsigned long>(worldResult),
                 static_cast<unsigned long>(viewResult),
                 static_cast<unsigned long>(projectionResult));
    } else {
        WriteLog("camera decomposition rejected for device %p "
                 "(suppressed %u; check=%s value=%.9g threshold=%.9g; "
                 "validRegisters=0x%08X pendingRegisters=0x%08X; "
                 "candidateCalls=%u worldUpload=c%u+%u wvpUpload=c%u+%u; "
                 "source=%s callerRva=0x%08IX)",
                 device, suppressedCount,
                 rejection ? rejection->check : "unknown",
                 rejection ? rejection->value : 0.0,
                 rejection ? rejection->limit : 0.0,
                 upload ? upload->validRegisters : 0,
                 upload ? upload->pendingRegisters : 0,
                 upload ? upload->candidateCallCount : 0,
                 upload ? upload->lastWorldUploadStart : 0,
                 upload ? upload->lastWorldUploadCount : 0,
                 upload ? upload->lastWvpUploadStart : 0,
                 upload ? upload->lastWvpUploadCount : 0,
                 CameraSourceName(upload),
                 upload ? upload->callerRva : 0);
        if (upload && upload->dumpRawConstants && constants) {
            WriteLog("camera rejected raw frame: validRegisters=0x%08X "
                     "pendingRegisters=0x%08X candidateCalls=%u capturedUploads=%u",
                     upload->validRegisters, upload->pendingRegisters,
                     upload->candidateCallCount, upload->capturedUploadCount);
            for (UINT i = 0; i < upload->capturedUploadCount; ++i) {
                WriteLog("camera rejected upload[%u]: start=c%u count=%u",
                         i, upload->candidateUploadStart[i],
                         upload->candidateUploadVectorCount[i]);
            }
            for (UINT reg = 5; reg <= 11; ++reg) {
                WriteLog("camera rejected raw c%u = %.9g %.9g %.9g %.9g",
                         reg, constants[reg][0], constants[reg][1],
                         constants[reg][2], constants[reg][3]);
            }
        }
    }
}

void RecordTracedPublish(DeviceHook* hook,
                         uintptr_t callerRva,
                         const D3DMATRIX& view,
                         const float constants[12][4],
                         bool skipped) {
    D3DMATRIX viewProjection;
    for (int column = 0; column < 4; ++column) {
        for (int row = 0; row < 4; ++row) {
            viewProjection.m[row][column] = constants[8 + column][row];
        }
    }
    AcquireSRWLockExclusive(&g_traceLock);
    FrameTrace& frame = hook->frame;
    if (frame.publishCount == 0) {
        frame.firstViewProjection = viewProjection;
    }
    if (frame.publishCount < ARRAYSIZE(frame.publishes)) {
        TracedPublish& publish = frame.publishes[frame.publishCount];
        publish.source = callerRva == kGameplayCameraUploadReturnRva ? 'G' : 'P';
        publish.skipped = skipped;
        publish.drawsBefore = static_cast<UINT>(frame.draws);
        for (int j = 0; j < 3; ++j) {
            publish.position[j] = -(view.m[3][0] * view.m[j][0] +
                                    view.m[3][1] * view.m[j][1] +
                                    view.m[3][2] * view.m[j][2]);
            publish.forward[j] = view.m[j][2];
        }
        float difference = 0.0f;
        for (int row = 0; row < 4; ++row) {
            for (int column = 0; column < 4; ++column) {
                difference = (std::max)(difference,
                    fabsf(viewProjection.m[row][column] - frame.firstViewProjection.m[row][column]));
            }
        }
        publish.vpDifference = difference;
    }
    ++frame.publishCount;
    ReleaseSRWLockExclusive(&g_traceLock);
}

void FlushFrameTrace(DeviceHook* hook) {
    AppendToFile(g_frameTracePath, hook->traceBuffer, &hook->traceLength);
    AppendToFile(g_callTracePath, hook->callBuffer, &hook->callLength);
    AppendToFile(g_gateTracePath, hook->gateBuffer, &hook->gateLength);
}

void RecordFrameCalls(DeviceHook* hook, size_t bufferSize, LONGLONG presentTicks) {
    InterlockedExchangeAdd64(&g_callTicks[kPresent], presentTicks);
    InterlockedIncrement(&g_callCounts[kPresent]);
    char* line = hook->callBuffer + hook->callLength;
    size_t remaining = bufferSize - hook->callLength;
    int written = _snprintf_s(line, remaining, _TRUNCATE, "%u", hook->tracedFrames);
    for (int kind = 0; kind < kCallKindCount && written > 0; ++kind) {
        const LONG count = InterlockedExchange(&g_callCounts[kind], 0);
        const LONG64 ticks = InterlockedExchange64(&g_callTicks[kind], 0);
        if (count == 0) continue;
        hook->callLength += static_cast<size_t>(written);
        line = hook->callBuffer + hook->callLength;
        remaining = bufferSize - hook->callLength;
        written = _snprintf_s(line, remaining, _TRUNCATE, ",%s=%ld:%.3f",
                              kCallKindNames[kind], count, QpcToMilliseconds(ticks));
    }
    if (written > 0) {
        hook->callLength += static_cast<size_t>(written);
        if (bufferSize - hook->callLength > 2) {
            hook->callBuffer[hook->callLength++] = '\r';
            hook->callBuffer[hook->callLength++] = '\n';
        }
    }
}

void FinishTracedFrame(DeviceHook* hook, LONGLONG entry, LONGLONG exit, const GateFrameMetrics& gate) {
    constexpr size_t kTraceBufferSize = 256 * 1024;
    constexpr size_t kLineReserve = 2048;
    AcquireSRWLockExclusive(&g_traceLock);
    FrameTrace& frame = hook->frame;
    if (hook->tracedFrames < g_frameTraceMax) {
        if (!hook->traceBuffer) {
            hook->traceBuffer = static_cast<char*>(
                HeapAlloc(GetProcessHeap(), 0, kTraceBufferSize));
            hook->lastTraceFlush = entry;
        }
        if (hook->traceBuffer) {
            char* line = hook->traceBuffer + hook->traceLength;
            size_t remaining = kTraceBufferSize - hook->traceLength;
            int written = _snprintf_s(
                line, remaining, _TRUNCATE, "%u,%.3f,%.3f,%.3f,%ld,%u,",
                hook->tracedFrames, QpcToMilliseconds(entry),
                frame.lastPresentEntry ? QpcToMilliseconds(entry - frame.lastPresentEntry) : 0.0,
                QpcToMilliseconds(exit - entry), frame.draws, frame.publishCount);
            const UINT recorded = (std::min)(frame.publishCount,
                                             static_cast<UINT>(ARRAYSIZE(frame.publishes)));
            for (UINT i = 0; i < recorded && written > 0; ++i) {
                const TracedPublish& publish = frame.publishes[i];
                hook->traceLength += static_cast<size_t>(written);
                line = hook->traceBuffer + hook->traceLength;
                remaining = kTraceBufferSize - hook->traceLength;
                written = _snprintf_s(
                    line, remaining, _TRUNCATE,
                    "%s%c:%u:%d:%.3f:%.3f:%.3f:%.4f:%.4f:%.4f:%.6f", i ? "|" : "",
                    publish.source, publish.drawsBefore, publish.skipped ? 1 : 0,
                    publish.position[0], publish.position[1], publish.position[2],
                    publish.forward[0], publish.forward[1], publish.forward[2],
                    publish.vpDifference);
            }
            if (written > 0) {
                hook->traceLength += static_cast<size_t>(written);
                if (kTraceBufferSize - hook->traceLength > 2) {
                    hook->traceBuffer[hook->traceLength++] = '\r';
                    hook->traceBuffer[hook->traceLength++] = '\n';
                }
            }
            if (g_callTiming && !hook->callBuffer) {
                hook->callBuffer = static_cast<char*>(
                    HeapAlloc(GetProcessHeap(), 0, kTraceBufferSize));
            }
            if (g_callTiming && hook->callBuffer) {
                RecordFrameCalls(hook, kTraceBufferSize, exit - entry);
            }
            if (!hook->gateBuffer) {
                hook->gateBuffer = static_cast<char*>(
                    HeapAlloc(GetProcessHeap(), 0, kTraceBufferSize));
            }
            if (hook->gateBuffer) {
                char* gline = hook->gateBuffer + hook->gateLength;
                const size_t grem = kTraceBufferSize - hook->gateLength;
                const int gw = _snprintf_s(
                    gline, grem, _TRUNCATE, "%u,%u,%u,%u,%d,%d\r\n",
                    hook->tracedFrames, gate.sceneDraws, gate.skinnedSceneDraws,
                    gate.handoffs, gate.goggles ? 1 : 0, gate.rtActive ? 1 : 0);
                if (gw > 0) hook->gateLength += static_cast<size_t>(gw);
            }
            ++hook->tracedFrames;
            if (kTraceBufferSize - hook->traceLength < kLineReserve ||
                kTraceBufferSize - hook->callLength < kLineReserve ||
                kTraceBufferSize - hook->gateLength < kLineReserve ||
                QpcToMilliseconds(entry - hook->lastTraceFlush) >= 250.0 ||
                hook->tracedFrames == g_frameTraceMax) {
                FlushFrameTrace(hook);
                hook->lastTraceFlush = entry;
            }
        }
    }
    frame.publishCount = 0;
    frame.draws = 0;
    frame.lastPresentEntry = entry;
    ReleaseSRWLockExclusive(&g_traceLock);
}

void HitchSamplerFrame(LONGLONG entry) {
    static LONGLONG previous = 0;
    static UINT frame = 0;
    static bool started = false;
    if (!started) {
        started = true;
        RegisterSampledThread(GetCurrentThreadId(), 'R');
        g_sampleRing = static_cast<ThreadSample*>(
            VirtualAlloc(nullptr, sizeof(ThreadSample) * kSampleRing, MEM_COMMIT | MEM_RESERVE,
                         PAGE_READWRITE));
        g_hitchEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (g_sampleRing && g_hitchEvent) {
            HANDLE sampler = CreateThread(nullptr, 0, HitchSamplerThread, nullptr, 0,
                                          &g_samplerThreadId);
            if (sampler) {
                SetThreadPriority(sampler, THREAD_PRIORITY_TIME_CRITICAL);
                CloseHandle(sampler);
                WriteLog("HitchSampler: started (every %u ms, hitch > %.0f ms)", g_sampleMs,
                         g_hitchMs);
            }
        }
    }
    ++frame;
    if (previous && QpcToMilliseconds(entry - previous) > g_hitchMs &&
        g_hitchHead - g_hitchTail < static_cast<LONG>(ARRAYSIZE(g_hitches))) {
        g_hitches[g_hitchHead % ARRAYSIZE(g_hitches)] = {previous, entry, frame};
        InterlockedIncrement(&g_hitchHead);
        SetEvent(g_hitchEvent);
    }
    previous = entry;
}

void RecordTextureCreate(void* caller,
                         IDirect3DTexture9* texture,
                         UINT width,
                         UINT height,
                         UINT levels,
                         DWORD usage,
                         D3DFORMAT format,
                         D3DPOOL pool,
                         double ms,
                         LONG presentCount) {
    void* stack[6] = {};
    CaptureStackBackTrace(1, ARRAYSIZE(stack), stack, nullptr);
    AcquireSRWLockExclusive(&g_texStatsLock);
    EnsureTexStatsTables();
    TexCreateStat& stat = (*g_texCreates)[TexCreateKey(caller, width, height, levels, usage,
                                                        static_cast<DWORD>(format),
                                                        static_cast<DWORD>(pool))];
    if (stat.count++ == 0) memcpy(stat.stack, stack, sizeof(stack));
    stat.createMs += ms;
    (*g_liveTextures)[texture] = {presentCount, &stat};
    ReleaseSRWLockExclusive(&g_texStatsLock);
}

void RecordTextureLock(void* caller,
                       IDirect3DTexture9* texture,
                       UINT level,
                       const RECT* rect,
                       DWORD flags,
                       double ms,
                       LONG presentCount) {
    AcquireSRWLockExclusive(&g_texStatsLock);
    EnsureTexStatsTables();
    int age = 3;
    const auto live = g_liveTextures->find(texture);
    if (live != g_liveTextures->end()) {
        const LONG frames = presentCount - live->second.createFrame;
        age = frames == 0 ? 0 : frames <= 10 ? 1 : 2;
        live->second.stat->locks++;
        live->second.stat->lockMs += ms;
    }
    TexLockStat& stat = (*g_texLocks)[TexLockKey(caller, level, flags, rect ? 0 : 1, age)];
    stat.count++;
    stat.ms += ms;
    ReleaseSRWLockExclusive(&g_texStatsLock);
}

void OnTextureReleasedForStats(void* texture, LONG presentCount) {
    if (!g_texStats) return;
    AcquireSRWLockExclusive(&g_texStatsLock);
    if (g_liveTextures) {
        const auto live = g_liveTextures->find(texture);
        if (live != g_liveTextures->end()) {
            const LONG frames = presentCount - live->second.createFrame;
            live->second.stat->released++;
            live->second.stat->lifetimeFrames += static_cast<unsigned long long>(frames);
            const int bucket = frames == 0 ? 0 : frames == 1 ? 1 : frames <= 10 ? 2 :
                frames <= 100 ? 3 : frames <= 1000 ? 4 : frames <= 10000 ? 5 : 6;
            g_lifetimeHistogram[bucket]++;
            g_liveTextures->erase(live);
        }
    }
    ReleaseSRWLockExclusive(&g_texStatsLock);
}

void NoteAvailableTextureMem(UINT available, void* caller) {
    if (available != g_lastAvailableTextureMem) {
        g_lastAvailableTextureMem = available;
        WriteLog("TexStats: GetAvailableTextureMem = %u bytes (%u MB) caller=%p", available,
                 available >> 20, caller);
    }
}

void DumpTexStats(LONG presentCount) {
    wchar_t path[MAX_PATH];
    if (!CombinePath(ExecutableDirectory(), L"d3d9_proxy_texstats.txt", path)) return;
    FILE* file = nullptr;
    if (_wfopen_s(&file, path, L"w") != 0 || !file) return;
    AcquireSRWLockShared(&g_texStatsLock);
    fprintf(file, "frames %ld  live textures %u  GetAvailableTextureMem %u MB\n", presentCount,
            g_liveTextures ? static_cast<unsigned>(g_liveTextures->size()) : 0u,
            g_lastAvailableTextureMem >> 20);
    fprintf(file, "lifetime (frames) of released textures: 0:%llu 1:%llu 2-10:%llu "
            "11-100:%llu 101-1000:%llu 1001-10000:%llu more:%llu\n\n",
            g_lifetimeHistogram[0], g_lifetimeHistogram[1], g_lifetimeHistogram[2],
            g_lifetimeHistogram[3], g_lifetimeHistogram[4], g_lifetimeHistogram[5],
            g_lifetimeHistogram[6]);
    char where[160];
    if (g_texCreates) {
        std::vector<std::pair<const TexCreateKey*, const TexCreateStat*>> rows;
        for (const auto& entry : *g_texCreates) rows.emplace_back(&entry.first, &entry.second);
        std::sort(rows.begin(), rows.end(),
                  [](const auto& a, const auto& b) { return a.second->count > b.second->count; });
        fprintf(file, "CREATES: count released avgLifeFrames locks lockMs createMs | caller "
                "w x h levels usage format pool | stack\n");
        for (size_t i = 0; i < rows.size() && i < 60; ++i) {
            const TexCreateKey& key = *rows[i].first;
            const TexCreateStat& stat = *rows[i].second;
            DescribeAddress(std::get<0>(key), where, sizeof(where));
            fprintf(file, "%8llu %8llu %8.1f %9llu %9.1f %9.1f | %s %ux%u L%u u=0x%lX f=0x%08lX "
                    "p=%lu |", stat.count, stat.released,
                    stat.released ? double(stat.lifetimeFrames) / double(stat.released) : -1.0,
                    stat.locks, stat.lockMs, stat.createMs, where, std::get<1>(key),
                    std::get<2>(key), std::get<3>(key), std::get<4>(key), std::get<5>(key),
                    std::get<6>(key));
            for (void* frame : stat.stack) {
                if (!frame) break;
                DescribeAddress(frame, where, sizeof(where));
                fprintf(file, " %s", where);
            }
            fprintf(file, "\n");
        }
    }
    if (g_texLocks) {
        std::vector<std::pair<const TexLockKey*, const TexLockStat*>> rows;
        for (const auto& entry : *g_texLocks) rows.emplace_back(&entry.first, &entry.second);
        std::sort(rows.begin(), rows.end(),
                  [](const auto& a, const auto& b) { return a.second->ms > b.second->ms; });
        fprintf(file, "\nLOCKS (by time): count ms | caller level flags wholeLevel "
                "age(0 same frame as create,1 <=10 frames,2 older,3 untracked)\n");
        for (size_t i = 0; i < rows.size() && i < 60; ++i) {
            const TexLockKey& key = *rows[i].first;
            DescribeAddress(std::get<0>(key), where, sizeof(where));
            fprintf(file, "%10llu %10.1f | %s L%u flags=0x%lX whole=%d age=%d\n",
                    rows[i].second->count, rows[i].second->ms, where, std::get<1>(key),
                    std::get<2>(key), std::get<3>(key), std::get<4>(key));
        }
    }
    ReleaseSRWLockShared(&g_texStatsLock);
    fclose(file);
}

void MaybeDumpTexStats(LONGLONG exitQpc, LONG presentCount) {
    if (g_texStats && QpcToMilliseconds(exitQpc - g_lastTexStatsDump) >= 10000.0) {
        g_lastTexStatsDump = exitQpc;
        DumpTexStats(presentCount);
    }
}

void SetSkinTraceEnabled(bool enabled) { g_skinTrace = enabled; }
bool IsSkinTraceEnabled() { return g_skinTrace; }

void RecordSkinnedDraw(FrameStage stage,
                       bool isScene,
                       bool rt1Bound,
                       bool alphaBlendEnabled,
                       const ShaderTraits& ps,
                       const DrawGeometryKey* geometry) {
    if (!g_skinTrace || stage == FrameStage::ShadowMap || stage == FrameStage::DepthPrePass) return;
    if (!isScene) {
        ++g_skinFrame.forwardOther;
    } else {
        ++(rt1Bound ? g_skinFrame.gbuffer : g_skinFrame.forward);
        if (alphaBlendEnabled) ++g_skinFrame.blended;
        if (ps.fadeValue) ++g_skinFrame.fade;
        if (ps.dissolve) ++g_skinFrame.dissolve;
        if (geometry) g_skinFrame.triangles += geometry->primitives;
        g_skinFrame.shaders += ps.hash;
    }
}

void FinishSkinTraceFrame(LONG presentCount) {
    if (g_skinTrace && memcmp(&g_skinFrame, &g_skinLast, sizeof(g_skinFrame))) {
        WriteLog("SkinTrace: frame %ld skinned draws to ray tracing: %u G-buffer + %u forward (%u blended, %u fade shader, "
                 "%u dissolve shader), %u triangles, shaders %08X; %u forward kept out", presentCount, g_skinFrame.gbuffer,
                 g_skinFrame.forward, g_skinFrame.blended, g_skinFrame.fade, g_skinFrame.dissolve, g_skinFrame.triangles,
                 g_skinFrame.shaders, g_skinFrame.forwardOther);
        g_skinLast = g_skinFrame;
    }
    g_skinFrame = {};
}

bool g_captureDrawsRequested = false;
int g_captureDrawsBurstCfg = 0;
bool IsCaptureDrawsActive() { return g_captureDraws; }

void SetCaptureDrawsConfig(bool requested, int burstCount) {
    g_captureDrawsRequested = requested;
    g_captureDrawsBurstCfg = std::clamp(burstCount, 0, 120);
}

void BeginFrameDump(LONG presentCount) {
    g_dumpBuffer.clear();
    g_captureDrawSequence = 0;
    g_dumpFrame = presentCount;
    SYSTEMTIME now;
    GetLocalTime(&now);
    DumpAppend("frame\tpresent=%ld\ttime=%02u:%02u:%02u.%03u\trton=%d\n", g_dumpFrame, now.wHour, now.wMinute,
               now.wSecond, now.wMilliseconds, lightpub::RaytracingOn() ? 1 : 0);
    wchar_t dir[MAX_PATH], file[MAX_PATH];
    DumpFolder(L"framedump", dir, ARRAYSIZE(dir));
    swprintf_s(file, L"%s\\f%ld_lights.tsv", dir, g_dumpFrame);
    lightpub::DumpLights(file);
}

void FlushFrameDump() {
    wchar_t dir[MAX_PATH], file[MAX_PATH];
    DumpFolder(L"framedump", dir, ARRAYSIZE(dir));
    swprintf_s(file, L"%s\\f%ld_draws.tsv", dir, g_dumpFrame);
    HANDLE out = CreateFileW(file, GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (out != INVALID_HANDLE_VALUE) {
        DWORD written = 0;
        WriteFile(out, g_dumpBuffer.data(), static_cast<DWORD>(g_dumpBuffer.size()), &written, nullptr);
        CloseHandle(out);
    }
    WriteLog("FrameDump: frame %ld, %ld draws, %u bytes -> framedump\\f%ld_draws.tsv", g_dumpFrame,
             g_captureDrawSequence, static_cast<unsigned>(g_dumpBuffer.size()), g_dumpFrame);
    g_dumpBuffer.clear();
    g_dumpBuffer.shrink_to_fit();
}

void UpdateCaptureDrawsEndFrame(LONG presentCount) {
    if (g_captureDraws) FlushFrameDump();
    const bool requested = g_captureDrawsRequested;
    static int burstLeft = 0;
    if (requested && !g_captureDrawsPrevious) {
        burstLeft = g_captureDrawsBurstCfg;
    }
    g_captureDraws = requested && (!g_captureDrawsPrevious || (burstLeft > 0 && !(presentCount % 6)));
    if (g_captureDraws && g_captureDrawsPrevious) --burstLeft;
    g_captureDrawsPrevious = requested;
    if (g_captureDraws) BeginFrameDump(presentCount);
}

void CaptureDraw(IDirect3DDevice9* device,
                 const char* kind,
                 UINT vertices,
                 UINT primitives,
                 void* caller,
                 const DrawCaptureContext& ctx) {
    if (!g_captureDraws) return;
    const LONG sequence = InterlockedIncrement(&g_captureDrawSequence);
    DWORD z = 0, blend = 0, src = 0, dst = 0, fvf = 0;
    HRESULT hz = device->GetRenderState(D3DRS_ZENABLE, &z);
    HRESULT hb = device->GetRenderState(D3DRS_ALPHABLENDENABLE, &blend);
    device->GetRenderState(D3DRS_SRCBLEND, &src);
    device->GetRenderState(D3DRS_DESTBLEND, &dst);
    device->GetFVF(&fvf);
    D3DVIEWPORT9 vp = {};
    HRESULT hv = device->GetViewport(&vp);
    IDirect3DVertexShader9* vs = nullptr;
    IDirect3DPixelShader9* ps = nullptr;
    IDirect3DBaseTexture9* tex = nullptr;
    device->GetVertexShader(&vs);
    device->GetPixelShader(&ps);
    device->GetTexture(0, &tex);
    D3DSURFACE_DESC rtDesc = {}, textureDesc = {};
    IDirect3DSurface9* rt = nullptr;
    const HRESULT hr = device->GetRenderTarget(0, &rt);
    const HRESULT hrd = rt ? rt->GetDesc(&rtDesc) : D3DERR_INVALIDCALL;
    const D3DRESOURCETYPE textureType = tex ? tex->GetType() : D3DRTYPE_FORCE_DWORD;
    HRESULT htd = D3DERR_INVALIDCALL;
    if (tex && textureType == D3DRTYPE_TEXTURE) {
        htd = static_cast<IDirect3DTexture9*>(tex)->GetLevelDesc(0, &textureDesc);
    }
    const uint32_t vsHash = HashAndDumpShader(vs, L"vsdump", L"vs");
    WriteLog("drawsig id=%ld vshash=%08X minz=%.4f maxz=%.4f", sequence, vsHash, vp.MinZ, vp.MaxZ);
    {
        IDirect3DVertexDeclaration9* decl = nullptr;
        D3DVERTEXELEMENT9 el[MAXD3DDECLLENGTH + 1] = {};
        UINT n = 0;
        if (SUCCEEDED(device->GetVertexDeclaration(&decl)) && decl) {
            n = ARRAYSIZE(el);
            if (FAILED(decl->GetDeclaration(el, &n))) n = 0;
            decl->Release();
        }
        char text[1024] = {};
        size_t used = 0;
        for (UINT i = 0; i < n && el[i].Stream != 0xFF && used + 32 < sizeof(text); ++i)
            used += sprintf_s(text + used, sizeof(text) - used, " s%u/o%u/t%u/u%u/i%u", el[i].Stream, el[i].Offset,
                              el[i].Type, el[i].Usage, el[i].UsageIndex);
        WriteLog("drawsig id=%ld decl%s", sequence, text);
    }
    float c[28] = {};
    HRESULT hc = device->GetVertexShaderConstantF(5, c, 7);
    WriteLog("drawsig id=%ld frame=%ld kind=%s caller=%p verts=%u prims=%u rtback=%d rttex=%d vs=%p ps=%p tex0=%p fvf=%08lX z=%lu blend=%lu src=%lu dst=%lu vp=%lu,%lu,%lu,%lu get=%08lX,%08lX,%08lX,%08lX rtget=%08lX,%08lX rt=%ux%u,fmt%u,usage%08lX texget=%08lX type=%u tex=%ux%u,fmt%u,usage%08lX",
        sequence, ctx.presentCount, kind, caller, vertices, primitives, ctx.rt0IsBackbuffer ? 1 : 0,
        (ctx.stageIsRt && ctx.trackedStages > 0 && ctx.stageIsRt[0]) ? 1 : 0, vs, ps, tex, fvf, z, blend, src, dst,
        vp.X, vp.Y, vp.Width, vp.Height, hz, hb, hv, hc, hr, hrd,
        rtDesc.Width, rtDesc.Height, static_cast<UINT>(rtDesc.Format), rtDesc.Usage,
        htd, static_cast<UINT>(textureType), textureDesc.Width, textureDesc.Height,
        static_cast<UINT>(textureDesc.Format), textureDesc.Usage);
    if (SUCCEEDED(hc)) {
        WriteLog("drawsig id=%ld constants c5=%.6g,%.6g,%.6g,%.6g c6=%.6g,%.6g,%.6g,%.6g c7=%.6g,%.6g,%.6g,%.6g c8=%.6g,%.6g,%.6g,%.6g c9=%.6g,%.6g,%.6g,%.6g c10=%.6g,%.6g,%.6g,%.6g c11=%.6g,%.6g,%.6g,%.6g",
            sequence, c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7],
            c[8], c[9], c[10], c[11], c[12], c[13], c[14], c[15],
            c[16], c[17], c[18], c[19], c[20], c[21], c[22], c[23],
            c[24], c[25], c[26], c[27]);
    }
    CaptureDrawFull(device, sequence, kind, vertices, primitives, caller, vsHash, ctx);
    if (rt) rt->Release();
    if (vs) vs->Release();
    if (ps) ps->Release();
    if (tex) tex->Release();
}

void CaptureCameraPublish(uintptr_t callerRva,
                          const D3DMATRIX& view,
                          const D3DMATRIX& projection,
                          bool skipped) {
    DumpAppend("campub\tafterdraw=%ld\tcaller=%08X\tapplied=%d", g_captureDrawSequence,
               static_cast<unsigned>(callerRva), skipped ? 0 : 1);
    DumpFloats("view", &view.m[0][0], 16);
    DumpFloats("proj", &projection.m[0][0], 16);
    DumpAppend("\n");
}

}  // namespace proxy
