#include "decal_tag.h"

#include "lightpub.h"
#include "proxy_log.h"
#include "replay_side.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <deque>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#pragma warning(push, 0)
#define XXH_INLINE_ALL
#include "third_party/xxhash-0.8.2/xxhash.h"
#pragma warning(pop)

namespace proxy {
namespace {

struct CodeRange {
    uintptr_t begin;
    uintptr_t end;
};

struct DecalTexture {
    uint64_t hash = 0;    // Remix hash (0 = not hashed yet)
    bool shared = false;  // an ordinary ray-traced draw used it too (logged)
};

constexpr int kMaxDecalCode = 32;

bool g_decalTag = false;
bool g_hideDecals = false;
bool g_texHashLog = false;
int g_streamBudgetMB = 0;
thread_local bool t_texHashing = false;

CodeRange g_decalCode[kMaxDecalCode] = {};
volatile LONG g_decalCodeCount = 0;
volatile LONG g_decalScanStarted = 0;
bool g_textureReleaseHooked = false;

SRWLOCK g_decalLock = SRWLOCK_INIT;
std::unordered_map<void*, DecalTexture>* g_decalTextures = nullptr;
std::vector<IDirect3DBaseTexture9*> g_decalPending;
std::vector<uint64_t> g_decalHashes;
volatile LONG g_decalTextureCount = 0;
bool g_decalListDirty = false;
LONGLONG g_decalLastSend = 0;
LONG g_statDecalShared = 0;
LONG g_statDecalDraws = 0;
LONG g_statDecalTraced = 0;

bool g_particleTag = false;
SRWLOCK g_particleLock = SRWLOCK_INIT;
std::unordered_map<void*, uint64_t>* g_particleTextures = nullptr;
std::vector<IDirect3DBaseTexture9*> g_particlePending;
std::vector<uint64_t> g_particleHashes;
volatile LONG g_particleTextureCount = 0;
bool g_particleListDirty = false;
bool g_particleClearPending = false;
LONGLONG g_particleLastSend = 0;
SRWLOCK g_texHashLock = SRWLOCK_INIT;
std::deque<IDirect3DTexture9*> g_texHashPending;

// End of a function: the first return instruction followed by CC/90 alignment padding.
uintptr_t FunctionEnd(uintptr_t start, uintptr_t codeEnd) {
    const uint8_t* p = reinterpret_cast<const uint8_t*>(start);
    const uint8_t* limit = reinterpret_cast<const uint8_t*>(std::min<uintptr_t>(start + 0x10000, codeEnd - 5));
    auto pad = [](uint8_t b) { return b == 0xCC || b == 0x90; };
    for (; p < limit; ++p) {
        if (p[0] == 0xC3 && pad(p[1]) && pad(p[2])) return reinterpret_cast<uintptr_t>(p + 1);
        if (p[0] == 0xC2 && pad(p[3]) && pad(p[4])) return reinterpret_cast<uintptr_t>(p + 3);
    }
    return 0;
}

DWORD FindDecalCodeScan() {
    LARGE_INTEGER t0, t1, frequency;
    QueryPerformanceCounter(&t0);
    const uintptr_t base = reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr));
    const auto* dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
    const auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS32*>(base + dos->e_lfanew);
    const IMAGE_SECTION_HEADER* section = IMAGE_FIRST_SECTION(nt);
    uintptr_t codeBegin = 0, codeEnd = 0;
    std::vector<std::pair<uintptr_t, uintptr_t>> data;
    for (WORD i = 0; i < nt->FileHeader.NumberOfSections; ++i, ++section) {
        const uintptr_t begin = base + section->VirtualAddress;
        const uintptr_t end = begin + std::min<DWORD>(section->Misc.VirtualSize, section->SizeOfRawData);
        if (section->Characteristics & IMAGE_SCN_MEM_EXECUTE) {
            if (end - begin > codeEnd - codeBegin) { codeBegin = begin; codeEnd = end; }
        } else if ((section->Characteristics & IMAGE_SCN_MEM_READ) && end > begin) {
            data.push_back({begin, end});
        }
    }
    auto inData = [&](uintptr_t a, size_t size) {
        for (const auto& d : data) if (a >= d.first && a < d.second && size <= d.second - a) return true;
        return false;
    };
    // 1. RTTI type descriptors whose class name ends in DecalMaterialImpl@l3d@@ (name = descriptor + 8).
    static const char kSuffix[] = "DecalMaterialImpl@l3d@@";
    std::vector<uintptr_t> types;
    std::vector<const char*> names;
    for (const auto& d : data) {
        for (uintptr_t p = d.first; p + sizeof(kSuffix) <= d.second && types.size() < 8; ++p) {
            if (memcmp(reinterpret_cast<const void*>(p), kSuffix, sizeof(kSuffix)) != 0) continue;
            uintptr_t name = p;
            while (name > p - 64 && name > d.first + 8 && memcmp(reinterpret_cast<const void*>(name), ".?A", 3) != 0) --name;
            if (memcmp(reinterpret_cast<const void*>(name), ".?A", 3) != 0) continue;
            types.push_back(name - 8);
            names.push_back(reinterpret_cast<const char*>(name));
        }
    }
    // 2. Virtual-function tables whose preceding slot points to a complete object locator for a decal type.
    std::vector<uintptr_t> tables;
    std::vector<int> tableType;
    for (const auto& d : data) {
        for (uintptr_t a = (d.first + 3) & ~uintptr_t(3); a + 8 <= d.second; a += 4) {
            const uintptr_t locator = *reinterpret_cast<const uint32_t*>(a);
            if (!inData(locator, 16) || *reinterpret_cast<const uint32_t*>(locator) != 0) continue;
            const uintptr_t type = *reinterpret_cast<const uint32_t*>(locator + 12);
            for (size_t t = 0; t < types.size(); ++t)
                if (type == types[t]) { tables.push_back(a + 4); tableType.push_back(static_cast<int>(t)); }
        }
    }
    // 3. Methods unique to one decal class (shared slots are generic base-class methods).
    std::vector<std::pair<uintptr_t, int>> methods;
    std::unordered_map<uintptr_t, int> uses;
    for (size_t t = 0; t < tables.size(); ++t) {
        for (int slot = 0; slot < 64; ++slot) {
            if (!inData(tables[t] + slot * 4, 4)) break;
            const uintptr_t fn = *reinterpret_cast<const uint32_t*>(tables[t] + slot * 4);
            if (fn < codeBegin || fn >= codeEnd) break;
            methods.push_back({fn, tableType[t]});
            ++uses[fn];
        }
    }
    LONG count = 0;
    for (const auto& m : methods) {
        if (uses[m.first] != 1 || count >= kMaxDecalCode) continue;
        const uintptr_t end = FunctionEnd(m.first, codeEnd);
        if (!end) continue;
        g_decalCode[count++] = {m.first, end};
        WriteLog("DecalTag: %s method %08X-%08X", names[m.second], static_cast<unsigned>(m.first),
                 static_cast<unsigned>(end));
    }
    MemoryBarrier();
    InterlockedExchange(&g_decalCodeCount, count);
    QueryPerformanceCounter(&t1);
    QueryPerformanceFrequency(&frequency);
    WriteLog("DecalTag: %u decal classes, %u tables, %ld decal render methods found in %.1f ms (background thread)",
             static_cast<unsigned>(types.size()), static_cast<unsigned>(tables.size()), count,
             (t1.QuadPart - t0.QuadPart) * 1000.0 / frequency.QuadPart);
    return 0;
}

}  // namespace

