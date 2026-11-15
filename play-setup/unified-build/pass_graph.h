#pragma once
// Explicit 5-station frame-stage classifier, G-buffer duplicate geometry filter,
// and pass-level setter suppression coalescer for Blacklist -> RTX Remix.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d9.h>
#include <cstddef>
#include <cstdint>

#include "shader_traits.h"

namespace proxy {

enum class FrameStage : uint8_t {
    PreScene = 0,
    DepthPrePass = 1,
    GBuffer = 2,
    ShadowMap = 3,
    DeferredLighting = 4,
    ForwardScene = 5,
    PostProcess = 6,
    CompositeAndUi = 7,
};

struct PassGraphConfig {
    bool enabled = true;
    bool forwardScene = true;
    bool skipShadowMapDraws = true;
    bool skipGameLighting = true;
    bool skipGamePost = true;
    bool gogglesGameView = true;
    bool handoffBeforePost = false;
    bool handoffBeforeEffects = false;
    bool modernFilter = false;
    bool raytraceParticles = false;
};

struct DrawGeometryKey {
    const void* vb = nullptr;
    UINT offset = 0;
    UINT stride = 0;
    const void* ib = nullptr;
    INT baseVertex = 0;
    UINT minIndex = 0;
    UINT startIndex = 0;
    UINT primitives = 0;
    UINT primitiveType = 0;
};

struct DrawContextInput {
    const void* rt0 = nullptr;
    bool rt0Primary = false;
    bool rt0IsShadowMap = false;
    D3DFORMAT rt0Format = D3DFMT_UNKNOWN;
    bool rt1Bound = false;
    bool stage0ReadsSceneHdr = false;
    bool depthTestEnabled = false;
    bool depthWriteEnabled = false;
    bool colorWriteEnabled = false;
    bool alphaBlendEnabled = false;
    bool gogglesSwitchActive = false;
    bool raytracingActive = false;
    ShaderTraits vs = {};
    ShaderTraits ps = {};
    const DrawGeometryKey* geometry = nullptr;
};

struct DrawDecision {
    FrameStage stage = FrameStage::PreScene;
    bool isScene = false;
    bool skipDraw = false;
    bool openBracket = false;
    bool rememberSceneHdrTarget = false;
    bool emitCompositeHandoffBeforeDraw = false;
    bool tryEarlyHandoffBeforeDraw = false;
    bool latchCompositeTarget = false;
};

struct PassGraphStats {
    uint64_t sceneFrames = 0;
    uint64_t sceneDraws = 0;
    uint64_t skippedShadowDraws = 0;
    uint64_t skippedLightingDraws = 0;
    uint64_t skippedPostDraws = 0;
    uint64_t forwardDuplicatesBlocked = 0;
    uint64_t particlesExcluded = 0;
    uint64_t compositesRejected = 0;
    uint64_t handoffsEmitted = 0;
    uint64_t fakeVolumesHidden = 0;
    uint64_t suppressedSetters = 0;
    uint64_t flushedSetters = 0;
};

class PassGraph {
public:
    PassGraph() = default;

    void SetConfig(const PassGraphConfig& cfg) { config_ = cfg; }
    const PassGraphConfig& Config() const { return config_; }

    // Evaluates one draw call and updates the per-frame stage state machine without heap allocations.
    DrawDecision ClassifyDraw(const DrawContextInput& in);

    // Marks that the early or composite handoff marker succeeded on the device.
    void NoteHandoffCompleted();

    // Finalizes the frame before Present; returns true if a pending composite handoff must emit first.
    bool EndFrame(const void* currentSceneHdrTarget, bool raytracingActive);

    // Resets transient state on IDirect3DDevice9::Reset().
    void OnDeviceReset();

    // Returns true when D3D9 setter commands can be buffered locally instead of crossing the IPC bridge.
    bool ShouldSuppressSetters(const void* rt0,
                               bool rt0Primary,
                               bool rt0IsShadowMap,
                               D3DFORMAT rt0Format,
                               bool rt1Bound,
                               bool raytracingActive,
                               VsClass currentVsClass,
                               bool currentPsIsComposite) const;

    bool IsReplacingScene(bool raytracingActive) const;
    bool GogglesActive() const { return gogglesOn_ || gogglesThisFrame_; }
    bool GogglesDetectedThisFrame() const { return gogglesThisFrame_; }
    bool MarkerDoneThisFrame() const { return markerDone_; }
    bool PostStartedThisFrame() const { return postStarted_; }
    uint32_t SceneDrawsThisFrame() const { return sceneDrawsThisFrame_; }
    uint32_t SkinnedSceneDrawsThisFrame() const { return skinnedSceneDrawsThisFrame_; }
    uint32_t RtOnFrames() const { return rtOnFrames_; }
    FrameStage CurrentStage() const { return stage_; }
    const PassGraphStats& Stats() const { return stats_; }
    void AddSetterStats(uint64_t suppressed, uint64_t flushed) {
        stats_.suppressedSetters += suppressed;
        stats_.flushedSetters += flushed;
    }

private:
    bool LookupOrInsertGBufferGeometry(const DrawGeometryKey& key, bool insert);

