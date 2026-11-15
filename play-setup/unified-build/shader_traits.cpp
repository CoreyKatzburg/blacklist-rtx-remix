#include "shader_traits.h"

#include <algorithm>
#include <cmath>
#include <cstring>

namespace proxy {
namespace {

template <typename Visitor>
void ForEachCtabConstant(const uint32_t* code, size_t wordCount, Visitor visit) {
    const uint8_t* bytes = reinterpret_cast<const uint8_t*>(code);
    for (size_t i = 1; i < wordCount;) {
        const uint32_t tok = code[i];
        if (tok == 0x0000FFFFu) break;
        if ((tok & 0xFFFFu) != 0xFFFEu) {
            i += 1 + ((tok >> 24) & 0x0Fu);
            continue;
        }
        const size_t length = (tok >> 16) & 0x7FFFu;
        if (length >= 8 && i + 1 + length <= wordCount && code[i + 1] == 0x42415443u) {  // 'CTAB'
            const size_t base = (i + 2) * 4;
            const size_t limit = (i + 1 + length) * 4;
            const uint32_t count = code[i + 5];
            const uint32_t info = code[i + 6];
            const size_t ctabBytes = limit - base;
            for (uint32_t k = 0; k < count; ++k) {
                if (info > ctabBytes || ctabBytes - info < 20 || k > (ctabBytes - info - 20) / 20) break;
                const size_t entry = base + info + static_cast<size_t>(k) * 20;
                uint32_t nameOffset = 0;
                uint16_t registerSet = 0;
                uint16_t registerIndex = 0;
                std::memcpy(&nameOffset, bytes + entry, sizeof(nameOffset));
                std::memcpy(&registerSet, bytes + entry + 4, sizeof(registerSet));
                std::memcpy(&registerIndex, bytes + entry + 6, sizeof(registerIndex));
                if (nameOffset >= ctabBytes) continue;
                const size_t namePos = base + nameOffset;
                const char* text = reinterpret_cast<const char*>(bytes + namePos);
                if (strnlen(text, limit - namePos) == limit - namePos) continue;
                visit(text, registerSet, registerIndex);
            }
            return;
        }
        i += 1 + length;
    }
}

bool ScanHasTexkill(const uint32_t* code, size_t wordCount) {
    for (size_t i = 1; i < wordCount;) {
        const uint32_t tok = code[i];
        if (tok == 0x0000FFFFu) break;
        if ((tok & 0xFFFFu) == 0xFFFEu) {
            i += 1 + ((tok >> 16) & 0x7FFFu);
            continue;
        }
        if ((tok & 0xFFFFu) == 0x0041u) return true;  // D3DSIO_TEXKILL
        i += 1 + ((tok >> 24) & 0x0Fu);
    }
    return false;
}

bool ScanUsesPackedUv(const uint32_t* code, size_t wordCount) {
    constexpr uint32_t kTexcoord0Decl = 0x80000005u;
    constexpr uint32_t kInv2048Bits = 0x3A000000u;  // 1.0f / 2048.0f
    bool hasDecl = false;
    bool hasScale = false;
    for (size_t i = 1; i < wordCount; ++i) {
        if (code[i] == kTexcoord0Decl) hasDecl = true;
        else if (code[i] == kInv2048Bits) hasScale = true;
        if (hasDecl && hasScale) return true;
    }
    return false;
}

}  // namespace

ShaderTraits AnalyzeShaderBytecode(const uint32_t* words, size_t byteSize, bool isVertex) {
    ShaderTraits traits;
    traits.vsClass = isVertex ? VsClass::Other : VsClass::None;
    if (!words || byteSize < 8) return traits;

    const size_t wordCount = byteSize / 4;
    const uint8_t* bytes = reinterpret_cast<const uint8_t*>(words);
    uint32_t fnv = 2166136261u;
    for (size_t b = 0; b < byteSize; ++b) {
        fnv ^= bytes[b];
        fnv *= 16777619u;
    }
    traits.hash = fnv;

    if (isVertex) {
        traits.usesPackedUv = ScanUsesPackedUv(words, wordCount);
    } else {
        traits.hasTexkill = ScanHasTexkill(words, wordCount);
    }

    bool world = false;
    bool screen = false;
    bool ui = false;
    ForEachCtabConstant(words, wordCount, [&](const char* name, uint16_t registerSet, uint16_t registerIndex) {
        if (!isVertex && registerSet == 0 && registerIndex < 16 && std::strcmp(name, "bNoise") == 0) {
            traits.gogglesSwitch = static_cast<uint8_t>(1 + registerIndex);
        }
        if (std::strcmp(name, "gWorldToProj") == 0 || std::strcmp(name, "cWorldToProj") == 0) world = true;
        else if (std::strcmp(name, "gSimpleWorldToProj") == 0) screen = true;
        else if (std::strcmp(name, "mvp") == 0) ui = true;
        else if (std::strcmp(name, "sSceneTexPoint") == 0 || std::strcmp(name, "fxaaFrameSize") == 0) traits.composite = true;
        else if (std::strcmp(name, "bUseVolumetricEffect") == 0) traits.volumetric = true;
        else if (std::strcmp(name, "cCorners") == 0 || std::strcmp(name, "bSoftParticles") == 0) traits.particle = true;
        else if (!isVertex && std::strcmp(name, "cThermalBaseColor") == 0) traits.thermal = true;
        else if (isVertex && std::strcmp(name, "gBones") == 0) traits.skinned = true;
        else if (isVertex && registerSet == 2 && std::strcmp(name, "cSHScale") == 0)
            traits.shScale = static_cast<uint16_t>(registerIndex + 1);

        if (isVertex) return;
        const bool isBool = (registerSet == 0 && registerIndex < 16);
        const bool isFloat4 = (registerSet == 2 && registerIndex < 0xFFFFu);
        const uint16_t stored = static_cast<uint16_t>(registerIndex + 1);
        if (isFloat4 && std::strcmp(name, "cAlphaTest") == 0) {
            traits.alphaCutoff = stored;
            traits.alphaCutoffComponent = 0;
        } else if (isFloat4 && std::strcmp(name, "cAlphaRef") == 0) {
            traits.alphaCutoff = stored;
            traits.alphaCutoffComponent = 3;
        } else if (isFloat4 && std::strcmp(name, "gCstFadeValue") == 0) {
            traits.fadeValue = stored;
        } else if (isBool && std::strcmp(name, "bAlphaTest") == 0) {
            traits.alphaTestSwitch = static_cast<uint8_t>(stored);
        } else if (isBool && std::strcmp(name, "bIsTransparent") == 0) {
            traits.transparentSwitch = static_cast<uint8_t>(stored);
        } else if (std::strcmp(name, "noiseTexture") == 0) {
            traits.dissolve = true;
        }
        else if (std::strcmp(name, "sFalloffCurve") == 0) {
            traits.closeCameraFade = true;
        }
    });

    if (isVertex) {
        traits.vsClass = ui ? VsClass::Ui : world ? VsClass::World : screen ? VsClass::Screen : VsClass::Other;
    }
    return traits;
}

bool ComputeAlphaCutoffFromConstants(const ShaderTraits& psTraits,
                                     uint32_t psBoolMask,
                                     const float (*psFloat4)[4],
                                     size_t float4Count,
                                     DWORD fallbackRef,
                                     DWORD* outRef) {
    if (psTraits.dissolve) return false;
    if (psTraits.alphaTestSwitch != 0) {
        const uint32_t bit = 1u << (psTraits.alphaTestSwitch - 1);
        if ((psBoolMask & bit) == 0) return false;
    }
    if (psTraits.transparentSwitch != 0) {
        const uint32_t bit = 1u << (psTraits.transparentSwitch - 1);
        if ((psBoolMask & bit) != 0) return false;
    }
    if (psTraits.fadeValue != 0) {
        const size_t idx = static_cast<size_t>(psTraits.fadeValue - 1);
        if (!psFloat4 || idx >= float4Count || !(psFloat4[idx][2] > 0.0f)) return false;
    }
    *outRef = fallbackRef;
    if (psTraits.alphaCutoff == 0) return true;
    const size_t cutoffIdx = static_cast<size_t>(psTraits.alphaCutoff - 1);
    if (!psFloat4 || cutoffIdx >= float4Count) return true;
    const float cutoff = psFloat4[cutoffIdx][psTraits.alphaCutoffComponent];
    if (!(cutoff > 0.01f)) return false;
    *outRef = static_cast<DWORD>(std::clamp(std::lround(255.0f * (cutoff + 0.01f)), 1L, 255L));
    return true;
}

bool QueryDeviceAlphaCutoff(IDirect3DDevice9* device,
                            const ShaderTraits& psTraits,
                            DWORD fallbackRef,
                            DWORD* outRef) {
    if (psTraits.dissolve) return false;
    BOOL on = FALSE;
    if (psTraits.alphaTestSwitch &&
        (FAILED(device->GetPixelShaderConstantB(psTraits.alphaTestSwitch - 1, &on, 1)) || !on)) {
        return false;
    }
    if (psTraits.transparentSwitch &&
        SUCCEEDED(device->GetPixelShaderConstantB(psTraits.transparentSwitch - 1, &on, 1)) && on) {
        return false;
    }
    float value[4] = {};
    if (psTraits.fadeValue &&
        (FAILED(device->GetPixelShaderConstantF(psTraits.fadeValue - 1, value, 1)) || !(value[2] > 0.0f))) {
        return false;
    }
    *outRef = fallbackRef;
    if (!psTraits.alphaCutoff || FAILED(device->GetPixelShaderConstantF(psTraits.alphaCutoff - 1, value, 1))) {
        return true;
    }
    const float cutoff = value[psTraits.alphaCutoffComponent];
    if (!(cutoff > 0.01f)) return false;
    *outRef = static_cast<DWORD>(std::clamp(std::lround(255.0f * (cutoff + 0.01f)), 1L, 255L));
    return true;
}

}  // namespace proxy