void SetDecalConfig(bool decalTag, bool hideDecals, bool logChanges) {
    if (logChanges && (decalTag != g_decalTag || hideDecals != g_hideDecals)) {
        WriteLog("DecalTag: now %d, HideDecals %d", decalTag ? 1 : 0, hideDecals ? 1 : 0);
        if (!decalTag && g_decalTag) lightpub::SetRemixConfig("rtx.decalTextures", "");
        if (decalTag && !g_decalTag) {
            AcquireSRWLockShared(&g_decalLock);
            g_decalListDirty = !g_decalHashes.empty();
            ReleaseSRWLockShared(&g_decalLock);
        }
    }
    g_decalTag = decalTag;
    g_hideDecals = hideDecals;
}

void SetTexHashLogEnabled(bool enabled) { g_texHashLog = enabled; }
void SetStreamBudgetMB(int budgetMB) { g_streamBudgetMB = budgetMB; }

bool IsDecalTagEnabled() { return g_decalTag; }
bool IsHideDecalsEnabled() { return g_hideDecals; }
void NoteTextureReleaseHooked() { g_textureReleaseHooked = true; }

DWORD WINAPI FindDecalCode(void*) {
    __try {
        return FindDecalCodeScan();
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        WriteLog("DecalTag: scan of the game's memory failed (exception %08lX); decal tagging is off this session",
                 GetExceptionCode());
        return 1;
    }
}

