#include "draw_filter.h"

#include "decal_tag.h"
#include "handoff_renderer.h"
#include "lightpub.h"
#include "proxy_diagnostics.h"
#include "proxy_log.h"
#include "query_probe.h"
#include "shader_traits.h"

#include <psapi.h>

#include <algorithm>
#include <cwchar>
#include <new>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_map>
#include <vector>

namespace proxy {
namespace {

FilterConfig g_cfg = {};
FilterOriginals g_orig = {};

PassGraph g_passGraph;
SetterCoalescer g_setterCoalescer{true};

IDirect3DDevice9* g_shaderCacheDevice = nullptr;
IDirect3DVertexShader9* g_cachedVs = nullptr;
bool g_cachedVsKnown = false;
IDirect3DPixelShader9* g_cachedPs = nullptr;
bool g_cachedPsKnown = false;
LONG g_shaderCacheMismatches = 0;
ShaderTraits g_cachedVsTraits = {};
ShaderTraits g_cachedPsTraits = {};
SRWLOCK g_shaderTraitsLock = SRWLOCK_INIT;
std::unordered_map<void*, ShaderTraits> g_shaderTraits;

constexpr DWORD kTrackedStages = 8;
SRWLOCK g_rtTextureLock = SRWLOCK_INIT;
std::unordered_map<void*, bool>* g_rtTextures = nullptr;
bool g_stageIsRt[kTrackedStages] = {};
IDirect3DBaseTexture9* g_stage0Texture = nullptr;
bool g_drawIsDecal = false;
bool g_rt0IsBackbuffer = true;
IDirect3DSurface9* g_backbuffer = nullptr;
UINT g_backbufferWidth = 0, g_backbufferHeight = 0;
IDirect3DSurface9* g_rt0 = nullptr;
bool g_rt0Primary = false;
D3DFORMAT g_rt0Format = D3DFMT_UNKNOWN;
bool g_rt1Bound = false;
bool g_rt0IsShadowMap = false;
struct CachedSurfaceDesc {
    const IDirect3DSurface9* surface = nullptr;
    UINT width = 0;
    UINT height = 0;
    D3DFORMAT format = D3DFMT_UNKNOWN;
    bool valid = false;
};
constexpr size_t kSurfaceCacheSlots = 64;
CachedSurfaceDesc g_surfaceDescCache[kSurfaceCacheSlots] = {};

bool GetCachedSurfaceDesc(IDirect3DSurface9* surface, D3DSURFACE_DESC* out) {
    if (!surface) return false;
    const size_t slot = (reinterpret_cast<uintptr_t>(surface) >> 4) & (kSurfaceCacheSlots - 1);
    CachedSurfaceDesc& entry = g_surfaceDescCache[slot];
    if (entry.surface == surface && entry.valid) {
        out->Width = entry.width;
        out->Height = entry.height;
        out->Format = entry.format;
        return true;
    }
    if (FAILED(surface->GetDesc(out))) return false;
    entry = {surface, out->Width, out->Height, out->Format, true};
    return true;
}
const void* g_vb0 = nullptr;
UINT g_vb0Offset = 0, g_vb0Stride = 0;
const void* g_ib = nullptr;

LONG g_postFxSkippedDraws = 0;
LONG g_postFxSkippedCopies = 0;
LONG g_backbufferBinds = 0;
LONG g_shadowMapSkipped = 0;
LONG g_lightingSkipped = 0;
bool g_drawIsWasted = false;
bool g_drawIsScene = false;
bool g_shScaleReadThisFrame = false;

int g_texTransformApplied = -1;
LONG g_packedUvDraws = 0;
std::vector<uint32_t> g_skipVs;
std::vector<uint32_t> g_skipVsAdditive;
std::vector<uint32_t> g_menuVs;
IDirect3DDevice9* g_skipVsDevice = nullptr;
LONG g_menuDrawsThisFrame = 0;

int g_rsZEnable = -1, g_rsColorWrite = -1, g_rsBlend = -1, g_rsZWrite = -1;
LONG g_rsMismatches = 0;
LONG g_alphaTestHinted = 0, g_alphaTestLeft = 0;
unsigned g_rsCheckZEnable = 0, g_rsCheckColorWrite = 0, g_rsCheckBlend = 0, g_rsCheckZWrite = 0;
IDirect3DSurface9* g_gbufferDepthRt0 = nullptr;
std::string g_lastGateDumpTag;
std::string g_pendingGateDumpTag;
bool g_gateDepthDumpedThisFrame = false;
uint64_t g_prevHandoffsEmitted = 0;
bool g_modernAwaitingComposite = false;
GateFrameMetrics g_lastGateMetrics = {};
bool g_skipCurrentDrawForModern = false;
bool g_drawIsParticle = false;

float HalfToFloat32(uint16_t h) {
    const uint32_t sign = static_cast<uint32_t>(h & 0x8000u) << 16;
    const uint32_t exp = (h >> 10) & 0x1Fu;
    const uint32_t mant = h & 0x03FFu;
    uint32_t bits = 0;
    if (exp == 0) {
        if (mant != 0) {
            uint32_t m = mant;
            uint32_t e = 127 - 14;
            while ((m & 0x0400u) == 0) { m <<= 1; --e; }
            bits = sign | (e << 23) | ((m & 0x03FFu) << 13);
        } else {
            bits = sign;
        }
    } else if (exp == 31) {
        bits = sign | 0x7F800000u | (mant ? (0x00400000u | (mant << 13)) : 0u);
    } else {
        bits = sign | ((exp + (127 - 15)) << 23) | (mant << 13);
    }
    float out = 0.0f;
    std::memcpy(&out, &bits, sizeof(out));
    return out;
}

void MaybeDumpGameDepthAfterGBuffer(IDirect3DDevice9* device) {
    if (g_pendingGateDumpTag.empty() || g_gateDepthDumpedThisFrame || !g_gbufferDepthRt0) return;
    D3DSURFACE_DESC desc = {};
    if (FAILED(g_gbufferDepthRt0->GetDesc(&desc)) || desc.Format != D3DFMT_A16B16G16R16F ||
        !desc.Width || !desc.Height) {
        return;
    }
    constexpr UINT kDumpW = 320, kDumpH = 180;
    const UINT w = (desc.Width > kDumpW) ? kDumpW : desc.Width;
    const UINT h = (desc.Height > kDumpH) ? kDumpH : desc.Height;
    IDirect3DSurface9* srcSurf = g_gbufferDepthRt0;
    IDirect3DSurface9* smallRt = nullptr;
    if (w != desc.Width || h != desc.Height) {
        if (FAILED(device->CreateRenderTarget(w, h, desc.Format, D3DMULTISAMPLE_NONE, 0, FALSE, &smallRt, nullptr)) ||
            !smallRt) {
            return;
        }
        if (FAILED(device->StretchRect(g_gbufferDepthRt0, nullptr, smallRt, nullptr, D3DTEXF_POINT))) {
            smallRt->Release();
            return;
        }
        srcSurf = smallRt;
    }
    IDirect3DSurface9* sysSurf = nullptr;
    if (SUCCEEDED(device->CreateOffscreenPlainSurface(w, h, desc.Format, D3DPOOL_SYSTEMMEM, &sysSurf, nullptr)) &&
        sysSurf) {
        if (SUCCEEDED(device->GetRenderTargetData(srcSurf, sysSurf))) {
            D3DLOCKED_RECT locked = {};
            if (SUCCEEDED(sysSurf->LockRect(&locked, nullptr, D3DLOCK_READONLY)) && locked.pBits) {
                std::vector<float> linearDepth(static_cast<size_t>(w) * h);
                for (UINT y = 0; y < h; ++y) {
                    const auto* row = reinterpret_cast<const uint16_t*>(
                        static_cast<const uint8_t*>(locked.pBits) + static_cast<size_t>(y) * locked.Pitch);
                    float* dst = linearDepth.data() + static_cast<size_t>(y) * w;
                    for (UINT x = 0; x < w; ++x) {
                        const float d = HalfToFloat32(row[x * 4 + 0]);
                        dst[x] = (std::isfinite(d) && d > 0.0f && d < 30000.0f) ? d : INFINITY;
                    }
                }
                sysSurf->UnlockRect();

                wchar_t gateDir[MAX_PATH] = {}, finalPath[MAX_PATH] = {}, tmpPath[MAX_PATH] = {};
                DumpFolder(L"gate", gateDir, ARRAYSIZE(gateDir));
                wchar_t wtag[128] = {};
                MultiByteToWideChar(CP_UTF8, 0, g_pendingGateDumpTag.c_str(), -1, wtag, ARRAYSIZE(wtag));
                swprintf_s(finalPath, L"%s\\%s_gamedepth.pfm", gateDir, wtag);
                swprintf_s(tmpPath, L"%s.tmp", finalPath);
                FILE* f = nullptr;
                if (_wfopen_s(&f, tmpPath, L"wb") == 0 && f) {
                    std::fprintf(f, "Pf\n%u %u\n-1.0\n", w, h);
                    for (UINT row = 0; row < h; ++row) {
                        const UINT srcRow = h - 1u - row;
                        std::fwrite(linearDepth.data() + static_cast<size_t>(srcRow) * w, sizeof(float), w, f);
                    }
                    std::fclose(f);
                    MoveFileExW(tmpPath, finalPath, MOVEFILE_REPLACE_EXISTING);
                }
                lightpub::SetRemixConfig("rtx.gateDumpTag", g_pendingGateDumpTag.c_str());
                WriteLog("GateDump: wrote %ls (%ux%u) and requested Remix dump '%s'",
                         finalPath, w, h, g_pendingGateDumpTag.c_str());
                g_gateDepthDumpedThisFrame = true;
                g_pendingGateDumpTag.clear();
            }
        }
        sysSurf->Release();
    }
    if (smallRt) smallRt->Release();
}

bool ShaderCacheUsable(IDirect3DDevice9* device) {
    return g_orig.shaderHooksInstalled && g_shaderCacheDevice == device;
}

template <typename Shader>
ShaderTraits TraitsOf(Shader* shader, bool vertex) {
    if (!shader) {
        return ShaderTraits{0, vertex ? VsClass::Other : VsClass::None};
    }
    AcquireSRWLockShared(&g_shaderTraitsLock);
    const auto found = g_shaderTraits.find(shader);
    const bool known = found != g_shaderTraits.end();
    ShaderTraits traits = known
        ? found->second
        : ShaderTraits{0, vertex ? VsClass::Other : VsClass::None};
    ReleaseSRWLockShared(&g_shaderTraitsLock);
    if (known) return traits;
    UINT size = 0;
    if (SUCCEEDED(shader->GetFunction(nullptr, &size)) && size >= 8 && size < (1u << 20)) {
        std::vector<uint32_t> code((size + 3) / 4);
        if (SUCCEEDED(shader->GetFunction(code.data(), &size))) {
            traits = AnalyzeShaderBytecode(code.data(), size, vertex);
        }
    }
    if (g_orig.hookShaderRelease) g_orig.hookShaderRelease(shader, vertex);
    AcquireSRWLockExclusive(&g_shaderTraitsLock);
    g_shaderTraits[shader] = traits;
    ReleaseSRWLockExclusive(&g_shaderTraitsLock);
    return traits;
}

ShaderTraits LookupPixelShaderTraits(IDirect3DPixelShader9* ps) {
    return TraitsOf(ps, false);
}

bool IsRenderTargetTexture(void* texture) {
    if (!texture) return false;
    AcquireSRWLockShared(&g_rtTextureLock);
    const bool found = g_rtTextures && g_rtTextures->find(texture) != g_rtTextures->end();
    ReleaseSRWLockShared(&g_rtTextureLock);
    return found;
}

bool ShouldSuppressSettersNow() {
    return !IsCaptureDrawsActive() &&
           g_passGraph.ShouldSuppressSetters(
               g_rt0,
               g_rt0Primary,
               g_rt0IsShadowMap,
               g_rt0Format,
               g_rt1Bound,
               lightpub::RaytracingOn(),
               g_cachedVsKnown ? g_cachedVsTraits.vsClass : VsClass::Other,
               g_cachedPsKnown && g_cachedPsTraits.composite);
}

bool StateEnabled(IDirect3DDevice9* device, D3DRENDERSTATETYPE state, int* cache, unsigned* checks) {
    if (*cache < 0 || !g_orig.setRenderState || (!g_setterCoalescer.HasDirtyState() && !(++(*checks) & 255))) {
        FlushCoalescedSetters(device);
        DWORD real = 0;
        if (SUCCEEDED(device->GetRenderState(state, &real))) {
            const int value = real ? 1 : 0;
            if (*cache >= 0 && *cache != value) InterlockedIncrement(&g_rsMismatches);
            *cache = value;
        }
    }
    return *cache != 0;
}

void ParseHashList(const wchar_t* key, std::vector<uint32_t>* out) {
    wchar_t text[1024] = {};
    GetPrivateProfileStringW(L"Proxy", key, L"", text, ARRAYSIZE(text), IniPath());
    out->clear();
    for (wchar_t* p = text; *p;) {
        wchar_t* end = nullptr;
        const unsigned long v = wcstoul(p, &end, 16);
        if (end == p) { ++p; continue; }
        out->push_back(static_cast<uint32_t>(v));
        p = end;
    }
}

bool SkipThisVs() {
    if ((g_skipVs.empty() && g_skipVsAdditive.empty() && g_menuVs.empty()) || !g_skipVsDevice) return false;
    uint32_t h = 0;
    if (g_cachedVsKnown && ShaderCacheUsable(g_skipVsDevice)) {
        h = g_cachedVsTraits.hash;
    } else {
        IDirect3DVertexShader9* vs = nullptr;
        if (FAILED(g_skipVsDevice->GetVertexShader(&vs)) || !vs) return false;
        h = TraitsOf(vs, true).hash;
        vs->Release();
    }
    if (std::find(g_menuVs.begin(), g_menuVs.end(), h) != g_menuVs.end()) ++g_menuDrawsThisFrame;
    if (std::find(g_skipVs.begin(), g_skipVs.end(), h) != g_skipVs.end()) return true;
    if (std::find(g_skipVsAdditive.begin(), g_skipVsAdditive.end(), h) == g_skipVsAdditive.end()) return false;
    DWORD blend = 0, dst = 0;
    g_skipVsDevice->GetRenderState(D3DRS_ALPHABLENDENABLE, &blend);
    g_skipVsDevice->GetRenderState(D3DRS_DESTBLEND, &dst);
    return blend && dst == D3DBLEND_ONE;
}

bool SkipPostFxDraw() {
    if (g_skipCurrentDrawForModern) return true;
    if (IsHideDecalsEnabled() && g_drawIsDecal) return true;
    if (g_cfg.passFilter) {
        if (g_drawIsWasted) {
            if (g_rt0IsShadowMap) InterlockedIncrement(&g_shadowMapSkipped);
            else InterlockedIncrement(&g_lightingSkipped);
        }
        return g_drawIsWasted;
    }
    if (g_cfg.skipShadowMapDraws && g_rt0IsShadowMap) {
        InterlockedIncrement(&g_shadowMapSkipped);
        return true;
    }
    if (SkipThisVs()) return true;
    if (!g_rt0IsBackbuffer || !lightpub::HidePostFx()) return false;
    for (DWORD i = 0; i < kTrackedStages; ++i)
        if (g_stageIsRt[i]) return true;
    return false;
}

void ApplyPackedTexcoordTransform(IDirect3DDevice9* device, DeviceHook* hook) {
    if (!g_cfg.packedTexcoordFix || !hook->setTransform) return;
    bool packed = false;
    if (g_cachedVsKnown && ShaderCacheUsable(device)) {
        packed = g_cachedVsTraits.usesPackedUv;
    } else {
        IDirect3DVertexShader9* vs = nullptr;
        if (FAILED(device->GetVertexShader(&vs))) return;
        packed = vs && TraitsOf(vs, true).usesPackedUv;
        if (vs) vs->Release();
    }
    const int want = packed ? 1 : 0;
    if (want) InterlockedIncrement(&g_packedUvDraws);
    if (want == g_texTransformApplied) return;
    D3DMATRIX m = {};
    const float s = want ? 1.0f / 2048.0f : 1.0f;
    m.m[0][0] = s; m.m[1][1] = s; m.m[2][2] = 1.0f; m.m[3][3] = 1.0f;
    hook->setTransform(device, D3DTS_TEXTURE0, &m);
    device->SetTextureStageState(0, D3DTSS_TEXTURETRANSFORMFLAGS, want ? D3DTTFF_COUNT2 : D3DTTFF_DISABLE);
    g_texTransformApplied = want;
}

void PassFilterBeforeDraw(IDirect3DDevice9* device,
                          DeviceHook* hook,
                          const CameraFrameTracker& cameraTracker,
                          const DrawGeometryKey* geometry) {
    g_drawIsScene = g_drawIsWasted = g_skipCurrentDrawForModern = g_drawIsParticle = false;
    if (!g_cfg.passFilter) {
        SetBracket(device, false);
        return;
    }
    const bool cacheUsable = ShaderCacheUsable(device);
    static LONG drawsSinceCheck = 0;
    if (cacheUsable && g_cachedVsKnown && g_cachedPsKnown && !g_setterCoalescer.HasDirtyState() && ++drawsSinceCheck >= 600) {
        drawsSinceCheck = 0;
        IDirect3DVertexShader9* realVs = nullptr;
        IDirect3DPixelShader9* realPs = nullptr;
        device->GetVertexShader(&realVs);
        device->GetPixelShader(&realPs);
        if (realVs != g_cachedVs || realPs != g_cachedPs) {
            g_cachedVsKnown = g_cachedPsKnown = false;
            InterlockedIncrement(&g_shaderCacheMismatches);
        }
        if (realVs) realVs->Release();
        if (realPs) realPs->Release();
    }
    ShaderTraits vertex = {}, pixel = {};
    if (cacheUsable && g_cachedVsKnown && g_cachedPsKnown) {
        vertex = g_cachedVsTraits;
        pixel = g_cachedPsTraits;
    } else {
        FlushCoalescedSetters(device);
        IDirect3DVertexShader9* vs = nullptr;
        IDirect3DPixelShader9* ps = nullptr;
        device->GetVertexShader(&vs);
        device->GetPixelShader(&ps);
        vertex = TraitsOf(vs, true);
        pixel = TraitsOf(ps, false);
        if (g_orig.shaderHooksInstalled) {
            g_shaderCacheDevice = device;
            g_cachedVs = vs; g_cachedVsTraits = vertex; g_cachedVsKnown = true;
            g_cachedPs = ps; g_cachedPsTraits = pixel; g_cachedPsKnown = true;
        }
        if (vs) vs->Release();
        if (ps) ps->Release();
    }

    if (!g_rt1Bound && g_gbufferDepthRt0) {
        MaybeDumpGameDepthAfterGBuffer(device);
    }
    if (vertex.shScale && !g_shScaleReadThisFrame) {
        float value[4] = {};
        if (SUCCEEDED(device->GetVertexShaderConstantF(vertex.shScale - 1u, value, 1))) lightpub::SetShScale(value[0]);
        g_shScaleReadThisFrame = true;
    }

    BOOL gogglesSw = FALSE;
    if (g_cfg.gogglesGameView && !g_passGraph.GogglesDetectedThisFrame() && !pixel.thermal && pixel.gogglesSwitch) {
        if (FAILED(device->GetPixelShaderConstantB(pixel.gogglesSwitch - 1, &gogglesSw, 1))) gogglesSw = FALSE;
    }

    DrawContextInput ctx = {};
    ctx.rt0 = g_rt0;
    ctx.rt0Primary = g_rt0Primary;
    ctx.rt0IsShadowMap = g_rt0IsShadowMap;
    ctx.rt0Format = g_rt0Format;
    ctx.rt1Bound = g_rt1Bound;
    ctx.stage0ReadsSceneHdr = ReadsSceneHdr(g_stage0Texture);
    ctx.depthTestEnabled = StateEnabled(device, D3DRS_ZENABLE, &g_rsZEnable, &g_rsCheckZEnable);
    ctx.depthWriteEnabled = StateEnabled(device, D3DRS_ZWRITEENABLE, &g_rsZWrite, &g_rsCheckZWrite);
    ctx.colorWriteEnabled = StateEnabled(device, D3DRS_COLORWRITEENABLE, &g_rsColorWrite, &g_rsCheckColorWrite);
    ctx.alphaBlendEnabled = StateEnabled(device, D3DRS_ALPHABLENDENABLE, &g_rsBlend, &g_rsCheckBlend);
    ctx.gogglesSwitchActive = gogglesSw != FALSE;
    ctx.raytracingActive = lightpub::RaytracingOn();
    ctx.vs = vertex;
    ctx.ps = pixel;
    ctx.geometry = geometry;
    g_drawIsParticle = vertex.particle || pixel.particle;

    const DrawDecision dec = g_passGraph.ClassifyDraw(ctx);
    if (dec.rememberSceneHdrTarget) RememberSceneHdr(g_rt0);
    if (g_rt1Bound && g_rt0Primary && g_rt0Format == D3DFMT_A16B16G16R16F && g_rt0 != g_gbufferDepthRt0) {
        if (g_gbufferDepthRt0) g_gbufferDepthRt0->Release();
        g_gbufferDepthRt0 = g_rt0;
        if (g_gbufferDepthRt0) g_gbufferDepthRt0->AddRef();
    }
    if (dec.emitCompositeHandoffBeforeDraw) {
        EmitHandoffMarker(device, hook, g_passGraph, cameraTracker, g_rt0, &FlushCoalescedSetters);
    }
    if (g_passGraph.MarkerDoneThisFrame()) {
        if (g_modernAwaitingComposite) {
            if (pixel.composite && g_rt0) {
                FlushCoalescedSetters(device);
                PresentModernSceneToComposite(device, hook, g_rt0);
                g_modernAwaitingComposite = false;
                g_skipCurrentDrawForModern = true;
                return;
            }
            if (g_rt0 != CurrentSceneHdrTarget() || ctx.stage0ReadsSceneHdr) {
                g_skipCurrentDrawForModern = true;
                return;
            }
            // SkipBracketDraws (query_probe.h, X2): a draw onto the scene HDR target over Remix's picture, before the
            // composite. Every draw after the hand-off comes through this branch.
            if (SkipAfterHandoffClass(g_skipBracketDraws, g_cfg.passFilter, g_passGraph.MarkerDoneThisFrame(),
                                      g_modernAwaitingComposite, pixel.composite, g_rt0 == CurrentSceneHdrTarget(),
                                      ctx.stage0ReadsSceneHdr)) {
                QueryProbeSkip(1);
                g_skipCurrentDrawForModern = true;
                return;
            }
        }
        g_drawIsWasted = dec.skipDraw;
        return;
    }

    if (dec.tryEarlyHandoffBeforeDraw &&
        HandoffBeforePost(device, hook, g_passGraph, cameraTracker, g_rt0,
                          g_backbufferWidth, g_backbufferHeight, &FlushCoalescedSetters)) {
        if (g_cfg.modernFilter && !g_cfg.raytraceParticles) {
            g_modernAwaitingComposite = true;
        }
        return;
    }
    if (dec.latchCompositeTarget) LatchCompositeTarget(g_rt0);

    if (vertex.skinned) {
        RecordSkinnedDraw(dec.stage, dec.isScene, g_rt1Bound, ctx.alphaBlendEnabled, pixel, geometry);
    }
    g_drawIsScene = dec.isScene;
    g_drawIsWasted = dec.skipDraw;
    SetBracket(device, dec.openBracket);
}

}  // namespace

void SetFilterConfig(const FilterConfig& cfg) {
    g_cfg = cfg;
    PassGraphConfig pg = {};
    pg.enabled = cfg.passFilter;
    pg.forwardScene = cfg.forwardScene;
    pg.skipShadowMapDraws = cfg.skipShadowMapDraws;
    pg.skipGameLighting = cfg.skipGameLighting;
    pg.skipGamePost = cfg.skipGamePost;
    pg.gogglesGameView = cfg.gogglesGameView;
    pg.handoffBeforePost = cfg.modernFilter ? (!cfg.raytraceParticles) : cfg.handoffBeforePost;
    pg.handoffBeforeEffects = cfg.modernFilter ? (!cfg.raytraceParticles) : cfg.handoffBeforeEffects;
    pg.modernFilter = cfg.modernFilter;
    pg.raytraceParticles = cfg.raytraceParticles;
    g_passGraph.SetConfig(pg);
}

void SetFilterOriginals(const FilterOriginals& originals) { g_orig = originals; }

void ReloadSkipVsLists() {
    ParseHashList(L"SkipVs", &g_skipVs);
    ParseHashList(L"SkipVsAdditive", &g_skipVsAdditive);
    ParseHashList(L"MenuVs", &g_menuVs);
}
void SetGateDumpTag(const char* tag) {
    const std::string next = tag ? tag : "";
    if (next.empty()) {
        g_lastGateDumpTag.clear();
        g_pendingGateDumpTag.clear();
        return;
    }
    if (next != g_lastGateDumpTag) {
        g_lastGateDumpTag = next;
        g_pendingGateDumpTag = next;
    }
}

void RecordTextureUsage(void* texture, DWORD usage) {
    AcquireSRWLockExclusive(&g_rtTextureLock);
    if (!g_rtTextures) g_rtTextures = new (std::nothrow) std::unordered_map<void*, bool>();
    if (g_rtTextures) {
        if (usage & D3DUSAGE_RENDERTARGET) (*g_rtTextures)[texture] = true;
        else g_rtTextures->erase(texture);
    }
    ReleaseSRWLockExclusive(&g_rtTextureLock);
}

void ForgetShader(void* shader) {
    AcquireSRWLockExclusive(&g_shaderTraitsLock);
    g_shaderTraits.erase(shader);
    ReleaseSRWLockExclusive(&g_shaderTraitsLock);
    if (shader == g_cachedVs) g_cachedVsKnown = false;
    if (shader == g_cachedPs) g_cachedPsKnown = false;
}

IDirect3DSurface9* CurrentBackbuffer(IDirect3DDevice9* device) {
    if (!g_backbuffer) {
        IDirect3DSurface9* surface = nullptr;
        if (SUCCEEDED(device->GetBackBuffer(0, 0, D3DBACKBUFFER_TYPE_MONO, &surface)) && surface) {
            g_backbuffer = surface;
            D3DSURFACE_DESC desc = {};
            if (SUCCEEDED(surface->GetDesc(&desc))) {
                g_backbufferWidth = desc.Width;
                g_backbufferHeight = desc.Height;
            }
            surface->Release();
        }
    }
    return g_backbuffer;
}

void RefreshBackbuffer(IDirect3DDevice9* device) {
    g_backbuffer = nullptr;
    CurrentBackbuffer(device);
}

UINT BackbufferWidth() { return g_backbufferWidth; }
UINT BackbufferHeight() { return g_backbufferHeight; }

void FlushCoalescedSetters(IDirect3DDevice9* device) {
    if (!g_setterCoalescer.HasDirtyState()) return;
    const uint64_t flushed = g_setterCoalescer.Flush(
        device, g_orig.setTexture, g_orig.setRenderState, g_orig.setStreamSource,
        g_orig.setIndices, g_orig.setPixelShaderConstantF);
    g_passGraph.AddSetterStats(g_setterCoalescer.TakeSuppressedCount(), flushed);
}

void OnSetRenderTarget(IDirect3DDevice9* device, DWORD index, IDirect3DSurface9* surface) {
    if (index == 0) {
        g_rt0IsBackbuffer = surface && surface == CurrentBackbuffer(device);
        if (g_rt0IsBackbuffer) InterlockedIncrement(&g_backbufferBinds);
        D3DSURFACE_DESC desc = {};
        const bool described = GetCachedSurfaceDesc(surface, &desc);
        g_rt0IsShadowMap = described && !g_rt0IsBackbuffer && desc.Width == desc.Height && desc.Width >= 1024;
        g_rt0 = surface;
        g_rt0Format = described ? desc.Format : D3DFMT_UNKNOWN;
        g_rt0Primary = described && g_backbufferWidth && desc.Width == g_backbufferWidth &&
                       desc.Height == g_backbufferHeight;
    } else if (index == 1) {
        g_rt1Bound = surface != nullptr;
    }
    if (g_setterCoalescer.HasDirtyState() && !ShouldSuppressSettersNow()) {
        FlushCoalescedSetters(device);
    }
}

bool TrySuppressSetTexture(IDirect3DDevice9* device, DWORD stage, IDirect3DBaseTexture9* texture) {
    if (stage < kTrackedStages)
        g_stageIsRt[stage] = !g_cfg.passFilter && lightpub::HidePostFx() && IsRenderTargetTexture(texture);
    if (stage == 0) g_stage0Texture = texture;
    if (stage < SetterCoalescer::kMaxStages && ShouldSuppressSettersNow()) {
        g_setterCoalescer.RecordSetTexture(stage, texture);
        return true;
    }
    FlushCoalescedSetters(device);
    return false;
}

bool TrySuppressSetRenderState(IDirect3DDevice9* device, D3DRENDERSTATETYPE state, DWORD value) {
    if (state == D3DRS_ZENABLE) g_rsZEnable = value ? 1 : 0;
    else if (state == D3DRS_COLORWRITEENABLE) g_rsColorWrite = value ? 1 : 0;
    else if (state == D3DRS_ALPHABLENDENABLE) g_rsBlend = value ? 1 : 0;
    else if (state == D3DRS_ZWRITEENABLE) g_rsZWrite = value ? 1 : 0;
    if (static_cast<uint32_t>(state) < SetterCoalescer::kMaxRenderStates && ShouldSuppressSettersNow()) {
        g_setterCoalescer.RecordSetRenderState(state, value);
        return true;
    }
    FlushCoalescedSetters(device);
    return false;
}

bool TrySuppressSetStreamSource(IDirect3DDevice9* device, UINT stream, IDirect3DVertexBuffer9* vb, UINT offset, UINT stride) {
    if (stream == 0) { g_vb0 = vb; g_vb0Offset = offset; g_vb0Stride = stride; }
    if (stream == 0 && ShouldSuppressSettersNow()) {
        g_setterCoalescer.RecordSetStreamSource0(vb, offset, stride);
        return true;
    }
    FlushCoalescedSetters(device);
    return false;
}

bool TrySuppressSetIndices(IDirect3DDevice9* device, IDirect3DIndexBuffer9* ib) {
    g_ib = ib;
    if (ShouldSuppressSettersNow()) {
        g_setterCoalescer.RecordSetIndices(ib);
        return true;
    }
    FlushCoalescedSetters(device);
    return false;
}

bool TrySuppressSetPixelShaderConstantF(IDirect3DDevice9* device, UINT startReg, const float* data, UINT vec4Count) {
    if (data && vec4Count > 0 && startReg + vec4Count <= SetterCoalescer::kMaxPsFloatRegs &&
        ShouldSuppressSettersNow()) {
        g_setterCoalescer.RecordSetPixelShaderConstantF(startReg, data, vec4Count);
        return true;
    }
    FlushCoalescedSetters(device);
    return false;
}

void OnVertexShaderSet(IDirect3DDevice9* device, IDirect3DVertexShader9* shader) {
    g_shaderCacheDevice = device;
    g_cachedVs = shader;
    g_cachedVsTraits = shader ? TraitsOf(shader, true) : ShaderTraits{};
    g_cachedVsKnown = true;
}

void OnPixelShaderSet(IDirect3DDevice9* device, IDirect3DPixelShader9* shader) {
    g_shaderCacheDevice = device;
    g_cachedPs = shader;
    g_cachedPsTraits = shader ? TraitsOf(shader, false) : ShaderTraits{};
    g_cachedPsKnown = true;
}

void FilterOnDeviceReset() {
    g_setterCoalescer.DiscardAll();
    g_passGraph.OnDeviceReset();
    g_rsZEnable = g_rsColorWrite = g_rsBlend = g_rsZWrite = -1;
    g_vb0 = g_ib = nullptr;
    g_cachedVsKnown = g_cachedPsKnown = false;
    std::memset(g_surfaceDescCache, 0, sizeof(g_surfaceDescCache));
    if (g_gbufferDepthRt0) { g_gbufferDepthRt0->Release(); g_gbufferDepthRt0 = nullptr; }
    g_modernAwaitingComposite = false;
    g_gateDepthDumpedThisFrame = false;
}

DrawGeometryKey MakeDrawGeometry(bool indexed,
                                 INT baseVertex,
                                 UINT minIndex,
                                 UINT startLocation,
                                 UINT primitiveCount,
                                 D3DPRIMITIVETYPE primitiveType) {
    return DrawGeometryKey{
        g_vb0,
        g_vb0Offset,
        g_vb0Stride,
        indexed ? g_ib : nullptr,
        baseVertex,
        minIndex,
        startLocation,
        primitiveCount,
        static_cast<UINT>(primitiveType),
    };
}

bool PrepareDraw(IDirect3DDevice9* device,
                 DeviceHook* hook,
                 const CameraFrameTracker& cameraTracker,
                 const DrawGeometryKey* geometry,
                 const char* drawKind,
                 UINT vertexCount,
                 UINT primitiveCount,
                 void* caller,
                 LONG presentCount) {
    PassFilterBeforeDraw(device, hook, cameraTracker, geometry);
    g_drawIsDecal = DecalTagBeforeDraw(caller, g_stage0Texture, g_cfg.passFilter, g_drawIsScene, g_passGraph.GogglesActive());
    ParticleTagBeforeDraw(g_stage0Texture, g_drawIsScene && g_drawIsParticle, g_passGraph.GogglesActive());
    if (IsCaptureDrawsActive()) {
        DrawCaptureContext ctx = {};
        ctx.presentCount = presentCount;
        ctx.rt0IsBackbuffer = g_rt0IsBackbuffer;
        ctx.rt0IsShadowMap = g_rt0IsShadowMap;
        ctx.stageIsRt = g_stageIsRt;
        ctx.trackedStages = kTrackedStages;
        ctx.skipShadowMapDraws = g_cfg.skipShadowMapDraws;
        ctx.drawIsWasted = g_drawIsWasted;
        ctx.alphaTestFromTexkill = g_cfg.alphaTestFromTexkill;
        ctx.alphaTestRef = g_cfg.alphaTestRef;
        ctx.passFilter = g_cfg.passFilter;
        ctx.drawIsScene = g_drawIsScene;
        ctx.bracketOpen = IsBracketOpen();
        ctx.markerDone = g_passGraph.MarkerDoneThisFrame();
        ctx.drawIsDecal = g_drawIsDecal;
        ctx.skipVs = &g_skipVs;
        ctx.skipVsAdditive = &g_skipVsAdditive;
        ctx.menuVs = &g_menuVs;
        ctx.lookupPsTraits = &LookupPixelShaderTraits;
        CaptureDraw(device, drawKind, vertexCount, primitiveCount, caller, ctx);
    }
    if (SkipPostFxDraw()) {
        InterlockedIncrement(&g_postFxSkippedDraws);
        return false;
    }
    FlushCoalescedSetters(device);
    ApplyPackedTexcoordTransform(device, hook);
    return true;
}

void OnAfterUpDraw(bool clearIndexBuffer) {
    g_vb0 = nullptr;
    if (clearIndexBuffer) g_ib = nullptr;
}

void NotePostFxSkippedCopy() { InterlockedIncrement(&g_postFxSkippedCopies); }

bool ShouldShowGamePicture() {
    return (!g_cfg.passFilter && g_menuDrawsThisFrame > 0) || g_passGraph.GogglesActive();
}

bool IsGogglesActive() { return g_passGraph.GogglesActive(); }
GateFrameMetrics SnapshotGateFrameMetrics() { return g_lastGateMetrics; }

void FilterEndFrameBeforePresent(IDirect3DDevice9* device,
                                 DeviceHook* hook,
                                 const CameraFrameTracker& cameraTracker,
                                 LONG presentCount) {
    FlushCoalescedSetters(device);
    g_shScaleReadThisFrame = false;
    GateFrameMetrics m = {};
    m.sceneDraws = g_passGraph.SceneDrawsThisFrame();
    m.skinnedSceneDraws = g_passGraph.SkinnedSceneDrawsThisFrame();
    m.goggles = g_cfg.gogglesGameView && g_passGraph.GogglesActive();
    m.rtActive = lightpub::RaytracingOn();
    if (g_cfg.passFilter) {
        FinishSkinTraceFrame(presentCount);
        HandoffEndFrame(device, hook, g_passGraph, cameraTracker, g_rt0, m.rtActive);
    }
    const uint64_t curHandoffs = g_passGraph.Stats().handoffsEmitted;
    m.handoffs = static_cast<uint32_t>(curHandoffs - g_prevHandoffsEmitted);
    g_prevHandoffsEmitted = curHandoffs;
    g_lastGateMetrics = m;
    if (g_gbufferDepthRt0) { g_gbufferDepthRt0->Release(); g_gbufferDepthRt0 = nullptr; }
    g_modernAwaitingComposite = false;
    g_gateDepthDumpedThisFrame = false;
}

void FilterResetAfterPresent(IDirect3DDevice9* device) {
    g_texTransformApplied = -1;
    g_skipVsDevice = device;
    g_menuDrawsThisFrame = 0;
}

void LogFilterStatsPeriodic(LONG presentCount, volatile LONG* focusMessagesDropped) {
    LogDecalStatsPeriodic(presentCount);
    if (!(presentCount % 600) && g_cfg.passFilter) {
        static PassGraphStats prev = {};
        const PassGraphStats cur = g_passGraph.Stats();
        AcquireSRWLockShared(&g_shaderTraitsLock);
        const unsigned classifiedShaders = static_cast<unsigned>(g_shaderTraits.size());
        ReleaseSRWLockShared(&g_shaderTraitsLock);
        WriteLog("PassFilter: last 600 frames %llu with a 3D scene, %llu scene draws sent to ray tracing, %llu hand-offs "
                 "(%ld before post-processing), %llu composite-like draws before post-processing ignored, "
                 "%ld query brackets, depth-state mismatches %ld, shaders classified %u",
                 cur.sceneFrames - prev.sceneFrames, cur.sceneDraws - prev.sceneDraws,
                 cur.handoffsEmitted - prev.handoffsEmitted, TakeEarlyHandoffsCount(),
                 cur.compositesRejected - prev.compositesRejected, TakeBracketOpensCount(),
                 InterlockedExchange(&g_rsMismatches, 0), classifiedShaders);
        WriteLog("PassFilter: last 600 frames %llu forward-pass copies of G-buffer meshes and %llu particle draws kept "
                 "out of ray tracing; %llu setters suppressed (%llu coalesced flushes)",
                 cur.forwardDuplicatesBlocked - prev.forwardDuplicatesBlocked,
                 cur.particlesExcluded - prev.particlesExcluded,
                 cur.suppressedSetters - prev.suppressedSetters,
                 cur.flushedSetters - prev.flushedSetters);
        prev = cur;
    }
    if (!(presentCount % 600)) {
        MEMORYSTATUSEX memory = {sizeof(memory)};
        PROCESS_MEMORY_COUNTERS_EX counters = {};
        GlobalMemoryStatusEx(&memory);
        K32GetProcessMemoryInfo(GetCurrentProcess(), reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&counters), sizeof(counters));
        WriteLog("Memory: address space used %llu of %llu MB, private %llu MB",
                 (memory.ullTotalVirtual - memory.ullAvailVirtual) >> 20, memory.ullTotalVirtual >> 20,
                 static_cast<unsigned long long>(counters.PrivateUsage) >> 20);
    }
    if (!(presentCount % 600) && g_cfg.alphaTestFromTexkill) {
        WriteLog("AlphaTestFromTexkill: last 600 frames %ld cut-out draws alpha-tested for Remix, %ld texkill draws "
                 "left alone (the game discards nothing there)",
                 InterlockedExchange(&g_alphaTestHinted, 0), InterlockedExchange(&g_alphaTestLeft, 0));
    }
    if (!(presentCount % 600) && g_cfg.skipShadowMapDraws) {
        WriteLog("SkipShadowMapDraws: last 600 frames skipped %ld shadow-map draws", InterlockedExchange(&g_shadowMapSkipped, 0));
        WriteLog("SkipGameLighting: last 600 frames skipped %ld draws into the lit scene", InterlockedExchange(&g_lightingSkipped, 0));
        if (const LONG dropped = focusMessagesDropped ? InterlockedExchange(focusMessagesDropped, 0) : 0)
            WriteLog("FocusGuard: last 600 frames kept %ld focus or minimise messages away from Remix", dropped);
        WriteLog("PackedTexcoordFix: last 600 frames %ld draws with 1/2048 texcoords", InterlockedExchange(&g_packedUvDraws, 0));
    }
    if (!(presentCount % 600) && lightpub::HidePostFx()) {
        WriteLog("HidePostFx: last 600 frames skipped %ld draws, %ld copies; back buffer bound %ld times",
                 InterlockedExchange(&g_postFxSkippedDraws, 0),
                 InterlockedExchange(&g_postFxSkippedCopies, 0),
                 InterlockedExchange(&g_backbufferBinds, 0));
    }
}

