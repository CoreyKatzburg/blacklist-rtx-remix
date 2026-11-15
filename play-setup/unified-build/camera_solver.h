#pragma once
// Pure view-projection matrix factorization and per-frame primary camera gate.
// Rejects 1:1 spotlight shadow-map camera uploads on widescreen displays and enforces
// one primary gameplay camera publication per frame.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d9.h>
#include <cstdint>

namespace proxy {

constexpr uintptr_t kPureCameraUploadReturnRva = 0x0160A972u;
constexpr uintptr_t kGameplayCameraUploadReturnRva = 0x015D8E71u;
constexpr uintptr_t kObjectWvpUploadReturnRva = 0x015B0567u;

struct CameraRejection {
    const char* check = "none";
    double value = 0.0;
    double limit = 0.0;
};

inline bool IsDirectCameraUploadReturn(uintptr_t callerRva) {
    return callerRva == kPureCameraUploadReturnRva || callerRva == kGameplayCameraUploadReturnRva;
}

int CountNonFiniteMatrix(const D3DMATRIX& matrix);

// Factors a row-vector perspective product (view * projection) into orthonormal view and projection.
bool SplitViewProjection(const D3DMATRIX& viewProjection,
                         D3DMATRIX* view,
                         D3DMATRIX* projection,
                         CameraRejection* rejection = nullptr);

// Diagnostic recovery of world, view, projection from c5..c11.
bool RecoverTransformsFromConstants(const float constants[12][4],
                                    D3DMATRIX* world,
                                    D3DMATRIX* view,
                                    D3DMATRIX* projection,
                                    CameraRejection* rejection = nullptr);

// True when a recovered projection is a square 1:1 shadow-map camera on a widescreen display.
bool IsSquareShadowMapCamera(const D3DMATRIX& projection, uint32_t displayWidth, uint32_t displayHeight);

class CameraFrameTracker {
public:
    void BeginFrame(uint32_t displayWidth, uint32_t displayHeight);

    // Evaluates a direct 4-vector c8..c11 camera upload and decides whether to publish SetTransform.
    // Returns true when view/projection factored cleanly; *outShouldPublish is true only for the
    // frame's primary gameplay camera (never for secondary spotlight shadow-map uploads).
    bool EvaluateDirectUpload(const float c8To11[4][4],
                              bool oncePerFrame,
                              D3DMATRIX* outView,
                              D3DMATRIX* outProjection,
                              bool* outShouldPublish,
                              CameraRejection* outRejection = nullptr);

    bool HasProjection() const { return hasProjection_; }
    const D3DMATRIX& LastProjection() const { return lastProjection_; }

private:
    uint32_t displayWidth_ = 0;
    uint32_t displayHeight_ = 0;
    bool publishedThisFrame_ = false;
    bool hasProjection_ = false;
    D3DMATRIX lastProjection_ = {};
};

}  // namespace proxy