bool IsDecalCaller(void* returnAddress) {
    const LONG count = g_decalCodeCount;
    const uintptr_t a = reinterpret_cast<uintptr_t>(returnAddress);
    for (LONG i = 0; i < count; ++i)
        if (a >= g_decalCode[i].begin && a < g_decalCode[i].end) return true;
    return false;
}

bool RowLayout(D3DFORMAT format, UINT width, UINT height, UINT* rowBytes, UINT* rows) {
    UINT block = 0, pixel = 0;
    switch (static_cast<DWORD>(format)) {
    case D3DFMT_DXT1: case MAKEFOURCC('A', 'T', 'I', '1'): block = 8; break;
    case D3DFMT_DXT2: case D3DFMT_DXT3: case D3DFMT_DXT4: case D3DFMT_DXT5: case MAKEFOURCC('A', 'T', 'I', '2'): block = 16; break;
    case D3DFMT_A8R8G8B8: case D3DFMT_X8R8G8B8: case D3DFMT_A8B8G8R8: case D3DFMT_X8B8G8R8: case D3DFMT_G16R16:
    case D3DFMT_A2R10G10B10: case D3DFMT_A2B10G10R10: case D3DFMT_R32F: case D3DFMT_G16R16F: pixel = 4; break;
    case D3DFMT_R5G6B5: case D3DFMT_X1R5G5B5: case D3DFMT_A1R5G5B5: case D3DFMT_A4R4G4B4: case D3DFMT_A8L8:
    case D3DFMT_L16: case D3DFMT_R16F: pixel = 2; break;
    case D3DFMT_A8: case D3DFMT_L8: case D3DFMT_P8: pixel = 1; break;
    case D3DFMT_A16B16G16R16F: case D3DFMT_A16B16G16R16: case D3DFMT_G32R32F: pixel = 8; break;
    case D3DFMT_A32B32G32R32F: pixel = 16; break;
    default: return false;
    }
    if (block) { *rowBytes = std::max(1u, (width + 3) / 4) * block; *rows = std::max(1u, (height + 3) / 4); }
    else { *rowBytes = width * pixel; *rows = height; }
    return true;
}

