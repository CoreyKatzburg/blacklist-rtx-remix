#pragma once
// Stutter, camera, texture, hitch, and single-frame draw-dump diagnostics:
// CallTimer/CallTiming CSV, FrameTrace CSV, main-loop QPC timer/smoother, HitchSampler,
// TexStats lifetime profiler, SkinTrace, and CaptureDraws TSV/shader dumps.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d9.h>
#include <cstddef>
#include <cstdint>
#include <vector>

#include "camera_solver.h"
#include "pass_graph.h"
#include "proxy_types.h"
#include "shader_traits.h"

namespace proxy {

enum CallKind {
    kCreateTexture,
    kCreateVertexBuffer,
    kCreateIndexBuffer,
    kTextureLock,
    kTextureUnlock,
    kBufferLock,
    kBufferUnlock,
    kQueryGetData,
    kDraw,
    kPresent,
    kClear,
    kUpdateSurface,
    kUpdateTexture,
    kGetRenderTargetData,
    kStretchRect,
    kCallKindCount
};

struct DiagnosticsConfig {
    bool frameTrace = false;
    UINT frameTraceMax = 36000;
    bool callTiming = false;
    UINT frameTiming = 0;
    bool hitchSampler = false;
    UINT sampleMs = 5;
    double hitchMs = 50.0;
    bool texStats = false;
};

// Configures diagnostic switches at startup and resolves trace output CSV paths.
// Clears frameTrace/callTiming on *cfg if output CSV paths exceed MAX_PATH.
void ConfigureDiagnostics(DiagnosticsConfig* cfg);
void InitDiagnosticsQpc();
double QpcToMilliseconds(LONGLONG ticks);

bool IsFrameTraceEnabled();
bool IsCallTimingEnabled();
bool IsHitchSamplerEnabled();
bool IsTexStatsEnabled();
UINT FrameTimingMode();

void HitchNoteCall(LONG kind);
bool BeginTimedCall(CallKind kind, LARGE_INTEGER* startOut);
void EndTimedCall(CallKind kind, LARGE_INTEGER start, bool timed);

// Records duration and hitch-sampler call state for one D3D9 method invocation.
class CallTimer {
public:
    explicit CallTimer(CallKind kind) : kind_(kind), timed_(BeginTimedCall(kind, &start_)) {}
    ~CallTimer() { EndTimedCall(kind_, start_, timed_); }
    CallTimer(const CallTimer&) = delete;
    CallTimer& operator=(const CallTimer&) = delete;

private:
    CallKind kind_;
    LARGE_INTEGER start_ = {};
    bool timed_ = false;
};

// Main-loop frame clock logger and optional smoother (Blacklist_game.exe 0x006FE4B7).
void InstallLoopTimer(uintptr_t executableBase);
void TraceLoopFrame(LONGLONG real, LONGLONG out);

// Rate-limited camera decomposition accept/reject diagnostic logger.
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
                     const float constants[12][4]);

// Per-frame camera publication and D3D9 call-timing CSV writers.
void RecordTracedPublish(DeviceHook* hook,
                         uintptr_t callerRva,
                         const D3DMATRIX& view,
                         const float constants[12][4],
                         bool skipped);
void RecordFrameCalls(DeviceHook* hook, size_t bufferSize, LONGLONG presentTicks);
struct GateFrameMetrics {
    uint32_t sceneDraws = 0;
    uint32_t skinnedSceneDraws = 0;
    uint32_t handoffs = 0;
    bool goggles = false;
    bool rtActive = false;
};
void FinishTracedFrame(DeviceHook* hook, LONGLONG entry, LONGLONG exit, const GateFrameMetrics& gate);
void FlushFrameTrace(DeviceHook* hook);

// HitchSampler render-thread entry hook.
void HitchSamplerFrame(LONGLONG entry);

// TexStats allocation, lock, release, and periodic summary writers.
void RecordTextureCreate(void* caller,
                         IDirect3DTexture9* texture,
                         UINT width,
                         UINT height,
                         UINT levels,
                         DWORD usage,
                         D3DFORMAT format,
                         D3DPOOL pool,
                         double ms,
                         LONG presentCount);
void RecordTextureLock(void* caller,
                       IDirect3DTexture9* texture,
                       UINT level,
                       const RECT* rect,
                       DWORD flags,
                       double ms,
                       LONG presentCount);
void OnTextureReleasedForStats(void* texture, LONG presentCount);
void NoteAvailableTextureMem(UINT available, void* caller);
void DumpTexStats(LONG presentCount);
void MaybeDumpTexStats(LONGLONG exitQpc, LONG presentCount);

// SkinTrace per-draw counter and end-of-frame logger.
void SetSkinTraceEnabled(bool enabled);
bool IsSkinTraceEnabled();
void RecordSkinnedDraw(FrameStage stage,
                       bool isScene,
                       bool rt1Bound,
                       bool alphaBlendEnabled,
                       const ShaderTraits& ps,
                       const DrawGeometryKey* geometry);
void FinishSkinTraceFrame(LONG presentCount);

// CaptureDraws one-frame / burst TSV and shader bytecode dumper.
using LookupPsTraitsFn = ShaderTraits (*)(IDirect3DPixelShader9* ps);

struct DrawCaptureContext {
    LONG presentCount = 0;
    bool rt0IsBackbuffer = false;
    bool rt0IsShadowMap = false;
    const bool* stageIsRt = nullptr;
    DWORD trackedStages = 0;
    bool skipShadowMapDraws = false;
    bool drawIsWasted = false;
    bool alphaTestFromTexkill = false;
    DWORD alphaTestRef = 128;
    bool passFilter = false;
    bool drawIsScene = false;
    bool bracketOpen = false;
    bool markerDone = false;
    bool drawIsDecal = false;
    const std::vector<uint32_t>* skipVs = nullptr;
    const std::vector<uint32_t>* skipVsAdditive = nullptr;
    const std::vector<uint32_t>* menuVs = nullptr;
    LookupPsTraitsFn lookupPsTraits = nullptr;
};

bool IsCaptureDrawsActive();
void SetCaptureDrawsConfig(bool requested, int burstCount);
void BeginFrameDump(LONG presentCount);
void FlushFrameDump();
void UpdateCaptureDrawsEndFrame(LONG presentCount);
void CaptureDraw(IDirect3DDevice9* device,
                 const char* kind,
                 UINT vertices,
                 UINT primitives,
                 void* caller,
                 const DrawCaptureContext& ctx);
void CaptureCameraPublish(uintptr_t callerRva,
                          const D3DMATRIX& view,
                          const D3DMATRIX& projection,
                          bool skipped);

}  // namespace proxy
