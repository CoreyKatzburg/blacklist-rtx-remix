#include "camera_solver.h"

#include <cfloat>
#include <cmath>

namespace proxy {
namespace {

float Dot3(const float a[3], const float b[3]) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

float Length3(const float v[3]) {
    return std::sqrt(Dot3(v, v));
}

bool Reject(CameraRejection* info, const char* check, double value, double limit) {
    if (info) {
        info->check = check;
        info->value = value;
        info->limit = limit;
    }
    return false;
}

void Multiply(const D3DMATRIX& left, const D3DMATRIX& right, D3DMATRIX* output) {
    D3DMATRIX result = {};
    for (int row = 0; row < 4; ++row) {
        for (int col = 0; col < 4; ++col) {
            result.m[row][col] =
                left.m[row][0] * right.m[0][col] +
                left.m[row][1] * right.m[1][col] +
                left.m[row][2] * right.m[2][col] +
                left.m[row][3] * right.m[3][col];
        }
    }
    *output = result;
}

bool Invert4x4(const D3DMATRIX& input, D3DMATRIX* output, float* failedPivot = nullptr) {
    double aug[4][8] = {};
    for (int r = 0; r < 4; ++r) {
        for (int c = 0; c < 4; ++c) {
            if (!_finite(input.m[r][c])) return false;
            aug[r][c] = input.m[r][c];
        }
        aug[r][4 + r] = 1.0;
    }
    for (int col = 0; col < 4; ++col) {
        int pivotRow = col;
        double pivotAbs = std::fabs(aug[col][col]);
        for (int r = col + 1; r < 4; ++r) {
            const double cand = std::fabs(aug[r][col]);
            if (cand > pivotAbs) {
                pivotAbs = cand;
                pivotRow = r;
            }
        }
        if (pivotAbs < 1.0e-7) {
            if (failedPivot) *failedPivot = static_cast<float>(pivotAbs);
            return false;
        }
        if (pivotRow != col) {
            for (int k = 0; k < 8; ++k) {
                const double tmp = aug[col][k];
                aug[col][k] = aug[pivotRow][k];
                aug[pivotRow][k] = tmp;
            }
        }
        const double pivot = aug[col][col];
        for (int k = 0; k < 8; ++k) aug[col][k] /= pivot;
        for (int r = 0; r < 4; ++r) {
            if (r == col) continue;
            const double factor = aug[r][col];
            for (int k = 0; k < 8; ++k) aug[r][k] -= factor * aug[col][k];
        }
    }
    for (int r = 0; r < 4; ++r) {
        for (int c = 0; c < 4; ++c) {
            output->m[r][c] = static_cast<float>(aug[r][4 + c]);
        }
    }
    return CountNonFiniteMatrix(*output) == 0;
}

}  // namespace

int CountNonFiniteMatrix(const D3DMATRIX& matrix) {
    int count = 0;
    for (int r = 0; r < 4; ++r) {
        for (int c = 0; c < 4; ++c) {
            if (!_finite(matrix.m[r][c])) ++count;
        }
    }
    return count;
}

bool SplitViewProjection(const D3DMATRIX& viewProjection,
                         D3DMATRIX* view,
                         D3DMATRIX* projection,
                         CameraRejection* rejection) {
    float x[3];
    float y[3];
    float z[3] = {
        viewProjection.m[0][3],
        viewProjection.m[1][3],
        viewProjection.m[2][3],
    };
    const float zLength = Length3(z);
    const float zLengthError = std::fabs(zLength - 1.0f);
    if (zLengthError > 0.03f) {
        return Reject(rejection, "view-z unit error (must <=)", zLengthError, 0.03f);
    }
    for (float& value : z) value /= zLength;

    const float column0[3] = {viewProjection.m[0][0], viewProjection.m[1][0], viewProjection.m[2][0]};
    const float column1[3] = {viewProjection.m[0][1], viewProjection.m[1][1], viewProjection.m[2][1]};
    const float column2[3] = {viewProjection.m[0][2], viewProjection.m[1][2], viewProjection.m[2][2]};

    float offsetX = Dot3(column0, z);
    float offsetY = Dot3(column1, z);
    for (int i = 0; i < 3; ++i) {
        x[i] = column0[i] - offsetX * z[i];
        y[i] = column1[i] - offsetY * z[i];
    }
    const float scaleX = Length3(x);
    const float scaleY = Length3(y);
    const float minAxisScale = scaleX < scaleY ? scaleX : scaleY;
    if (minAxisScale < 1.0e-4f) {
        return Reject(rejection, "projection axis scale (must >=)", minAxisScale, 1.0e-4f);
    }
    for (int i = 0; i < 3; ++i) {
        x[i] /= scaleX;
        y[i] /= scaleY;
    }

    const float dotXY = std::fabs(Dot3(x, y));
    const float dotXZ = std::fabs(Dot3(x, z));
    const float dotYZ = std::fabs(Dot3(y, z));
    float maxAxisDot = dotXY;
    if (dotXZ > maxAxisDot) maxAxisDot = dotXZ;
    if (dotYZ > maxAxisDot) maxAxisDot = dotYZ;
    if (maxAxisDot > 0.03f) {
        return Reject(rejection, "orthonormal-axis max dot (must <=)", maxAxisDot, 0.03f);
    }

    float handedness =
        (x[1] * y[2] - x[2] * y[1]) * z[0] +
        (x[2] * y[0] - x[0] * y[2]) * z[1] +
        (x[0] * y[1] - x[1] * y[0]) * z[2];
    float projectionSign = 1.0f;
    if (handedness < 0.0f) {
        projectionSign = -1.0f;
        for (float& value : z) value = -value;
        offsetX = -offsetX;
        offsetY = -offsetY;
        handedness = -handedness;
    }
    if (handedness < 0.97f) {
        return Reject(rejection, "proper view-basis determinant (must >=)", handedness, 0.97f);
    }

    const float depthScale = Dot3(column2, z);
    const float translationZ = projectionSign * viewProjection.m[3][3];
    const float depthOffset = viewProjection.m[3][2] - depthScale * translationZ;
    const float signedDepthScale = projectionSign * depthScale;
    bool reverseDepth = false;
    if (depthOffset < -1.0e-6f) {
        if (!(signedDepthScale > 0.5f)) {
            return Reject(rejection, "conventional signed depth scale (must >)", signedDepthScale, 0.5f);
        }
    } else if (depthOffset > 1.0e-6f) {
        reverseDepth = true;
        if (signedDepthScale > 1.0e-6f) {
            return Reject(rejection, "reverse signed depth scale (must <=)", signedDepthScale, 1.0e-6f);
        }
    } else {
        return Reject(rejection, "perspective depth-offset magnitude (must >)", std::fabs(depthOffset), 1.0e-6f);
    }

    const float nearPlane = reverseDepth
        ? depthOffset / (1.0f - signedDepthScale)
        : -depthOffset / signedDepthScale;
    if (!_finite(nearPlane) || nearPlane <= 1.0e-6f) {
        return Reject(rejection, "perspective near plane (must >)", nearPlane, 1.0e-6f);
    }
    const float farDenom = reverseDepth ? signedDepthScale : (1.0f - signedDepthScale);
    if (std::fabs(farDenom) > 1.0e-7f) {
        const float farPlane = reverseDepth ? -depthOffset / farDenom : depthOffset / farDenom;
        if (!_finite(farPlane) || farPlane <= nearPlane) {
            return Reject(rejection, "perspective far-minus-near (must >)", farPlane - nearPlane, 0.0f);
        }
    }

    D3DMATRIX recoveredView = {};
    for (int r = 0; r < 3; ++r) {
        recoveredView.m[r][0] = x[r];
        recoveredView.m[r][1] = y[r];
        recoveredView.m[r][2] = z[r];
    }
    recoveredView.m[3][0] = (viewProjection.m[3][0] - offsetX * translationZ) / scaleX;
    recoveredView.m[3][1] = (viewProjection.m[3][1] - offsetY * translationZ) / scaleY;
    recoveredView.m[3][2] = translationZ;
    recoveredView.m[3][3] = 1.0f;

    D3DMATRIX shaderProjection = {};
    shaderProjection.m[0][0] = scaleX;
    shaderProjection.m[1][1] = scaleY;
    shaderProjection.m[2][0] = offsetX;
    shaderProjection.m[2][1] = offsetY;
    shaderProjection.m[2][2] = depthScale;
    shaderProjection.m[2][3] = projectionSign;
    shaderProjection.m[3][2] = depthOffset;

    D3DMATRIX check;
    Multiply(recoveredView, shaderProjection, &check);
    float maxErr = 0.0f;
    for (int r = 0; r < 4; ++r) {
        for (int c = 0; c < 4; ++c) {
            const float err = std::fabs(check.m[r][c] - viewProjection.m[r][c]);
            if (err > maxErr) maxErr = err;
        }
    }
    if (maxErr > 0.02f) {
        return Reject(rejection, "recomposition max error (must <=)", maxErr, 0.02f);
    }
    const int nonFinite = CountNonFiniteMatrix(recoveredView) + CountNonFiniteMatrix(shaderProjection);
    if (nonFinite != 0) {
        return Reject(rejection, "recovered non-finite count (must <=)", nonFinite, 0.0);
    }

    *view = recoveredView;
    *projection = shaderProjection;
    return true;
}

bool RecoverTransformsFromConstants(const float constants[12][4],
                                    D3DMATRIX* world,
                                    D3DMATRIX* view,
                                    D3DMATRIX* projection,
                                    CameraRejection* rejection) {
    D3DMATRIX recoveredWorld = {};
    for (int col = 0; col < 3; ++col) {
        for (int row = 0; row < 4; ++row) {
            recoveredWorld.m[row][col] = constants[5 + col][row];
        }
    }
    recoveredWorld.m[3][3] = 1.0f;

    D3DMATRIX wvp = {};
    for (int col = 0; col < 4; ++col) {
        for (int row = 0; row < 4; ++row) {
            wvp.m[row][col] = constants[8 + col][row];
        }
    }
    const int nonFinite = CountNonFiniteMatrix(recoveredWorld) + CountNonFiniteMatrix(wvp);
    if (nonFinite != 0) {
        return Reject(rejection, "input non-finite count (must <=)", nonFinite, 0.0);
    }
    D3DMATRIX invWorld = {};
    float failedPivot = 0.0f;
    if (!Invert4x4(recoveredWorld, &invWorld, &failedPivot)) {
        return Reject(rejection, "world inverse pivot (must >=)", failedPivot, 1.0e-7);
    }
    D3DMATRIX vp = {};
    Multiply(invWorld, wvp, &vp);
    if (!SplitViewProjection(vp, view, projection, rejection)) return false;
    *world = recoveredWorld;
    return true;
}

bool IsSquareShadowMapCamera(const D3DMATRIX& projection, uint32_t displayWidth, uint32_t displayHeight) {
    if (displayWidth == 0 || displayHeight == 0) return false;
    const float displayAspect = static_cast<float>(displayWidth) / static_cast<float>(displayHeight);
    if (displayAspect < 1.15f) return false;  // Not a widescreen display
    if (std::fabs(projection.m[0][0]) < 1.0e-5f) return true;
    const float projRatio = std::fabs(projection.m[1][1] / projection.m[0][0]);
    // Widescreen gameplay cameras have m[1][1] / m[0][0] == aspect (~1.7778 at 16:9);
    // spotlight shadow-map uploads at 0x015D8E71 have 1:1 square frustums (projRatio == 1.0).
    return std::fabs(projRatio - 1.0f) < 0.05f;
}

void CameraFrameTracker::BeginFrame(uint32_t displayWidth, uint32_t displayHeight) {
    displayWidth_ = displayWidth;
    displayHeight_ = displayHeight;
    publishedThisFrame_ = false;
}

bool CameraFrameTracker::EvaluateDirectUpload(const float c8To11[4][4],
                                              bool oncePerFrame,
                                              D3DMATRIX* outView,
                                              D3DMATRIX* outProjection,
                                              bool* outShouldPublish,
                                              CameraRejection* outRejection) {
    *outShouldPublish = false;
    D3DMATRIX vp = {};
    for (int col = 0; col < 4; ++col) {
        for (int row = 0; row < 4; ++row) {
            vp.m[row][col] = c8To11[col][row];
        }
    }
    const int nonFinite = CountNonFiniteMatrix(vp);
    if (nonFinite != 0) {
        return Reject(outRejection, "pure VP non-finite count (must <=)", nonFinite, 0.0);
    }
    if (!SplitViewProjection(vp, outView, outProjection, outRejection)) {
        return false;
    }
    if (IsSquareShadowMapCamera(*outProjection, displayWidth_, displayHeight_)) {
        // Spotlight shadow camera uploaded via gameplay RVA mid-frame: never overwrite Remix main camera.
        return Reject(outRejection, "square 1:1 shadow-map projection on widescreen", 1.0, 1.15);
    }
    const bool skip = oncePerFrame && publishedThisFrame_;
    publishedThisFrame_ = true;
    if (!skip) {
        lastProjection_ = *outProjection;
        hasProjection_ = true;
        *outShouldPublish = true;
    }
    return true;
}

}  // namespace proxy