bool RemixTextureHash(IDirect3DBaseTexture9* base, uint64_t* hash, D3DSURFACE_DESC* desc) {
    if (base->GetType() != D3DRTYPE_TEXTURE) return false;
    auto* texture = static_cast<IDirect3DTexture9*>(base);
    UINT rowBytes = 0, rows = 0;
    if (FAILED(texture->GetLevelDesc(0, desc)) || desc->Pool != D3DPOOL_MANAGED ||
        !RowLayout(desc->Format, desc->Width, desc->Height, &rowBytes, &rows)) return false;
    D3DLOCKED_RECT locked = {};
    if (FAILED(texture->LockRect(0, &locked, nullptr, D3DLOCK_READONLY)) || !locked.pBits) return false;
    if (static_cast<UINT>(locked.Pitch) == rowBytes) {
        *hash = XXH3_64bits(locked.pBits, static_cast<size_t>(rowBytes) * rows);
    } else {
        XXH3_state_t state;
        XXH3_64bits_reset(&state);
        for (UINT r = 0; r < rows; ++r)
            XXH3_64bits_update(&state, static_cast<const uint8_t*>(locked.pBits) + static_cast<size_t>(r) * locked.Pitch, rowBytes);
        *hash = XXH3_64bits_digest(&state);
    }
    texture->UnlockRect(0);
    return true;
}

bool DecalTagBeforeDraw(void* returnAddress,
                        IDirect3DBaseTexture9* stage0Texture,
                        bool passFilterEnabled,
                        bool drawIsScene,
                        bool gogglesActive) {
    const bool wanted = g_decalTag || g_hideDecals;
    const bool scan = !ReplayMode() && (wanted || ReplayRecording());
    if (scan && !g_decalScanStarted && !InterlockedExchange(&g_decalScanStarted, 1)) {
        HANDLE thread = CreateThread(nullptr, 0, &FindDecalCode, nullptr, 0, nullptr);
        if (thread) { SetThreadPriority(thread, THREAD_PRIORITY_BELOW_NORMAL); CloseHandle(thread); }
    }
    const bool drawIsDecal = ReplaySideDecalDraw(scan && IsDecalCaller(returnAddress));
    if (!wanted) return false;
    const bool traced = g_decalTag && (!passFilterEnabled || drawIsScene) && stage0Texture && g_textureReleaseHooked &&
                        !gogglesActive;
    if (!drawIsDecal) {
        if (!traced || !g_decalTextureCount) return false;
        AcquireSRWLockShared(&g_decalLock);
        const auto it = g_decalTextures->find(stage0Texture);
        const bool newlyShared = it != g_decalTextures->end() && !it->second.shared;
        ReleaseSRWLockShared(&g_decalLock);
        if (!newlyShared) return false;
        AcquireSRWLockExclusive(&g_decalLock);
        const auto again = g_decalTextures->find(stage0Texture);
        const bool first = again != g_decalTextures->end() && !again->second.shared;
        if (first) again->second.shared = true;
        ReleaseSRWLockExclusive(&g_decalLock);
        if (first) {
            InterlockedIncrement(&g_statDecalShared);
            WriteLog("DecalTag: SHARED texture %p (decal) is also used by an ordinary ray-traced draw (caller %p): "
                     "Remix shows that object as a decal", stage0Texture, returnAddress);
        }
        return false;
    }
    InterlockedIncrement(&g_statDecalDraws);
    if (!traced) return true;
    InterlockedIncrement(&g_statDecalTraced);
    AcquireSRWLockExclusive(&g_decalLock);
    if (!g_decalTextures) g_decalTextures = new std::unordered_map<void*, DecalTexture>();
    if (g_decalTextures->emplace(stage0Texture, DecalTexture{}).second) {
        stage0Texture->AddRef();
        g_decalPending.push_back(stage0Texture);
        InterlockedIncrement(&g_decalTextureCount);
    }
    ReleaseSRWLockExclusive(&g_decalLock);
    return true;
}

void ForgetDecalTexture(void* texture) {
    if (!g_decalTextureCount) return;
    AcquireSRWLockExclusive(&g_decalLock);
    if (g_decalTextures && g_decalTextures->erase(texture)) InterlockedDecrement(&g_decalTextureCount);
    ReleaseSRWLockExclusive(&g_decalLock);
}