AlphaTestHint::AlphaTestHint(IDirect3DDevice9* d) {
    if (!g_cfg.alphaTestFromTexkill || !lightpub::RaytracingOn()) return;
    if (g_cfg.passFilter && !g_drawIsScene) return;
    ShaderTraits traits = {};
    if (g_cachedPsKnown && ShaderCacheUsable(d)) {
        traits = g_cachedPsTraits;
    } else {
        IDirect3DPixelShader9* ps = nullptr;
        if (FAILED(d->GetPixelShader(&ps)) || !ps) return;
        traits = TraitsOf(ps, false);
        ps->Release();
    }
    if (!traits.hasTexkill) return;
    d->GetRenderState(D3DRS_ALPHATESTENABLE, &enable);
    if (enable) return;
    DWORD cutoff = 0;
    if (!QueryDeviceAlphaCutoff(d, traits, g_cfg.alphaTestRef, &cutoff)) {
        InterlockedIncrement(&g_alphaTestLeft);
        return;
    }
    d->GetRenderState(D3DRS_ALPHAFUNC, &func);
    d->GetRenderState(D3DRS_ALPHAREF, &ref);
    d->SetRenderState(D3DRS_ALPHATESTENABLE, TRUE);
    d->SetRenderState(D3DRS_ALPHAFUNC, D3DCMP_GREATEREQUAL);
    d->SetRenderState(D3DRS_ALPHAREF, cutoff);
    device = d;
    InterlockedIncrement(&g_alphaTestHinted);
}

