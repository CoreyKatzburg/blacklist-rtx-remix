// Stage texture dump writer (play-setup\unified-build\stage_texture_dump.h) on a real D3D9 device, x86 like the proxy.
//   1. managed DXT1 16x4, blocks white, white, white, black  -> written; header 16x4 DXT1; payload = the 4 blocks
//   2. managed A8R8G8B8 2x2 white, black / black, white      -> written; header 2x2 RGB+alpha; payload = the 4 pixels
//   3. default-pool texture                                   -> skipped-pool, no file
//   4. keys: "BF48978C,4BAFE381" + "0,1,2,12" -> 2 ids, mask 1007; empty ps list -> off; unlisted ps not wanted
//   5. the fill counters: a managed texture locked once through LockRectCounted reads ,lock1 before and after the dump
// /DSTAGE_TEXTURE_DUMP_MUTANT swaps width and height in the header; that build must fail case 1.
// argv[1] = folder for the .dds files (run_stage_texture_dump_test.ps1 then decodes them with PIL).
// Exit 0 = every case as expected, 1 = a case failed, 2 = no D3D9 device.
#include <windows.h>
#include <d3d9.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "stage_texture_dump.h"
#include "texture_fill.h"

namespace {

int g_failures = 0;

void Check(bool ok, const char* name) {
    std::printf("%s %s\n", ok ? "ok  " : "FAIL", name);
    if (!ok) ++g_failures;
}

std::vector<uint8_t> ReadWholeFile(const std::wstring& path) {
    std::vector<uint8_t> data;
    HANDLE in = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    if (in == INVALID_HANDLE_VALUE) return data;
    const DWORD size = GetFileSize(in, nullptr);
    data.resize(size);
    DWORD got = 0;
    ReadFile(in, data.data(), size, &got, nullptr);
    CloseHandle(in);
    data.resize(got);
    return data;
}

uint32_t Word(const std::vector<uint8_t>& data, size_t index) {
    uint32_t w = 0;
    if (data.size() >= (index + 1) * 4) std::memcpy(&w, data.data() + index * 4, 4);
    return w;
}

bool Fill(IDirect3DTexture9* texture, const void* bytes, UINT rowBytes, UINT rows) {
    D3DLOCKED_RECT locked = {};
    if (FAILED(texture->LockRect(0, &locked, nullptr, 0))) return false;
    for (UINT r = 0; r < rows; ++r) {
        std::memcpy(static_cast<uint8_t*>(locked.pBits) + r * locked.Pitch, static_cast<const uint8_t*>(bytes) + r * rowBytes,
                    rowBytes);
    }
    texture->UnlockRect(0);
    return true;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) { std::printf("usage: stage_texture_dump_test <out folder>\n"); return 1; }
    wchar_t outDir[MAX_PATH];
    MultiByteToWideChar(CP_UTF8, 0, argv[1], -1, outDir, MAX_PATH);

    WNDCLASSW wc = {};
    wc.lpfnWndProc = DefWindowProcW;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = L"stage_texture_dump_test";
    RegisterClassW(&wc);
    HWND window = CreateWindowW(wc.lpszClassName, L"", WS_OVERLAPPEDWINDOW, 0, 0, 64, 64, nullptr, nullptr,
                                wc.hInstance, nullptr);
    IDirect3D9* d3d = Direct3DCreate9(D3D_SDK_VERSION);
    D3DPRESENT_PARAMETERS pp = {};
    pp.Windowed = TRUE;
    pp.SwapEffect = D3DSWAPEFFECT_DISCARD;
    pp.BackBufferWidth = pp.BackBufferHeight = 16;
    pp.BackBufferFormat = D3DFMT_UNKNOWN;
    pp.hDeviceWindow = window;
    IDirect3DDevice9* device = nullptr;
    if (!d3d || FAILED(d3d->CreateDevice(D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, window,
                                         D3DCREATE_SOFTWARE_VERTEXPROCESSING, &pp, &device))) {
        std::printf("no D3D9 device\n");
        return 2;
    }