    struct GeometrySlot {
        uint64_t hash = 0;
        uint32_t generation = 0;
    };
    static constexpr size_t kGeometryTableSize = 4096;

    PassGraphConfig config_ = {};
    FrameStage stage_ = FrameStage::PreScene;
    const void* litSceneTarget_ = nullptr;
    bool hasKnownSceneHdr_ = false;
    bool postStarted_ = false;
    bool markerPending_ = false;
    bool markerDone_ = false;
    bool handoffLastFrame_ = false;
    bool gogglesOn_ = false;
    bool gogglesThisFrame_ = false;
    uint32_t rtOnFrames_ = 0;
    uint32_t sceneDrawsThisFrame_ = 0;
    uint32_t skinnedSceneDrawsThisFrame_ = 0;
    uint32_t geometryGeneration_ = 1;
    size_t geometryCount_ = 0;
    GeometrySlot geometryTable_[kGeometryTableSize] = {};
    PassGraphStats stats_ = {};
};

// Coalesces D3D9 setter commands buffered while inside skipped shadow/lighting/post passes
// and replays only the net changed values before the next unsuppressed draw, handoff, or Present.
class SetterCoalescer {
public:
    static constexpr uint32_t kMaxStages = 8;
    static constexpr uint32_t kMaxRenderStates = 256;
    static constexpr uint32_t kMaxPsFloatRegs = 224;

    explicit SetterCoalescer(bool manageComRefs = false) : manageComRefs_(manageComRefs) {}
    ~SetterCoalescer() { DiscardAll(); }
    SetterCoalescer(const SetterCoalescer&) = delete;
    SetterCoalescer& operator=(const SetterCoalescer&) = delete;

    void RecordSetTexture(DWORD stage, IDirect3DBaseTexture9* tex);
    void RecordSetRenderState(D3DRENDERSTATETYPE state, DWORD value);
    void RecordSetStreamSource0(IDirect3DVertexBuffer9* vb, UINT offset, UINT stride);
    void RecordSetIndices(IDirect3DIndexBuffer9* ib);
    void RecordSetPixelShaderConstantF(UINT startReg, const float* data, UINT vec4Count);

    bool HasDirtyState() const { return dirtyAny_; }
    uint64_t TakeSuppressedCount() {
        const uint64_t n = suppressedCount_;
        suppressedCount_ = 0;
        return n;
    }

    // Replays buffered changes via raw device function pointers and returns number of calls issued.
    using SetTextureFn = HRESULT(WINAPI*)(IDirect3DDevice9*, DWORD, IDirect3DBaseTexture9*);
    using SetRenderStateFn = HRESULT(WINAPI*)(IDirect3DDevice9*, D3DRENDERSTATETYPE, DWORD);
    using SetStreamSourceFn = HRESULT(WINAPI*)(IDirect3DDevice9*, UINT, IDirect3DVertexBuffer9*, UINT, UINT);
    using SetIndicesFn = HRESULT(WINAPI*)(IDirect3DDevice9*, IDirect3DIndexBuffer9*);
    using SetPsConstFFn = HRESULT(WINAPI*)(IDirect3DDevice9*, UINT, const float*, UINT);

    uint64_t Flush(IDirect3DDevice9* device,
                   SetTextureFn setTex,
                   SetRenderStateFn setRs,
                   SetStreamSourceFn setStream,
                   SetIndicesFn setIb,
                   SetPsConstFFn setPsConst);

    void DiscardAll();

private:
    bool dirtyAny_ = false;
    bool manageComRefs_ = false;
    uint8_t dirtyTextures_ = 0;
    IDirect3DBaseTexture9* textures_[kMaxStages] = {};

    uint64_t dirtyRsMask_[kMaxRenderStates / 64] = {};
    DWORD renderStates_[kMaxRenderStates] = {};

    bool dirtyStream0_ = false;
    IDirect3DVertexBuffer9* vb0_ = nullptr;
    UINT vb0Offset_ = 0;
    UINT vb0Stride_ = 0;

    bool dirtyIb_ = false;
    IDirect3DIndexBuffer9* ib_ = nullptr;

    uint64_t dirtyPsRegs_[4] = {};
    float psConsts_[kMaxPsFloatRegs][4] = {};

    uint64_t suppressedCount_ = 0;
};

}  // namespace proxy