void DecalTagEndFrame() {
    if (!g_decalTag) return;
    IDirect3DBaseTexture9* texture = nullptr;
    AcquireSRWLockExclusive(&g_decalLock);
    if (!g_decalPending.empty()) { texture = g_decalPending.back(); g_decalPending.pop_back(); }
    ReleaseSRWLockExclusive(&g_decalLock);
    LARGE_INTEGER now, frequency;
    QueryPerformanceFrequency(&frequency);
    if (texture) {
        LARGE_INTEGER t0;
        QueryPerformanceCounter(&t0);
        uint64_t hash = 0;
        D3DSURFACE_DESC desc = {};
        const bool hashed = RemixTextureHash(texture, &hash, &desc);
        QueryPerformanceCounter(&now);
        AcquireSRWLockExclusive(&g_decalLock);
        if (hashed && g_decalTextures) {
            auto it = g_decalTextures->find(texture);
            if (it != g_decalTextures->end()) it->second.hash = hash;
            if (std::find(g_decalHashes.begin(), g_decalHashes.end(), hash) == g_decalHashes.end()) {
                g_decalHashes.push_back(hash);
                g_decalListDirty = true;
            }
        }
        ReleaseSRWLockExclusive(&g_decalLock);
        WriteLog("DecalTag: texture %p %ux%u format %08lX pool %u -> %s 0x%016llX in %.2f ms", texture, desc.Width,
                 desc.Height, static_cast<unsigned long>(desc.Format), static_cast<unsigned>(desc.Pool),
                 hashed ? "hash" : "NOT HASHED", hash, (now.QuadPart - t0.QuadPart) * 1000.0 / frequency.QuadPart);
        texture->Release();
    } else {
        QueryPerformanceCounter(&now);
    }
    if (!g_decalListDirty || now.QuadPart - g_decalLastSend < frequency.QuadPart) return;
    std::string list;
    unsigned totalHashes = 0;
    AcquireSRWLockExclusive(&g_decalLock);
    for (const uint64_t h : g_decalHashes) {
        char item[24];
        sprintf_s(item, "%s0x%016llX", list.empty() ? "" : ", ", static_cast<unsigned long long>(h));
        list += item;
    }
    totalHashes = static_cast<unsigned>(g_decalHashes.size());
    g_decalListDirty = false;
    ReleaseSRWLockExclusive(&g_decalLock);
    g_decalLastSend = now.QuadPart;
    const bool sent = lightpub::SetRemixConfig("rtx.decalTextures", list.c_str());
    WriteLog("DecalTag: sent %u decal texture hashes to Remix (%s)", totalHashes,
             sent ? "ok" : "Remix API not ready, will retry");
    if (!sent) g_decalListDirty = true;
}

void SetParticleTagEnabled(bool enabled, bool logChanges) {
    if (enabled != g_particleTag) {
        if (logChanges) WriteLog("RaytraceParticles: particle texture auto-tagging now %d", enabled ? 1 : 0);
        AcquireSRWLockExclusive(&g_particleLock);
        if (enabled) {
            g_particleListDirty = !g_particleHashes.empty();
            g_particleClearPending = false;
        } else {
            g_particleClearPending = true;
            g_particleListDirty = false;
        }
        ReleaseSRWLockExclusive(&g_particleLock);
    }
    g_particleTag = enabled;
}

void ParticleTagBeforeDraw(IDirect3DBaseTexture9* stage0Texture, bool isParticleDraw, bool gogglesActive) {
    if (!g_particleTag || !isParticleDraw || !stage0Texture || !g_textureReleaseHooked || gogglesActive) return;
    AcquireSRWLockExclusive(&g_particleLock);
    if (!g_particleTextures) g_particleTextures = new std::unordered_map<void*, uint64_t>();
    if (g_particleTextures->emplace(stage0Texture, 0ull).second) {
        stage0Texture->AddRef();
        g_particlePending.push_back(stage0Texture);
        InterlockedIncrement(&g_particleTextureCount);
    }
    ReleaseSRWLockExclusive(&g_particleLock);
}