    // 0. Off by default (no keys read yet): no draw is wanted and the draw dump lists stages 0..7, as before the dump.
    Check(proxy::g_stageDumpPs.empty() && proxy::g_stageDumpStages == 0, "0 defaults: no ids, stage mask 0");
    Check(!proxy::StageTextureDumpWants(0xBF48978C) && !proxy::StageTextureDumpWants(0),
          "0 defaults: no pixel shader wanted");
    Check(proxy::DrawDumpStageCount(proxy::StageTextureDumpWants(0xBF48978C)) == 8, "0 defaults: draw dump lists 8 stages");

    // 1. DXT1 16x4: three white blocks then one black block (color0 = color1, all indices 0).
    const uint8_t white[8] = {0xFF, 0xFF, 0xFF, 0xFF, 0, 0, 0, 0};
    const uint8_t black[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    uint8_t blocks[32];
    std::memcpy(blocks, white, 8);
    std::memcpy(blocks + 8, white, 8);
    std::memcpy(blocks + 16, white, 8);
    std::memcpy(blocks + 24, black, 8);
    IDirect3DTexture9* dxt = nullptr;
    const bool dxtMade = SUCCEEDED(device->CreateTexture(16, 4, 1, 0, D3DFMT_DXT1, D3DPOOL_MANAGED, &dxt, nullptr)) &&
                         Fill(dxt, blocks, 32, 1);
    Check(dxtMade, "1 create and fill managed DXT1 16x4");
    if (dxtMade) {
        const std::wstring path = std::wstring(outDir) + L"\\stage_dump_dxt1.dds";
        D3DSURFACE_DESC desc = {};
        const proxy::StageDumpResult result = proxy::WriteStageTexture(dxt, path.c_str(), &desc);
        const std::vector<uint8_t> file = ReadWholeFile(path);
        Check(result == proxy::StageDumpResult::Written, "1 result written");
        Check(file.size() == 128 + 32, "1 file size 160");
        Check(Word(file, 0) == 0x20534444 && Word(file, 1) == 124, "1 magic and header size");
        Check(Word(file, 3) == 4 && Word(file, 4) == 16, "1 header height 4, width 16");
        Check(Word(file, 21) == static_cast<uint32_t>(D3DFMT_DXT1) && Word(file, 20) == 0x4, "1 fourCC DXT1");
        Check(file.size() == 160 && std::memcmp(file.data() + 128, blocks, 32) == 0, "1 payload = the 4 blocks");
    }

    // 2. A8R8G8B8 2x2.
    const uint32_t pixels[4] = {0xFFFFFFFF, 0xFF000000, 0xFF000000, 0xFFFFFFFF};
    IDirect3DTexture9* argb = nullptr;
    const bool argbMade = SUCCEEDED(device->CreateTexture(2, 2, 1, 0, D3DFMT_A8R8G8B8, D3DPOOL_MANAGED, &argb, nullptr)) &&
                          Fill(argb, pixels, 8, 2);
    Check(argbMade, "2 create and fill managed A8R8G8B8 2x2");
    if (argbMade) {
        const std::wstring path = std::wstring(outDir) + L"\\stage_dump_argb.dds";
        D3DSURFACE_DESC desc = {};
        const proxy::StageDumpResult result = proxy::WriteStageTexture(argb, path.c_str(), &desc);
        const std::vector<uint8_t> file = ReadWholeFile(path);
        Check(result == proxy::StageDumpResult::Written, "2 result written");
        Check(Word(file, 3) == 2 && Word(file, 4) == 2 && Word(file, 22) == 32 && Word(file, 20) == 0x41,
              "2 header 2x2, 32 bit, RGB+alpha");
        Check(file.size() == 128 + 16 && std::memcmp(file.data() + 128, pixels, 16) == 0, "2 payload = the 4 pixels");
    }

    // 3. Default pool: skipped, and no file.
    IDirect3DTexture9* gpu = nullptr;
    if (SUCCEEDED(device->CreateTexture(16, 16, 1, 0, D3DFMT_A8R8G8B8, D3DPOOL_DEFAULT, &gpu, nullptr))) {
        const std::wstring path = std::wstring(outDir) + L"\\stage_dump_default.dds";
        DeleteFileW(path.c_str());
        D3DSURFACE_DESC desc = {};
        const proxy::StageDumpResult result = proxy::WriteStageTexture(gpu, path.c_str(), &desc);
        Check(result == proxy::StageDumpResult::Pool, "3 default pool skipped");
        Check(GetFileAttributesW(path.c_str()) == INVALID_FILE_ATTRIBUTES, "3 no file for the default pool");
    } else {
        Check(false, "3 create default-pool texture");
    }

    // 4. Keys.
    proxy::SetStageTextureDump(L"BF48978C,4BAFE381", L"0,1,2,12");
    Check(proxy::g_stageDumpPs.size() == 2 && proxy::g_stageDumpStages == 0x1007, "4 two ids, stage mask 1007");
    Check(proxy::StageTextureDumpWants(0xBF48978C) && !proxy::StageTextureDumpWants(0x12345678),
          "4 listed ps wanted, unlisted not");
    Check(proxy::DrawDumpStageCount(proxy::StageTextureDumpWants(0xBF48978C)) == 16 &&
              proxy::DrawDumpStageCount(proxy::StageTextureDumpWants(0x12345678)) == 8,
          "4 draw dump lists 16 stages for a listed ps only, 8 for an unlisted one");
    proxy::SetStageTextureDump(L"", L"0,1,2,12");
    Check(!proxy::StageTextureDumpWants(0xBF48978C), "4 empty ps list is off");
    proxy::SetStageTextureDump(L"BF48978C", L"");
    Check(!proxy::StageTextureDumpWants(0xBF48978C), "4 empty stage list is off");
    proxy::SetStageTextureDump(L"", L"");  // both keys missing: what the proxy reads from an ini without them
    Check(!proxy::StageTextureDumpWants(0xBF48978C) && proxy::g_stageDumpStages == 0 &&
              proxy::DrawDumpStageCount(proxy::StageTextureDumpWants(0xBF48978C)) == 8,
          "4 keys missing: off, draw dump lists 8 stages");

    // 5. Fill counters unchanged by the dump's read.
    proxy::SetTextureFillCounts(true);
    IDirect3DTexture9* counted = nullptr;
    const auto create = [&] {
        return device->CreateTexture(4, 4, 1, 0, D3DFMT_A8R8G8B8, D3DPOOL_MANAGED, &counted, nullptr);
    };
    char before[64] = {}, after[64] = {};
    if (SUCCEEDED(proxy::CreateTextureCounted(D3DPOOL_MANAGED, &counted, create))) {
        D3DLOCKED_RECT locked = {};
        proxy::LockRectCounted(counted, [&] { return counted->LockRect(0, &locked, nullptr, 0); });
        counted->UnlockRect(0);
        proxy::FormatTextureFill(counted, before, sizeof before);
        const std::wstring path = std::wstring(outDir) + L"\\stage_dump_counted.dds";
        D3DSURFACE_DESC desc = {};
        proxy::WriteStageTexture(counted, path.c_str(), &desc);
        proxy::FormatTextureFill(counted, after, sizeof after);
    }
    std::printf("     counters before %s after %s\n", before, after);
    Check(std::strcmp(before, ",pool1,lock1,upd0") == 0 && std::strcmp(before, after) == 0, "5 lockN unchanged by the dump");

    if (dxt) dxt->Release();
    if (argb) argb->Release();
    if (gpu) gpu->Release();
    if (counted) counted->Release();
    device->Release();
    d3d->Release();
    return g_failures ? 1 : 0;
}
