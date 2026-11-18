#pragma once
// Per-draw scene stage filtering, setter coalescing integration, shader-trait cache,
// packed-UV texture transform, texkill alpha-test hints, and close-camera character fade guard.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d9.h>
#include <cstdint>

#include "camera_solver.h"
#include "pass_graph.h"
#include "proxy_diagnostics.h"
#include "proxy_types.h"

namespace proxy {

using HookShaderReleaseFn = void (*)(void* shader, bool vertex);

struct FilterConfig {
    bool passFilter = false;
    bool forwardScene = true;
    bool skipShadowMapDraws = false;
    bool skipGameLighting = true;
    bool skipGamePost = false;
    bool gogglesGameView = true;
    bool handoffBeforePost = true;
    bool handoffBeforeEffects = true;
    bool packedTexcoordFix = false;
    bool modernFilter = false;
    bool raytraceParticles = false;
    bool alphaTestFromTexkill = false;
    DWORD alphaTestRef = 128;
};

struct FilterOriginals {
    SetterCoalescer::SetTextureFn setTexture = nullptr;
    SetterCoalescer::SetRenderStateFn setRenderState = nullptr;
    SetterCoalescer::SetStreamSourceFn setStreamSource = nullptr;
    SetterCoalescer::SetIndicesFn setIndices = nullptr;
    SetterCoalescer::SetPsConstFFn setPixelShaderConstantF = nullptr;
    bool shaderHooksInstalled = false;
    HookShaderReleaseFn hookShaderRelease = nullptr;
};

void SetFilterConfig(const FilterConfig& cfg);
void SetFilterOriginals(const FilterOriginals& originals);
void ReloadSkipVsLists();
void SetGateDumpTag(const char* tag);

// Tracks render-target usage on newly created textures and invalidates destroyed shaders.
void RecordTextureUsage(void* texture, DWORD usage);
void ForgetShader(void* shader);

// Refreshes and queries the current device backbuffer and its dimensions.
IDirect3DSurface9* CurrentBackbuffer(IDirect3DDevice9* device);
void RefreshBackbuffer(IDirect3DDevice9* device);
UINT BackbufferWidth();
UINT BackbufferHeight();

// Setter hooks: update tracked render state and return true when buffered locally by SetterCoalescer.
void OnSetRenderTarget(IDirect3DDevice9* device, DWORD index, IDirect3DSurface9* surface);
bool TrySuppressSetTexture(IDirect3DDevice9* device, DWORD stage, IDirect3DBaseTexture9* texture);
bool TrySuppressSetRenderState(IDirect3DDevice9* device, D3DRENDERSTATETYPE state, DWORD value);
bool TrySuppressSetStreamSource(IDirect3DDevice9* device, UINT stream, IDirect3DVertexBuffer9* vb, UINT offset, UINT stride);
bool TrySuppressSetIndices(IDirect3DDevice9* device, IDirect3DIndexBuffer9* ib);
bool TrySuppressSetPixelShaderConstantF(IDirect3DDevice9* device, UINT startReg, const float* data, UINT vec4Count);
void OnVertexShaderSet(IDirect3DDevice9* device, IDirect3DVertexShader9* shader);
void OnPixelShaderSet(IDirect3DDevice9* device, IDirect3DPixelShader9* shader);
void FlushCoalescedSetters(IDirect3DDevice9* device);
void FilterOnDeviceReset();

// Query trace (query_trace.h, X3): whether the last PrepareDraw classed its draw as a scene draw, and a render state as
// the next sent draw will see it (a value SetterCoalescer still buffers, else the device's).
bool LastDrawWasScene();
DWORD TraceRenderState(IDirect3DDevice9* device, D3DRENDERSTATETYPE state);

// Builds a DrawGeometryKey using the currently bound stream-0 VB and (optionally) index buffer.
DrawGeometryKey MakeDrawGeometry(bool indexed,
                                 INT baseVertex,
                                 UINT minIndex,
                                 UINT startLocation,
                                 UINT primitiveCount,
                                 D3DPRIMITIVETYPE primitiveType);

// Runs PassFilterBeforeDraw, DecalTagBeforeDraw, optional CaptureDraw, SkipPostFxDraw, setter flush,
// and PackedTexcoordFix. Returns false when the draw should be dropped without forwarding to D3D9.
bool PrepareDraw(IDirect3DDevice9* device,
                 DeviceHook* hook,
                 const CameraFrameTracker& cameraTracker,
                 const DrawGeometryKey* geometry,
                 const char* drawKind,
                 UINT vertexCount,
                 UINT primitiveCount,
                 void* caller,
                 LONG presentCount);

void OnAfterUpDraw(bool clearIndexBuffer);
void NotePostFxSkippedCopy();

// Frame boundaries and Present status queries.
bool ShouldShowGamePicture();
bool IsGogglesActive();
GateFrameMetrics SnapshotGateFrameMetrics();
void FilterEndFrameBeforePresent(IDirect3DDevice9* device,
                                 DeviceHook* hook,
                                 const CameraFrameTracker& cameraTracker,
                                 LONG presentCount);
void FilterResetAfterPresent(IDirect3DDevice9* device);
void LogFilterStatsPeriodic(LONG presentCount, volatile LONG* focusMessagesDropped);

// RAII draw guards active during the forwarded Draw* call.
struct AlphaTestHint {
    IDirect3DDevice9* device = nullptr;
    DWORD enable = 0, func = 0, ref = 0;
    explicit AlphaTestHint(IDirect3DDevice9* d);
    ~AlphaTestHint();
};

struct CloseCameraFadeGuard {
    IDirect3DDevice9* device = nullptr;
    DWORD prevBlend = 0;
    bool dropDraw = false;
    explicit CloseCameraFadeGuard(IDirect3DDevice9* d);
    ~CloseCameraFadeGuard();
};

}  // namespace proxy
