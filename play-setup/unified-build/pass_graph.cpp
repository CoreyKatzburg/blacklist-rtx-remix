#include "pass_graph.h"

#include <cstring>

#define XXH_INLINE_ALL
#include "third_party/xxhash-0.8.2/xxhash.h"

#include <intrin.h>

namespace {
inline uint32_t CountTrailingZeros64(uint64_t mask) {
    unsigned long bit = 0;
    const uint32_t lo = static_cast<uint32_t>(mask);
    if (lo != 0) {
        _BitScanForward(&bit, lo);
        return static_cast<uint32_t>(bit);
    }
    _BitScanForward(&bit, static_cast<uint32_t>(mask >> 32));
    return 32u + static_cast<uint32_t>(bit);
}
}  // namespace

namespace proxy {

bool PassGraph::LookupOrInsertGBufferGeometry(const DrawGeometryKey& key, bool insert) {
    const uint64_t hash = XXH3_64bits(&key, sizeof(key));
    for (size_t probe = 0, idx = static_cast<size_t>(hash) & (kGeometryTableSize - 1);
         probe < kGeometryTableSize;
         ++probe, idx = (idx + 1) & (kGeometryTableSize - 1)) {
        GeometrySlot& slot = geometryTable_[idx];
        if (slot.generation != geometryGeneration_) {
            if (!insert || geometryCount_ >= kGeometryTableSize / 2) return false;
            slot = {hash, geometryGeneration_};
            ++geometryCount_;
            return true;
        }
        if (slot.hash == hash) return true;
    }
    return false;
}

// Both hand-off modes overwrite the game's lit scene (HandoffBeforePost decodes the ray-traced image into it), so
// the lighting pass is wasted in both. HandoffBeforePost only keeps the game's post-processing, which runs after its
// hand-off and is never skipped (ClassifyDraw returns before the skip rules once the marker is done).
bool PassGraph::IsReplacingScene(bool raytracingActive) const {
    return config_.enabled &&
           config_.skipGameLighting &&
           handoffLastFrame_ &&
           rtOnFrames_ >= 2 &&
           raytracingActive &&
           !gogglesOn_ &&
           !gogglesThisFrame_;
}

DrawDecision PassGraph::ClassifyDraw(const DrawContextInput& in) {
    DrawDecision out = {};
    if (!config_.enabled) {
        out.stage = FrameStage::PreScene;
        out.isScene = true;
        out.openBracket = false;
        return out;
    }

    if (config_.gogglesGameView && !gogglesThisFrame_) {
        if (in.ps.thermal || (in.ps.gogglesSwitch != 0 && in.gogglesSwitchActive)) {
            gogglesThisFrame_ = true;
        }
    }

    if (markerPending_ && !in.ps.composite) {
        out.emitCompositeHandoffBeforeDraw = true;
        markerPending_ = false;
    }

    // The game's fake light shafts (beam cards, bUseVolumetricEffect) are replaced by Remix's volumetric light
    // whenever the ray-traced image is shown, including after a HandoffBeforeEffects hand-off, where the game would
    // otherwise draw them over it.
    const bool hideFakeVolume = in.ps.volumetric && IsReplacingScene(in.raytracingActive);
    if (hideFakeVolume) ++stats_.fakeVolumesHidden;

    if (markerDone_ || out.emitCompositeHandoffBeforeDraw) {
        stage_ = FrameStage::CompositeAndUi;
        out.stage = FrameStage::CompositeAndUi;
        out.isScene = false;
        out.skipDraw = hideFakeVolume;
        out.openBracket = false;  // Remix natively rasterizes all draws after UI handoff
        return out;
    }

    if (in.rt0IsShadowMap) {
        stage_ = FrameStage::ShadowMap;
        out.stage = FrameStage::ShadowMap;
        out.isScene = false;
        out.skipDraw = config_.skipShadowMapDraws;
        out.openBracket = true;
        if (out.skipDraw) ++stats_.skippedShadowDraws;
        return out;
    }

    // Depth pre-pass draws world geometry with color write disabled; never trace or treat as scene HDR.
    if (in.rt0Primary && in.vs.vsClass == VsClass::World && in.depthTestEnabled && !in.colorWriteEnabled) {
        stage_ = FrameStage::DepthPrePass;
        out.stage = FrameStage::DepthPrePass;
        out.isScene = false;
        out.skipDraw = false;
        out.openBracket = true;
        return out;
    }

    const bool worldCandidate = in.rt0Primary &&
                                in.vs.vsClass == VsClass::World &&
                                !in.ps.volumetric &&
                                (in.rt1Bound || in.rt0Format == D3DFMT_A16B16G16R16F) &&
                                in.depthTestEnabled &&
                                in.colorWriteEnabled;

    const bool isForwardWorld = worldCandidate && !in.rt1Bound;
    if (isForwardWorld) {
        hasKnownSceneHdr_ = true;
        out.rememberSceneHdrTarget = true;
    }

    const bool isParticle = isForwardWorld && (in.vs.particle || in.ps.particle);
    if (isParticle && !config_.raytraceParticles) ++stats_.particlesExcluded;

    bool isForwardDuplicate = false;
    bool scene = worldCandidate && (in.rt1Bound || (config_.forwardScene && (config_.raytraceParticles || !isParticle)));
    if (scene && in.geometry) {
        if (in.rt1Bound) {
            LookupOrInsertGBufferGeometry(*in.geometry, true);
        } else if (LookupOrInsertGBufferGeometry(*in.geometry, false)) {
            scene = false;
            isForwardDuplicate = true;
            ++stats_.forwardDuplicatesBlocked;
        }
    } else if (!scene && isForwardWorld && !isParticle && in.geometry) {
        // Even if forwardScene is disabled, detect whether this mesh is a G-buffer duplicate.
        isForwardDuplicate = LookupOrInsertGBufferGeometry(*in.geometry, false);
    }

    const bool readsScene = !scene && hasKnownSceneHdr_ && in.stage0ReadsSceneHdr;
    if (readsScene) {
        postStarted_ = true;
    }

    if (readsScene && config_.handoffBeforePost && !config_.modernFilter) {
        out.tryEarlyHandoffBeforeDraw = true;
    } else if (!scene && !isForwardDuplicate && config_.handoffBeforeEffects && config_.handoffBeforePost &&
               hasKnownSceneHdr_ && in.rt0Format == D3DFMT_A16B16G16R16F && !in.rt1Bound &&
               in.alphaBlendEnabled && !in.depthWriteEnabled && !in.ps.volumetric) {
        out.tryEarlyHandoffBeforeDraw = true;
    }

    if (in.ps.composite && in.rt0 && in.rt0Primary && sceneDrawsThisFrame_ > 0) {
        if (!postStarted_ && hasKnownSceneHdr_) {
            ++stats_.compositesRejected;
        } else {
            out.latchCompositeTarget = true;
            markerPending_ = true;
        }
    }

    // Determine explicit frame stage
    if (scene && in.rt1Bound) {
        stage_ = FrameStage::GBuffer;
    } else if (isForwardWorld) {
        stage_ = FrameStage::ForwardScene;
    } else if (in.ps.composite && markerPending_) {
        stage_ = FrameStage::CompositeAndUi;
    } else if (postStarted_) {
        stage_ = FrameStage::PostProcess;
    } else if (!in.rt1Bound && in.rt0Primary && in.rt0Format == D3DFMT_A16B16G16R16F) {
        stage_ = FrameStage::DeferredLighting;
    }

    // Skip wasted game lighting and post-processing passes when Remix replaces the lit scene.
    // Note: forward-only non-duplicate world draws (e.g., hair/eyelashes/glass) are never wasted,
    // even if ForwardScene=0 rasterizes them!
    const bool replacing = IsReplacingScene(in.raytracingActive);
    bool wastedLighting = false;
    bool wastedPost = false;
    if (!scene && replacing && in.rt0Primary) {
        const bool isForwardUniqueWorld = isForwardWorld && !isForwardDuplicate && (config_.raytraceParticles || !isParticle);
        if (!out.tryEarlyHandoffBeforeDraw && !isForwardUniqueWorld && !postStarted_ &&
            in.rt0 == litSceneTarget_ && in.rt0Format == D3DFMT_A16B16G16R16F && !in.rt1Bound) {
            wastedLighting = true;
            ++stats_.skippedLightingDraws;
        } else if (config_.skipGamePost && sceneDrawsThisFrame_ > 0 &&
                   (!config_.handoffBeforePost || config_.modernFilter) &&
                   !in.ps.composite && in.colorWriteEnabled && !isForwardUniqueWorld) {
            wastedPost = true;
            ++stats_.skippedPostDraws;
        }
    }

    out.stage = stage_;
    out.isScene = scene;
    out.skipDraw = wastedLighting || wastedPost || hideFakeVolume;
    out.openBracket = !scene;

    if (scene) {
        ++sceneDrawsThisFrame_;
        if (in.vs.skinned) ++skinnedSceneDrawsThisFrame_;
    }
    return out;
}

void PassGraph::NoteHandoffCompleted() {
    markerPending_ = false;
    markerDone_ = true;
    stage_ = FrameStage::CompositeAndUi;
    ++stats_.handoffsEmitted;
}

bool PassGraph::EndFrame(const void* currentSceneHdrTarget, bool raytracingActive) {
    const bool needFinalHandoff = config_.enabled && markerPending_ && !markerDone_ && sceneDrawsThisFrame_ > 0;
    if (currentSceneHdrTarget) {
        litSceneTarget_ = currentSceneHdrTarget;
    }
    gogglesOn_ = gogglesThisFrame_;
    gogglesThisFrame_ = false;

    const bool handoffHappened = (markerDone_ || needFinalHandoff);
    rtOnFrames_ = raytracingActive ? (rtOnFrames_ + 1) : 0;
    handoffLastFrame_ = handoffHappened && raytracingActive;

    if (sceneDrawsThisFrame_ > 0) {
        ++stats_.sceneFrames;
        stats_.sceneDraws += sceneDrawsThisFrame_;
    }

    stage_ = FrameStage::PreScene;
    hasKnownSceneHdr_ = false;
    postStarted_ = false;
    markerPending_ = false;
    markerDone_ = false;
    sceneDrawsThisFrame_ = 0;
    skinnedSceneDrawsThisFrame_ = 0;
    if (++geometryGeneration_ == 0) geometryGeneration_ = 1;
    geometryCount_ = 0;
    return needFinalHandoff;
}

void PassGraph::OnDeviceReset() {
    litSceneTarget_ = nullptr;
    hasKnownSceneHdr_ = false;
    postStarted_ = false;
    markerPending_ = false;
    markerDone_ = false;
    sceneDrawsThisFrame_ = 0;
    skinnedSceneDrawsThisFrame_ = 0;
    stage_ = FrameStage::PreScene;
}

bool PassGraph::ShouldSuppressSetters(const void* rt0,
                                      bool rt0Primary,
                                      bool rt0IsShadowMap,
                                      D3DFORMAT rt0Format,
                                      bool rt1Bound,
                                      bool raytracingActive,
                                      VsClass currentVsClass,
                                      bool currentPsIsComposite) const {
    if (!config_.enabled || markerDone_) return false;
    if (rt0IsShadowMap && config_.skipShadowMapDraws) return true;
    if (!IsReplacingScene(raytracingActive)) return false;

    // Inside deferred light accumulation (200+ draws on litSceneTarget with non-world VS)
    if (!postStarted_ && rt0 != nullptr && rt0 == litSceneTarget_ &&
        rt0Format == D3DFMT_A16B16G16R16F && !rt1Bound && currentVsClass != VsClass::World) {
        return true;
    }
    // Inside skipped post-processing chain before final composite
    if (config_.skipGamePost && postStarted_ && !markerPending_ &&
        rt0Primary && !currentPsIsComposite && sceneDrawsThisFrame_ > 0) {
        return true;
    }
    return false;
}

void SetterCoalescer::RecordSetTexture(DWORD stage, IDirect3DBaseTexture9* tex) {
    if (stage >= kMaxStages) return;
    if (manageComRefs_) {
        if (tex) tex->AddRef();
        if (textures_[stage]) textures_[stage]->Release();
    }
    textures_[stage] = tex;
    dirtyTextures_ |= static_cast<uint8_t>(1u << stage);
    dirtyAny_ = true;
    ++suppressedCount_;
}

void SetterCoalescer::RecordSetRenderState(D3DRENDERSTATETYPE state, DWORD value) {
    const uint32_t idx = static_cast<uint32_t>(state);
    if (idx >= kMaxRenderStates) return;
    renderStates_[idx] = value;
    dirtyRsMask_[idx / 64] |= (1ull << (idx % 64));
    dirtyAny_ = true;
    ++suppressedCount_;
}

void SetterCoalescer::RecordSetStreamSource0(IDirect3DVertexBuffer9* vb, UINT offset, UINT stride) {
    if (manageComRefs_) {
        if (vb) vb->AddRef();
        if (vb0_) vb0_->Release();
    }
    vb0_ = vb;
    vb0Offset_ = offset;
    vb0Stride_ = stride;
    dirtyStream0_ = true;
    dirtyAny_ = true;
    ++suppressedCount_;
}

void SetterCoalescer::RecordSetIndices(IDirect3DIndexBuffer9* ib) {
    if (manageComRefs_) {
        if (ib) ib->AddRef();
        if (ib_) ib_->Release();
    }
    ib_ = ib;
    dirtyIb_ = true;
    dirtyAny_ = true;
    ++suppressedCount_;
}

void SetterCoalescer::RecordSetPixelShaderConstantF(UINT startReg, const float* data, UINT vec4Count) {
    if (!data || startReg >= kMaxPsFloatRegs) return;
    const UINT limit = (startReg + vec4Count > kMaxPsFloatRegs) ? (kMaxPsFloatRegs - startReg) : vec4Count;
    for (UINT i = 0; i < limit; ++i) {
        const UINT r = startReg + i;
        std::memcpy(psConsts_[r], data + i * 4, sizeof(float) * 4);
        dirtyPsRegs_[r / 64] |= (1ull << (r % 64));
    }
    dirtyAny_ = true;
    ++suppressedCount_;
}

uint64_t SetterCoalescer::Flush(IDirect3DDevice9* device,
                                SetTextureFn setTex,
                                SetRenderStateFn setRs,
                                SetStreamSourceFn setStream,
                                SetIndicesFn setIb,
                                SetPsConstFFn setPsConst) {
    if (!dirtyAny_) return 0;
    uint64_t flushed = 0;

    if (dirtyTextures_ && setTex) {
        for (DWORD s = 0; s < kMaxStages; ++s) {
            if (dirtyTextures_ & (1u << s)) {
                setTex(device, s, textures_[s]);
                ++flushed;
            }
        }
    }

    if (setRs) {
        for (uint32_t word = 0; word < kMaxRenderStates / 64; ++word) {
            uint64_t mask = dirtyRsMask_[word];
            while (mask != 0) {
                const uint32_t bit = CountTrailingZeros64(mask);
                const uint32_t rs = word * 64 + bit;
                setRs(device, static_cast<D3DRENDERSTATETYPE>(rs), renderStates_[rs]);
                ++flushed;
                mask &= (mask - 1);
            }
        }
    }

    if (dirtyStream0_ && setStream) {
        setStream(device, 0, vb0_, vb0Offset_, vb0Stride_);
        ++flushed;
    }

    if (dirtyIb_ && setIb) {
        setIb(device, ib_);
        ++flushed;
    }

    if (setPsConst) {
        // Coalesce contiguous dirty PS float4 registers into single upload spans
        UINT runStart = 0;
        UINT runLen = 0;
        for (UINT r = 0; r <= kMaxPsFloatRegs; ++r) {
            const bool isDirty = (r < kMaxPsFloatRegs) && ((dirtyPsRegs_[r / 64] & (1ull << (r % 64))) != 0);
            if (isDirty) {
                if (runLen == 0) runStart = r;
                ++runLen;
            } else if (runLen > 0) {
                setPsConst(device, runStart, psConsts_[runStart], runLen);
                ++flushed;
                runLen = 0;
            }
        }
    }

    DiscardAll();
    return flushed;
}

void SetterCoalescer::DiscardAll() {
    if (manageComRefs_) {
        for (DWORD s = 0; s < kMaxStages; ++s) {
            if (textures_[s]) { textures_[s]->Release(); textures_[s] = nullptr; }
        }
        if (vb0_) { vb0_->Release(); vb0_ = nullptr; }
        if (ib_) { ib_->Release(); ib_ = nullptr; }
    }
    dirtyAny_ = false;
    dirtyTextures_ = 0;
    std::memset(dirtyRsMask_, 0, sizeof(dirtyRsMask_));
    dirtyStream0_ = false;
    dirtyIb_ = false;
    std::memset(dirtyPsRegs_, 0, sizeof(dirtyPsRegs_));
}

}  // namespace proxy