AlphaTestHint::~AlphaTestHint() {
    if (!device) return;
    device->SetRenderState(D3DRS_ALPHATESTENABLE, enable);
    device->SetRenderState(D3DRS_ALPHAFUNC, func);
    device->SetRenderState(D3DRS_ALPHAREF, ref);
}

CloseCameraFadeGuard::CloseCameraFadeGuard(IDirect3DDevice9* d) {
    if (!g_cfg.passFilter || !g_drawIsScene || !lightpub::RaytracingOn() ||
        !g_cachedPsKnown || !g_cachedVsKnown || !ShaderCacheUsable(d)) return;
    if (!g_cachedPsTraits.closeCameraFade || !g_cachedVsTraits.skinned) return;
    float viewPos[4] = {}, bone0[3][4] = {};
    if (SUCCEEDED(d->GetVertexShaderConstantF(13, viewPos, 1)) &&
        SUCCEEDED(d->GetVertexShaderConstantF(41, bone0[0], 3))) {
        const float dx = bone0[0][3] - viewPos[0];
        const float dy = bone0[1][3] - viewPos[1];
        if (dx * dx + dy * dy < 50.0f * 50.0f) {
            dropDraw = true;
            return;
        }
    }
    if (SUCCEEDED(d->GetRenderState(D3DRS_ALPHABLENDENABLE, &prevBlend)) && prevBlend) {
        d->SetRenderState(D3DRS_ALPHABLENDENABLE, FALSE);
        device = d;
    }
}

CloseCameraFadeGuard::~CloseCameraFadeGuard() {
    if (device) device->SetRenderState(D3DRS_ALPHABLENDENABLE, prevBlend);
}

}  // namespace proxy
