#include "handoff_renderer.h"

#include "lightpub.h"
#include "proxy_log.h"
#include "rt_decode_ps.h"

#include <cmath>
#include <cstdio>

namespace proxy {
namespace {

bool g_handoffBeforePost = true;
int g_headroomStops = 4;
bool g_modernFilter = true;

bool g_bracketOpen = false;
IDirect3DQuery9* g_bracketQuery = nullptr;
IDirect3DDevice9* g_bracketDevice = nullptr;
bool g_bracketFailed = false;
LONG g_statBracketOpens = 0;

IDirect3DSurface9* g_compositeTarget = nullptr;
IDirect3DSurface9* g_sceneHdrTarget = nullptr;
IDirect3DBaseTexture9* g_checkedTexture = nullptr;
bool g_checkedReadsScene = false;

IDirect3DTexture9* g_rtImage = nullptr;
IDirect3DPixelShader9* g_decodePs = nullptr;
IDirect3DStateBlock9* g_handoffStateBlock = nullptr;
IDirect3DDevice9* g_handoffDevice = nullptr;
IDirect3DSurface9* g_cachedCompositeSurf = nullptr;
UINT g_cachedCompositeW = 0, g_cachedCompositeH = 0;
UINT g_rtImageWidth = 0;
UINT g_rtImageHeight = 0;
bool g_handoffResourcesFailed = false;
LONG g_statEarlyHandoffs = 0;

IDirect3DStateBlock9* EnsureHandoffStateBlock(IDirect3DDevice9* device) {
    if (g_handoffStateBlock) return g_handoffStateBlock;
    if (FAILED(device->BeginStateBlock())) return nullptr;
    static const D3DRENDERSTATETYPE kRs[] = {
        D3DRS_ZENABLE, D3DRS_ZWRITEENABLE, D3DRS_ALPHABLENDENABLE, D3DRS_ALPHATESTENABLE,
        D3DRS_STENCILENABLE, D3DRS_CULLMODE, D3DRS_SCISSORTESTENABLE, D3DRS_SRGBWRITEENABLE,
        D3DRS_FOGENABLE, D3DRS_COLORWRITEENABLE,
    };
    static const D3DSAMPLERSTATETYPE kSs[] = {
        D3DSAMP_ADDRESSU, D3DSAMP_ADDRESSV, D3DSAMP_MAGFILTER,
        D3DSAMP_MINFILTER, D3DSAMP_MIPFILTER, D3DSAMP_SRGBTEXTURE,
    };
    for (D3DRENDERSTATETYPE rs : kRs) device->SetRenderState(rs, 0);
    for (D3DSAMPLERSTATETYPE ss : kSs) device->SetSamplerState(0, ss, 0);
    device->SetVertexShader(nullptr);
    device->SetPixelShader(nullptr);
    device->SetFVF(D3DFVF_XYZRHW | D3DFVF_TEX1);
    device->SetStreamSource(0, nullptr, 0, 0);
    device->SetTexture(0, nullptr);
    const float zero[4] = {};
    device->SetPixelShaderConstantF(0, zero, 1);
    D3DVIEWPORT9 vp = {0, 0, 1, 1, 0.0f, 1.0f};
    device->SetViewport(&vp);
    if (FAILED(device->EndStateBlock(&g_handoffStateBlock))) g_handoffStateBlock = nullptr;
    return g_handoffStateBlock;
}
}  // namespace

void SetHandoffConfig(bool handoffBeforePost, int headroomStops, bool modernFilter, bool logAndApplyOverrides) {
    if (logAndApplyOverrides &&
        (handoffBeforePost != g_handoffBeforePost || headroomStops != g_headroomStops || modernFilter != g_modernFilter)) {
        WriteLog("HandoffBeforePost: now %d, headroom %d stops, ModernFilter %d",
                 handoffBeforePost ? 1 : 0, headroomStops, modernFilter ? 1 : 0);
        g_handoffBeforePost = handoffBeforePost;
        g_headroomStops = headroomStops;
        g_modernFilter = modernFilter;
        ApplyHandoffOverrides();
        return;
    }
    g_handoffBeforePost = handoffBeforePost;
    g_headroomStops = headroomStops;
    g_modernFilter = modernFilter;
}

bool IsHandoffBeforePostEnabled() { return g_handoffBeforePost; }
bool IsModernFilterEnabled() { return g_modernFilter; }

void ApplyHandoffOverrides() {
    if (!g_handoffBeforePost || g_modernFilter) {
        lightpub::SetRemixOverrides(nullptr, 0);
        return;
    }
    char bias[16];
    sprintf_s(bias, "%d", -g_headroomStops);
    const char* const list[][3] = {
        {"rtx.tonemap.exposureBias", bias, "0"},
        {"rtx.tonemap.tonemappingEnabled", "False", "True"},
        {"rtx.tonemap.colorGradingEnabled", "False", "False"},
        {"rtx.tonemap.ditherMode", "0", "2"},
        {"rtx.tonemappingMode", "0", "0"},
        {"rtx.autoExposure.enabled", "False", "True"},
        {"rtx.bloom.enable", "False", "True"},
        {"rtx.postfx.enable", "False", "True"},
    };
    lightpub::SetRemixOverrides(list, static_cast<int>(ARRAYSIZE(list)));
}

void SetBracket(IDirect3DDevice9* device, bool open) {
    if (open == g_bracketOpen && g_bracketDevice == device) return;
    if (g_bracketDevice != device) {
        g_bracketQuery = nullptr;
        g_bracketDevice = device;
        g_bracketOpen = false;
        g_bracketFailed = false;
    }
    if (!g_bracketQuery) {
        if (g_bracketFailed) return;
        if (FAILED(device->CreateQuery(D3DQUERYTYPE_OCCLUSION, &g_bracketQuery)) || !g_bracketQuery) {
            g_bracketFailed = true;
            g_bracketQuery = nullptr;
            WriteLog("PassFilter: could not create the occlusion query; non-scene draws will reach Remix");
            return;
        }
    }
    if (SUCCEEDED(g_bracketQuery->Issue(open ? D3DISSUE_BEGIN : D3DISSUE_END))) {
        g_bracketOpen = open;
        if (open) InterlockedIncrement(&g_statBracketOpens);
    }
}

bool IsBracketOpen() { return g_bracketOpen; }

void LatchCompositeTarget(IDirect3DSurface9* rt0) {
    if (rt0 && g_compositeTarget != rt0) {
        if (g_compositeTarget) g_compositeTarget->Release();
        g_compositeTarget = rt0;
        g_compositeTarget->AddRef();
    }
}

void RememberSceneHdr(IDirect3DSurface9* surface) {
    if (!surface || surface == g_sceneHdrTarget) return;
    if (g_sceneHdrTarget) g_sceneHdrTarget->Release();
    g_sceneHdrTarget = surface;
    g_sceneHdrTarget->AddRef();
    g_checkedTexture = nullptr;
}
IDirect3DSurface9* CurrentSceneHdrTarget() { return g_sceneHdrTarget; }

bool ReadsSceneHdr(IDirect3DBaseTexture9* texture) {
    if (!g_sceneHdrTarget || !texture) return false;
    if (texture == g_checkedTexture) return g_checkedReadsScene;
    g_checkedTexture = texture;
    g_checkedReadsScene = false;
    if (texture->GetType() != D3DRTYPE_TEXTURE) return false;
    IDirect3DSurface9* level0 = nullptr;
    if (SUCCEEDED(static_cast<IDirect3DTexture9*>(texture)->GetSurfaceLevel(0, &level0)) && level0) {
        g_checkedReadsScene = level0 == g_sceneHdrTarget;
        level0->Release();
    }
    return g_checkedReadsScene;
}

void ReleaseHandoffResources() {
    if (g_rtImage) {
        g_rtImage->Release();
        g_rtImage = nullptr;
    }
    g_rtImageWidth = g_rtImageHeight = 0;
}

void HandoffOnDeviceReset() {
    if (g_rtImage) WriteLog("HandoffBeforePost: device Reset, image texture released (recreated on the next frame)");
    ReleaseHandoffResources();
    if (g_handoffStateBlock) { g_handoffStateBlock->Release(); g_handoffStateBlock = nullptr; }
    if (g_bracketQuery) { g_bracketQuery->Release(); g_bracketQuery = nullptr; }
    g_bracketOpen = false;
    g_bracketFailed = false;
    if (g_sceneHdrTarget) { g_sceneHdrTarget->Release(); g_sceneHdrTarget = nullptr; }
    if (g_compositeTarget) { g_compositeTarget->Release(); g_compositeTarget = nullptr; }
    g_cachedCompositeSurf = nullptr;
    g_cachedCompositeW = g_cachedCompositeH = 0;
    g_checkedTexture = nullptr;
}

bool EnsureHandoffResources(IDirect3DDevice9* device, UINT width, UINT height) {
    if (g_handoffDevice != device) {
        g_rtImage = nullptr;
        g_decodePs = nullptr;
        g_handoffStateBlock = nullptr;
        g_cachedCompositeSurf = nullptr;
        g_cachedCompositeW = g_cachedCompositeH = 0;
        g_rtImageWidth = g_rtImageHeight = 0;
        g_handoffDevice = device;
        g_handoffResourcesFailed = false;
    }
    if (g_rtImage && (g_rtImageWidth != width || g_rtImageHeight != height)) ReleaseHandoffResources();
    if (!g_rtImage) {
        if (FAILED(device->CreateTexture(width, height, 1, D3DUSAGE_RENDERTARGET, D3DFMT_A16B16G16R16F, D3DPOOL_DEFAULT,
                                         &g_rtImage, nullptr)) || !g_rtImage) {
            g_rtImage = nullptr;
            g_handoffResourcesFailed = true;
            WriteLog("HandoffBeforePost: could not create the %ux%u image texture; using the hand-off after the composite",
                     width, height);
            return false;
        }
        g_rtImageWidth = width;
        g_rtImageHeight = height;
    }
    if (!g_decodePs && (FAILED(device->CreatePixelShader(reinterpret_cast<const DWORD*>(g_rtDecodePs), &g_decodePs)) ||
                        !g_decodePs)) {
        g_decodePs = nullptr;
        g_handoffResourcesFailed = true;
        WriteLog("HandoffBeforePost: could not create the decode shader; using the hand-off after the composite");
        return false;
    }
    return true;
}

void DecodeIntoScene(IDirect3DDevice9* device, DeviceHook* hook, UINT width, UINT height) {
    IDirect3DSurface9* previousTarget = nullptr;
    device->GetRenderTarget(0, &previousTarget);
    IDirect3DStateBlock9* sb = EnsureHandoffStateBlock(device);
    if (sb) sb->Capture();
    static const struct { D3DRENDERSTATETYPE id; DWORD value; } kStates[] = {
        {D3DRS_ZENABLE, FALSE}, {D3DRS_ZWRITEENABLE, FALSE}, {D3DRS_ALPHABLENDENABLE, FALSE},
        {D3DRS_ALPHATESTENABLE, FALSE}, {D3DRS_STENCILENABLE, FALSE}, {D3DRS_CULLMODE, D3DCULL_NONE},
        {D3DRS_SCISSORTESTENABLE, FALSE}, {D3DRS_SRGBWRITEENABLE, FALSE}, {D3DRS_FOGENABLE, FALSE},
        {D3DRS_COLORWRITEENABLE, D3DCOLORWRITEENABLE_RED | D3DCOLORWRITEENABLE_GREEN | D3DCOLORWRITEENABLE_BLUE},
    };
    static const struct { D3DSAMPLERSTATETYPE id; DWORD value; } kSamplerStates[] = {
        {D3DSAMP_ADDRESSU, D3DTADDRESS_CLAMP}, {D3DSAMP_ADDRESSV, D3DTADDRESS_CLAMP}, {D3DSAMP_MAGFILTER, D3DTEXF_POINT},
        {D3DSAMP_MINFILTER, D3DTEXF_POINT}, {D3DSAMP_MIPFILTER, D3DTEXF_NONE}, {D3DSAMP_SRGBTEXTURE, FALSE},
    };
    DWORD savedStates[ARRAYSIZE(kStates)] = {}, savedSampler[ARRAYSIZE(kSamplerStates)] = {};
    if (!sb) {
        for (size_t i = 0; i < ARRAYSIZE(kStates); ++i) device->GetRenderState(kStates[i].id, &savedStates[i]);
        for (size_t i = 0; i < ARRAYSIZE(kSamplerStates); ++i) device->GetSamplerState(0, kSamplerStates[i].id, &savedSampler[i]);
    }
    for (size_t i = 0; i < ARRAYSIZE(kStates); ++i) device->SetRenderState(kStates[i].id, kStates[i].value);
    for (size_t i = 0; i < ARRAYSIZE(kSamplerStates); ++i) device->SetSamplerState(0, kSamplerStates[i].id, kSamplerStates[i].value);
    if (previousTarget != g_sceneHdrTarget) device->SetRenderTarget(0, g_sceneHdrTarget);
    device->SetVertexShader(nullptr);
    device->SetPixelShader(g_decodePs);
    device->SetFVF(D3DFVF_XYZRHW | D3DFVF_TEX1);
    device->SetTexture(0, g_rtImage);
    const float linearScale = g_modernFilter ? 1.0f : exp2f(static_cast<float>(g_headroomStops));
    const float decode[4] = {2.2f, linearScale, 0.0f, 0.0f};
    device->SetPixelShaderConstantF(0, decode, 1);
    const float right = static_cast<float>(width) - 0.5f, bottom = static_cast<float>(height) - 0.5f;
    const float quad[4][6] = {
        {-0.5f, -0.5f, 0.0f, 1.0f, 0.0f, 0.0f}, {right, -0.5f, 0.0f, 1.0f, 1.0f, 0.0f},
        {-0.5f, bottom, 0.0f, 1.0f, 0.0f, 1.0f}, {right, bottom, 0.0f, 1.0f, 1.0f, 1.0f},
    };
    hook->drawPrimitiveUP(device, D3DPT_TRIANGLESTRIP, 2, quad, sizeof(quad[0]));
    if (previousTarget != g_sceneHdrTarget) device->SetRenderTarget(0, previousTarget);
    if (sb) {
        sb->Apply();
    } else {
        for (size_t i = 0; i < ARRAYSIZE(kStates); ++i) device->SetRenderState(kStates[i].id, savedStates[i]);
        for (size_t i = 0; i < ARRAYSIZE(kSamplerStates); ++i) device->SetSamplerState(0, kSamplerStates[i].id, savedSampler[i]);
    }
    if (previousTarget) previousTarget->Release();
}

bool PresentModernSceneToComposite(IDirect3DDevice9* device, DeviceHook* hook, IDirect3DSurface9* target) {
    if (!g_sceneHdrTarget || !target || !hook || !hook->drawPrimitiveUP) return false;
    UINT dstW = g_cachedCompositeW, dstH = g_cachedCompositeH;
    if (target != g_cachedCompositeSurf || !dstW || !dstH) {
        D3DSURFACE_DESC dstDesc = {};
        if (FAILED(target->GetDesc(&dstDesc)) || !dstDesc.Width || !dstDesc.Height) return false;
        g_cachedCompositeSurf = target;
        dstW = g_cachedCompositeW = dstDesc.Width;
        dstH = g_cachedCompositeH = dstDesc.Height;
    }
    if (!EnsureHandoffResources(device, dstW, dstH)) return false;
    static const GUID kTexture9Iid = {0x85c31227, 0x3de5, 0x4f00, {0x9b, 0x3a, 0xf1, 0x1a, 0xc3, 0x8c, 0x18, 0xb5}};
    IDirect3DTexture9* sceneTex = nullptr;
    if (FAILED(g_sceneHdrTarget->GetContainer(kTexture9Iid, reinterpret_cast<void**>(&sceneTex))) || !sceneTex) {
        return false;
    }
    SetBracket(device, false);
    IDirect3DSurface9* previousTarget = nullptr;
    device->GetRenderTarget(0, &previousTarget);
    IDirect3DStateBlock9* sb = EnsureHandoffStateBlock(device);
    if (sb) sb->Capture();
    static const struct { D3DRENDERSTATETYPE id; DWORD value; } kStates[] = {
        {D3DRS_ZENABLE, FALSE}, {D3DRS_ZWRITEENABLE, FALSE}, {D3DRS_ALPHABLENDENABLE, FALSE},
        {D3DRS_ALPHATESTENABLE, FALSE}, {D3DRS_STENCILENABLE, FALSE}, {D3DRS_CULLMODE, D3DCULL_NONE},
        {D3DRS_SCISSORTESTENABLE, FALSE}, {D3DRS_SRGBWRITEENABLE, FALSE}, {D3DRS_FOGENABLE, FALSE},
        {D3DRS_COLORWRITEENABLE, 0xF},
    };
    static const struct { D3DSAMPLERSTATETYPE id; DWORD value; } kSamplerStates[] = {
        {D3DSAMP_ADDRESSU, D3DTADDRESS_CLAMP}, {D3DSAMP_ADDRESSV, D3DTADDRESS_CLAMP},
        {D3DSAMP_MAGFILTER, D3DTEXF_LINEAR}, {D3DSAMP_MINFILTER, D3DTEXF_LINEAR},
        {D3DSAMP_MIPFILTER, D3DTEXF_NONE}, {D3DSAMP_SRGBTEXTURE, FALSE},
    };
    DWORD savedStates[ARRAYSIZE(kStates)] = {}, savedSampler[ARRAYSIZE(kSamplerStates)] = {};
    if (!sb) {
        for (size_t i = 0; i < ARRAYSIZE(kStates); ++i) device->GetRenderState(kStates[i].id, &savedStates[i]);
        for (size_t i = 0; i < ARRAYSIZE(kSamplerStates); ++i) device->GetSamplerState(0, kSamplerStates[i].id, &savedSampler[i]);
    }
    for (size_t i = 0; i < ARRAYSIZE(kStates); ++i) device->SetRenderState(kStates[i].id, kStates[i].value);
    for (size_t i = 0; i < ARRAYSIZE(kSamplerStates); ++i) device->SetSamplerState(0, kSamplerStates[i].id, kSamplerStates[i].value);
    if (previousTarget != target) device->SetRenderTarget(0, target);
    device->SetVertexShader(nullptr);
    device->SetPixelShader(g_decodePs);
    device->SetFVF(D3DFVF_XYZRHW | D3DFVF_TEX1);
    device->SetTexture(0, sceneTex);
    const float encodeSrgb[4] = {1.0f / 2.2f, 1.0f, 0.0f, 0.0f};
    device->SetPixelShaderConstantF(0, encodeSrgb, 1);
    const float right = static_cast<float>(dstW) - 0.5f, bottom = static_cast<float>(dstH) - 0.5f;
    const float quad[4][6] = {
        {-0.5f, -0.5f, 0.0f, 1.0f, 0.0f, 0.0f}, {right, -0.5f, 0.0f, 1.0f, 1.0f, 0.0f},
        {-0.5f, bottom, 0.0f, 1.0f, 0.0f, 1.0f}, {right, bottom, 0.0f, 1.0f, 1.0f, 1.0f},
    };
    hook->drawPrimitiveUP(device, D3DPT_TRIANGLESTRIP, 2, quad, sizeof(quad[0]));
    if (previousTarget != target) device->SetRenderTarget(0, previousTarget);
    if (sb) {
        sb->Apply();
    } else {
        for (size_t i = 0; i < ARRAYSIZE(kStates); ++i) device->SetRenderState(kStates[i].id, savedStates[i]);
        for (size_t i = 0; i < ARRAYSIZE(kSamplerStates); ++i) device->SetSamplerState(0, kSamplerStates[i].id, savedSampler[i]);
    }
    sceneTex->Release();
    if (previousTarget) previousTarget->Release();
    return true;
}

void EmitHandoffMarker(IDirect3DDevice9* device,
                       DeviceHook* hook,
                       PassGraph& passGraph,
                       const CameraFrameTracker& cameraTracker,
                       IDirect3DSurface9* currentRt0,
                       FlushSettersFn flushSetters) {
    IDirect3DSurface9* target = g_compositeTarget;
    g_compositeTarget = nullptr;
    if (!target) return;
    if (!passGraph.SceneDrawsThisFrame() || passGraph.MarkerDoneThisFrame() ||
        !hook->drawPrimitiveUP || !hook->setTransform) {
        target->Release();
        return;
    }
    if (flushSetters) flushSetters(device);
    passGraph.NoteHandoffCompleted();
    SetBracket(device, false);
    IDirect3DSurface9* previousTarget = nullptr;
    D3DVIEWPORT9 viewport = {};
    const bool rebind = target != currentRt0;
    if (rebind) {
        device->GetViewport(&viewport);
        device->GetRenderTarget(0, &previousTarget);
        device->SetRenderTarget(0, target);
    }
    IDirect3DVertexShader9* vs = nullptr;
    IDirect3DPixelShader9* ps = nullptr;
    IDirect3DVertexDeclaration9* decl = nullptr;
    IDirect3DVertexBuffer9* vb = nullptr;
    UINT vbOffset = 0, vbStride = 0;
    DWORD fvf = 0;
    device->GetVertexShader(&vs);
    device->GetPixelShader(&ps);
    device->GetVertexDeclaration(&decl);
    device->GetFVF(&fvf);
    device->GetStreamSource(0, &vb, &vbOffset, &vbStride);
    static const struct { D3DRENDERSTATETYPE id; DWORD value; } kStates[] = {
        {D3DRS_ZWRITEENABLE, FALSE}, {D3DRS_ZENABLE, FALSE}, {D3DRS_COLORWRITEENABLE, 0xF},
        {D3DRS_STENCILENABLE, FALSE}, {D3DRS_ALPHABLENDENABLE, FALSE}, {D3DRS_ALPHATESTENABLE, FALSE},
    };
    DWORD saved[ARRAYSIZE(kStates)] = {};
    for (size_t i = 0; i < ARRAYSIZE(kStates); ++i) {
        device->GetRenderState(kStates[i].id, &saved[i]);
        device->SetRenderState(kStates[i].id, kStates[i].value);
    }
    device->SetVertexShader(nullptr);
    device->SetPixelShader(nullptr);
    device->SetFVF(D3DFVF_XYZ);
    D3DMATRIX identity = {};
    identity.m[0][0] = identity.m[1][1] = identity.m[2][2] = identity.m[3][3] = 1.0f;
    hook->setTransform(device, D3DTS_PROJECTION, &identity);
    static const float kDegenerateTriangle[9] = {};
    hook->drawPrimitiveUP(device, D3DPT_TRIANGLELIST, 1, kDegenerateTriangle, 3 * sizeof(float));
    if (cameraTracker.HasProjection()) hook->setTransform(device, D3DTS_PROJECTION, &cameraTracker.LastProjection());
    for (size_t i = 0; i < ARRAYSIZE(kStates); ++i) device->SetRenderState(kStates[i].id, saved[i]);
    if (decl) device->SetVertexDeclaration(decl); else device->SetFVF(fvf);
    device->SetStreamSource(0, vb, vbOffset, vbStride);
    device->SetVertexShader(vs);
    device->SetPixelShader(ps);
    if (rebind) {
        device->SetRenderTarget(0, previousTarget);
        device->SetViewport(&viewport);
    }
    if (vs) vs->Release();
    if (ps) ps->Release();
    if (decl) decl->Release();
    if (vb) vb->Release();
    if (previousTarget) previousTarget->Release();
    target->Release();
}

bool HandoffBeforePost(IDirect3DDevice9* device,
                       DeviceHook* hook,
                       PassGraph& passGraph,
                       const CameraFrameTracker& cameraTracker,
                       IDirect3DSurface9* currentRt0,
                       UINT backbufferWidth,
                       UINT backbufferHeight,
                       FlushSettersFn flushSetters) {
    if (!lightpub::RaytracingOn() || passGraph.RtOnFrames() < 2 || passGraph.GogglesActive() ||
        !passGraph.SceneDrawsThisFrame() || !hook->drawPrimitiveUP || !g_sceneHdrTarget)
        return false;
    D3DSURFACE_DESC desc = {};
    if (FAILED(g_sceneHdrTarget->GetDesc(&desc)) || desc.Width != backbufferWidth || desc.Height != backbufferHeight)
        return false;
    if (flushSetters) flushSetters(device);
    if (!EnsureHandoffResources(device, desc.Width, desc.Height)) return false;
    IDirect3DSurface9* image = nullptr;
    if (FAILED(g_rtImage->GetSurfaceLevel(0, &image)) || !image) return false;
    if (g_compositeTarget) g_compositeTarget->Release();
    g_compositeTarget = image;
    EmitHandoffMarker(device, hook, passGraph, cameraTracker, currentRt0, flushSetters);
    if (!passGraph.MarkerDoneThisFrame()) return false;
    DecodeIntoScene(device, hook, desc.Width, desc.Height);
    InterlockedIncrement(&g_statEarlyHandoffs);
    return true;
}

void HandoffEndFrame(IDirect3DDevice9* device,
                     DeviceHook* hook,
                     PassGraph& passGraph,
                     const CameraFrameTracker& cameraTracker,
                     IDirect3DSurface9* currentRt0,
                     bool raytracingOn) {
    const bool needFinalHandoff = passGraph.EndFrame(g_sceneHdrTarget, raytracingOn);
    if (needFinalHandoff && g_compositeTarget) {
        EmitHandoffMarker(device, hook, passGraph, cameraTracker, currentRt0, nullptr);
    }
    SetBracket(device, false);
    if (g_compositeTarget) { g_compositeTarget->Release(); g_compositeTarget = nullptr; }
    if (g_sceneHdrTarget) { g_sceneHdrTarget->Release(); g_sceneHdrTarget = nullptr; }
    g_checkedTexture = nullptr;
}

LONG TakeEarlyHandoffsCount() { return InterlockedExchange(&g_statEarlyHandoffs, 0); }
LONG TakeBracketOpensCount() { return InterlockedExchange(&g_statBracketOpens, 0); }

}  // namespace proxy
