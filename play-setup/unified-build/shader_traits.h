#pragma once
// Pure Direct3D 9 shader bytecode and CTAB constant-table analyzer.
// Contains no global D3D9 state so both d3d9.dll and offline verify_proxy_tests.exe
// run identical classification and alpha-cutoff math on dumped .bin shaders.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d9.h>
#include <cstddef>
#include <cstdint>

namespace proxy {

enum class VsClass : uint8_t {
    None = 0,
    World = 1,
    Screen = 2,
    Ui = 3,
    Other = 4,
};

struct ShaderTraits {
    uint32_t hash = 0;
    VsClass vsClass = VsClass::None;
    bool composite = false;
    bool volumetric = false;
    bool particle = false;
    bool thermal = false;
    bool dissolve = false;
    bool skinned = false;
    bool hasTexkill = false;
    bool usesPackedUv = false;
    bool closeCameraFade = false;
    uint8_t gogglesSwitch = 0;        // 1 + bool register of PS bNoise
    uint8_t alphaCutoffComponent = 0; // 0 for cAlphaTest.x, 3 for cAlphaRef.w
    uint8_t alphaTestSwitch = 0;      // 1 + bool register of PS bAlphaTest
    uint8_t transparentSwitch = 0;    // 1 + bool register of PS bIsTransparent
    uint16_t alphaCutoff = 0;         // 1 + float4 register of cAlphaTest / cAlphaRef
    uint16_t fadeValue = 0;           // 1 + float4 register of gCstFadeValue
    uint16_t shScale = 0;             // VS: 1 + float4 register of cSHScale (the engine's SH ambient multiplier)
};

// Parses D3D9 SM2/SM3 bytecode tokens and CTAB constant names into a pure ShaderTraits value.
ShaderTraits AnalyzeShaderBytecode(const uint32_t* words, size_t byteSize, bool isVertex);

// Evaluates Blacklist's per-draw texkill cutoff from raw pixel-shader constants.
// Returns true and writes [1..255] to *outRef when the shader discards cutout texels on this draw.
bool ComputeAlphaCutoffFromConstants(const ShaderTraits& psTraits,
                                     uint32_t psBoolMask,
                                     const float (*psFloat4)[4],
                                     size_t float4Count,
                                     DWORD fallbackRef,
                                     DWORD* outRef);

// Queries only the needed pixel-shader registers from a live IDirect3DDevice9 for GameCutoff.
bool QueryDeviceAlphaCutoff(IDirect3DDevice9* device,
                            const ShaderTraits& psTraits,
                            DWORD fallbackRef,
                            DWORD* outRef);

}  // namespace proxy