void ForgetParticleTexture(void* texture) {
    if (!g_particleTextureCount) return;
    AcquireSRWLockExclusive(&g_particleLock);
    if (g_particleTextures && g_particleTextures->erase(texture)) InterlockedDecrement(&g_particleTextureCount);
    ReleaseSRWLockExclusive(&g_particleLock);
}

void ParticleTagEndFrame() {
    if (g_particleClearPending) {
        if (lightpub::SetRemixConfig("rtx.particleTextures", "")) {
            g_particleClearPending = false;
        }
        return;
    }
    if (!g_particleTag) return;
    IDirect3DBaseTexture9* texture = nullptr;
    AcquireSRWLockExclusive(&g_particleLock);
    if (!g_particlePending.empty()) { texture = g_particlePending.back(); g_particlePending.pop_back(); }
    ReleaseSRWLockExclusive(&g_particleLock);
    LARGE_INTEGER now, frequency;
    QueryPerformanceFrequency(&frequency);
    if (texture) {
        uint64_t hash = 0;
        D3DSURFACE_DESC desc = {};
        t_texHashing = true;
        const bool hashed = RemixTextureHash(texture, &hash, &desc);
        t_texHashing = false;
        AcquireSRWLockExclusive(&g_particleLock);
        if (hashed && g_particleTextures) {
            auto it = g_particleTextures->find(texture);
            if (it != g_particleTextures->end()) it->second = hash;
            if (std::find(g_particleHashes.begin(), g_particleHashes.end(), hash) == g_particleHashes.end()) {
                g_particleHashes.push_back(hash);
                g_particleListDirty = true;
            }
        }
        ReleaseSRWLockExclusive(&g_particleLock);
        texture->Release();
    }
    QueryPerformanceCounter(&now);
    if (!g_particleListDirty || now.QuadPart - g_particleLastSend < frequency.QuadPart / 4) return;
    std::string list;
    unsigned totalHashes = 0;
    AcquireSRWLockExclusive(&g_particleLock);
    for (const uint64_t h : g_particleHashes) {
        char item[24];
        sprintf_s(item, "%s0x%016llX", list.empty() ? "" : ", ", static_cast<unsigned long long>(h));
        list += item;
    }
    totalHashes = static_cast<unsigned>(g_particleHashes.size());
    g_particleListDirty = false;
    ReleaseSRWLockExclusive(&g_particleLock);
    g_particleLastSend = now.QuadPart;
    const bool sent = lightpub::SetRemixConfig("rtx.particleTextures", list.c_str());
    WriteLog("RaytraceParticles: sent %u particle texture hashes to Remix (%s)", totalHashes, sent ? "ok" : "will retry");
    if (!sent) g_particleListDirty = true;
}

void OnTextureUnlockRect(IDirect3DTexture9* texture, UINT level, HRESULT unlockResult) {
    if (g_texHashLog && level == 0 && SUCCEEDED(unlockResult) && !t_texHashing) {
        texture->AddRef();
        AcquireSRWLockExclusive(&g_texHashLock);
        g_texHashPending.push_back(texture);
        ReleaseSRWLockExclusive(&g_texHashLock);
    }
}

