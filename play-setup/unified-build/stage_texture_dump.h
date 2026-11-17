#pragma once
// Stage texture dump (worker E, row 20): on the framedump frame, the proxy writes the top mip of chosen texture stages
// as .dds for draws whose pixel shader is listed. Off by default:
//   [Proxy] DumpStageTexturesPs=BF48978C,4BAFE381     pixel shader ids as in the draw dump's ps= field
//   [Proxy] DumpStageTexturesStages=0,1,2,12          stages 0..15
// Only managed (pool 1) and systemmem (pool 2) 2D textures in DXT1..DXT5, A8R8G8B8 or X8R8G8B8 are written; anything
// else is reported and skipped. Level 0 is read through GetSurfaceLevel and IDirect3DSurface9::LockRect with
// D3DLOCK_READONLY: the proxy hooks only IDirect3DTexture9::LockRect, so the texture fill counters (lockN) and the
// texture stats do not see this read. tests\hitch\run_stage_texture_dump_test.ps1 checks the writer on a real device.
#include <windows.h>
#include <d3d9.h>

#include <cstdint>
#include <cstring>
#include <cwchar>
#include <vector>

namespace proxy {

inline std::vector<uint32_t> g_stageDumpPs;
inline uint32_t g_stageDumpStages = 0;  // bit N = stage N

inline void SetStageTextureDump(const wchar_t* psList, const wchar_t* stageList) {
    g_stageDumpPs.clear();
    g_stageDumpStages = 0;
    for (const wchar_t* p = psList; p && *p;) {
        wchar_t* end = nullptr;
        const unsigned long value = std::wcstoul(p, &end, 16);
        if (end == p) { ++p; continue; }
        g_stageDumpPs.push_back(static_cast<uint32_t>(value));
        p = end;
    }
    for (const wchar_t* p = stageList; p && *p;) {
        wchar_t* end = nullptr;
        const unsigned long value = std::wcstoul(p, &end, 10);
        if (end == p) { ++p; continue; }
        if (value < 16) g_stageDumpStages |= 1u << value;
        p = end;
    }
    if (g_stageDumpPs.empty()) g_stageDumpStages = 0;
}

inline bool StageTextureDumpWants(uint32_t psHash) {
    if (!g_stageDumpStages) return false;
    for (uint32_t id : g_stageDumpPs) {
        if (id == psHash) return true;
    }
    return false;
}

// Texture stages the draw dump lists for a draw: 0..7 as before the stage dump existed, 0..15 only for a draw the
// stage dump wants, so the draw dump reads exactly as before while the dump is off.
inline unsigned DrawDumpStageCount(bool stageDump) { return stageDump ? 16u : 8u; }

enum class StageDumpResult { Written, NotTexture2D, Pool, Format, LockFailed, FileFailed };

inline const char* StageDumpResultName(StageDumpResult result) {
    switch (result) {
        case StageDumpResult::Written: return "written";
        case StageDumpResult::NotTexture2D: return "skipped-not-2d";
        case StageDumpResult::Pool: return "skipped-pool";
        case StageDumpResult::Format: return "skipped-format";
        case StageDumpResult::LockFailed: return "lock-failed";
        default: return "file-failed";
    }
}

// Writes level 0 of a 2D texture as a one-mip .dds. desc receives the level description.
inline StageDumpResult WriteStageTexture(IDirect3DBaseTexture9* base, const wchar_t* path, D3DSURFACE_DESC* desc) {
    *desc = {};
    if (!base || base->GetType() != D3DRTYPE_TEXTURE) return StageDumpResult::NotTexture2D;
    IDirect3DTexture9* texture = static_cast<IDirect3DTexture9*>(base);
    if (FAILED(texture->GetLevelDesc(0, desc))) return StageDumpResult::NotTexture2D;
    if (desc->Pool != D3DPOOL_MANAGED && desc->Pool != D3DPOOL_SYSTEMMEM) return StageDumpResult::Pool;

    uint32_t blockBytes = 0, pixelBytes = 0;
    switch (desc->Format) {
        case D3DFMT_DXT1: blockBytes = 8; break;
        case D3DFMT_DXT2: case D3DFMT_DXT3: case D3DFMT_DXT4: case D3DFMT_DXT5: blockBytes = 16; break;
        case D3DFMT_A8R8G8B8: case D3DFMT_X8R8G8B8: pixelBytes = 4; break;
        default: return StageDumpResult::Format;
    }
    const uint32_t width = desc->Width, height = desc->Height;
    const uint32_t rows = blockBytes ? (height + 3) / 4 : height;
    const uint32_t rowBytes = blockBytes ? ((width + 3) / 4) * blockBytes : width * pixelBytes;

    uint32_t header[32] = {};  // "DDS " + DDS_HEADER (124 bytes)
    header[0] = 0x20534444;
    header[1] = 124;
    header[2] = 0x1 | 0x2 | 0x4 | 0x1000 | (blockBytes ? 0x80000 : 0x8);  // caps, height, width, pixelformat, size
#if defined(STAGE_TEXTURE_DUMP_MUTANT)
    header[3] = width;   // mutant for the test: width and height swapped
    header[4] = height;
#else
    header[3] = height;
    header[4] = width;
#endif
    header[5] = blockBytes ? rowBytes * rows : rowBytes;
    header[7] = 1;   // mip count
    header[19] = 32; // DDS_PIXELFORMAT size
    if (blockBytes) {
        header[20] = 0x4;  // DDPF_FOURCC
        header[21] = static_cast<uint32_t>(desc->Format);
    } else {
        const bool alpha = desc->Format == D3DFMT_A8R8G8B8;
        header[20] = 0x40 | (alpha ? 0x1 : 0);  // DDPF_RGB (| DDPF_ALPHAPIXELS)
        header[22] = 32;
        header[23] = 0x00FF0000;
        header[24] = 0x0000FF00;
        header[25] = 0x000000FF;
        header[26] = alpha ? 0xFF000000 : 0;
    }
    header[27] = 0x1000;  // DDSCAPS_TEXTURE

    IDirect3DSurface9* surface = nullptr;
    if (FAILED(texture->GetSurfaceLevel(0, &surface)) || !surface) return StageDumpResult::LockFailed;
    D3DLOCKED_RECT locked = {};
    if (FAILED(surface->LockRect(&locked, nullptr, D3DLOCK_READONLY)) || !locked.pBits) {
        surface->Release();
        return StageDumpResult::LockFailed;
    }
    std::vector<uint8_t> data(sizeof header + static_cast<size_t>(rowBytes) * rows);
    std::memcpy(data.data(), header, sizeof header);
    for (uint32_t r = 0; r < rows; ++r) {
        std::memcpy(data.data() + sizeof header + static_cast<size_t>(r) * rowBytes,
                    static_cast<const uint8_t*>(locked.pBits) + static_cast<size_t>(r) * locked.Pitch, rowBytes);
    }
    surface->UnlockRect();
    surface->Release();

    HANDLE out = CreateFileW(path, GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (out == INVALID_HANDLE_VALUE) return StageDumpResult::FileFailed;
    DWORD written = 0;
    const BOOL ok = WriteFile(out, data.data(), static_cast<DWORD>(data.size()), &written, nullptr);
    CloseHandle(out);
    return ok && written == data.size() ? StageDumpResult::Written : StageDumpResult::FileFailed;
}

}  // namespace proxy