void TexHashLogEndFrame(LONG presentCount) {
    static HANDLE file = INVALID_HANDLE_VALUE;
    if (!g_texHashLog) return;
    LARGE_INTEGER frequency, start, now;
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&start);
    for (now = start; now.QuadPart - start.QuadPart < frequency.QuadPart / 500; QueryPerformanceCounter(&now)) {
        IDirect3DTexture9* texture = nullptr;
        AcquireSRWLockExclusive(&g_texHashLock);
        if (!g_texHashPending.empty()) { texture = g_texHashPending.front(); g_texHashPending.pop_front(); }
        ReleaseSRWLockExclusive(&g_texHashLock);
        if (!texture) break;
        uint64_t hash = 0;
        D3DSURFACE_DESC desc = {};
        t_texHashing = true;
        const bool hashed = RemixTextureHash(texture, &hash, &desc);
        t_texHashing = false;
        if (hashed) {
            if (file == INVALID_HANDLE_VALUE) {
                wchar_t path[MAX_PATH];
                DumpFolder(L"texhash", path, ARRAYSIZE(path));
                wcscat_s(path, L"\\texhash.tsv");
                file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
            }
            if (file != INVALID_HANDLE_VALUE) {
                char line[160];
                const int length = sprintf_s(line, "%ld\t%p\t%u\t%u\t%08lX\t%lu\t%016llX\n", presentCount, texture,
                                             desc.Width, desc.Height, static_cast<unsigned long>(desc.Format),
                                             texture->GetLevelCount(), hash);
                DWORD written = 0;
                WriteFile(file, line, static_cast<DWORD>(length), &written, nullptr);
            }
        }
        texture->Release();
    }
    if (!(presentCount % 600)) {
        AcquireSRWLockShared(&g_texHashLock);
        WriteLog("TexHashLog: %zu textures waiting to be hashed", g_texHashPending.size());
        ReleaseSRWLockShared(&g_texHashLock);
    }
}

void ApplyStreamBudget(LONG presentCount) {
    if (ReplayMode()) return;
    static uint32_t lastLogged = 0;
    constexpr uint32_t kGameBudget = 40u << 20;
    const uint32_t wanted = g_streamBudgetMB > 0 ? static_cast<uint32_t>(g_streamBudgetMB) << 20 : kGameBudget;
    uint32_t current = 0, used = 0, usedAndPending = 0;
    uint32_t* budget = nullptr;
    __try {
        const uintptr_t database = *reinterpret_cast<const uintptr_t*>(0x032F5A58);
        const uintptr_t streamer = database ? *reinterpret_cast<const uintptr_t*>(database + 0xC) : 0;
        if (!streamer) return;
        budget = reinterpret_cast<uint32_t*>(streamer + 0x9C);
        current = *budget;
        used = *reinterpret_cast<const uint32_t*>(streamer + 0x98);
        usedAndPending = *reinterpret_cast<const uint32_t*>(streamer + 0xA0);
        if (current != wanted && (current == kGameBudget || current == lastLogged)) *budget = wanted;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return;
    }
    if (current != wanted && lastLogged != wanted) {
        WriteLog("TextureStreamBudget: streamer budget %u MB -> %u MB%s", current >> 20, wanted >> 20,
                 (current == kGameBudget || current == lastLogged) ? "" : " (NOT written: unexpected current value)");
        lastLogged = wanted;
    }
    if (!(presentCount % 600))
        WriteLog("TextureStreamBudget: budget %u MB, loaded %u MB, loaded+waiting %u MB", current >> 20, used >> 20,
                 usedAndPending >> 20);
}

void LogDecalStatsPeriodic(LONG presentCount) {
    if (!(presentCount % 600) && (g_decalTag || g_hideDecals)) {
        AcquireSRWLockShared(&g_decalLock);
        const unsigned sentHashes = static_cast<unsigned>(g_decalHashes.size());
        ReleaseSRWLockShared(&g_decalLock);
        WriteLog("DecalTag: last 600 frames %ld decal draws (%ld sent to ray tracing), %ld decal textures known, "
                 "%u hashes sent, %ld shared with ordinary objects (total), HideDecals %d",
                 InterlockedExchange(&g_statDecalDraws, 0), InterlockedExchange(&g_statDecalTraced, 0),
                 g_decalTextureCount, sentHashes, g_statDecalShared,
                 g_hideDecals ? 1 : 0);
    }
}

}  // namespace proxy
