// Texture fill counters (play-setup\unified-build\texture_fill.h) on a real D3D9 device, x86 like the proxy.
// Calls the same *Counted wrappers the proxy hooks call, around the real D3D9 calls, then checks the tN suffix:
//   1. default-pool texture filled only by UpdateTexture from a systemmem texture   -> ,pool0,lock0,upd1
//   2. managed 2-level texture filled by LockRect/UnlockRect on both levels        -> ,pool1,lock2,upd0
//   3. managed texture never filled                                                -> ,pool1,lock0,upd0
//   4. the systemmem source of case 1 (locked once, never an UpdateTexture target) -> ,pool2,lock1,upd0
//   5. counters off                                                                -> ,poolx,lockx,updx
// /DTEXTURE_FILL_MUTANT wires the lock to the update counter and the update to the lock counter (a swapped hook);
// /DTEXTURE_FILL_NOCOUNT calls D3D9 without the wrappers (the rule removed). Both builds must fail, and the
// no-count build must fail cases 1, 2 and 3 each.
// Cost: the extra time per LockRect / UpdateTexture call with the counters on, and with them off, against a no-op
// call (1,000,000 calls each). The counters fail the test above 1000 ns per call on, or 20 ns per call off.
// Exit 0 = every case as expected, 1 = a case failed, 2 = no D3D9 device.
#include <windows.h>
#include <d3d9.h>

#include <cstdio>
#include <cstring>

#include "texture_fill.h"

namespace {

int g_failures = 0;

void Expect(const char* name, IDirect3DBaseTexture9* texture, const char* want) {
    char got[64];
    proxy::FormatTextureFill(texture, got, sizeof got);
    const bool ok = std::strcmp(got, want) == 0;
    std::printf("%s %-44s got %-22s want %s\n", ok ? "ok  " : "FAIL", name, got, want);
    if (!ok) ++g_failures;
}

HRESULT Lock(IDirect3DTexture9* texture, UINT level, D3DLOCKED_RECT* locked) {
    const auto call = [&] { return texture->LockRect(level, locked, nullptr, 0); };
#if defined(TEXTURE_FILL_NOCOUNT)
    return call();
#elif defined(TEXTURE_FILL_MUTANT)
    return proxy::UpdateTextureCounted(texture, call);
#else
    return proxy::LockRectCounted(texture, call);
#endif
}

HRESULT Update(IDirect3DDevice9* device, IDirect3DTexture9* source, IDirect3DTexture9* destination) {
    const auto call = [&] { return device->UpdateTexture(source, destination); };
#if defined(TEXTURE_FILL_NOCOUNT)
    return call();
#elif defined(TEXTURE_FILL_MUTANT)
    return proxy::LockRectCounted(destination, call);
#else
    return proxy::UpdateTextureCounted(destination, call);
#endif
}

IDirect3DTexture9* Create(IDirect3DDevice9* device, UINT levels, D3DPOOL pool) {
    IDirect3DTexture9* texture = nullptr;
    const auto call = [&] { return device->CreateTexture(16, 16, levels, 0, D3DFMT_A8R8G8B8, pool, &texture, nullptr); };
#if defined(TEXTURE_FILL_NOCOUNT)
    const HRESULT hr = call();
#else
    const HRESULT hr = proxy::CreateTextureCounted(pool, &texture, call);
#endif
    if (FAILED(hr)) {
        std::printf("FAIL CreateTexture pool %d: 0x%08lX\n", static_cast<int>(pool), hr);
        ++g_failures;
    }
    return texture;
}

bool Fill(IDirect3DTexture9* texture, UINT level) {
    D3DLOCKED_RECT locked = {};
    if (FAILED(Lock(texture, level, &locked)) || !locked.pBits) return false;
    std::memset(locked.pBits, 0x80, static_cast<size_t>(locked.Pitch) * (16u >> level));
    texture->UnlockRect(level);
    return true;
}

}  // namespace

int main() {
    WNDCLASSW wc = {};
    wc.lpfnWndProc = DefWindowProcW;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = L"texture_fill_test";
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

    proxy::SetTextureFillCounts(true);
    IDirect3DTexture9* source = Create(device, 1, D3DPOOL_SYSTEMMEM);
    IDirect3DTexture9* gpu = Create(device, 1, D3DPOOL_DEFAULT);
    IDirect3DTexture9* managed = Create(device, 2, D3DPOOL_MANAGED);
    IDirect3DTexture9* empty = Create(device, 1, D3DPOOL_MANAGED);
    if (g_failures) return 1;
    if (!Fill(source, 0)) { std::printf("FAIL lock of the systemmem source\n"); return 1; }
    if (FAILED(Update(device, source, gpu))) { std::printf("FAIL UpdateTexture\n"); return 1; }
    if (!Fill(managed, 0) || !Fill(managed, 1)) { std::printf("FAIL lock of the managed texture\n"); return 1; }

    Expect("1 default pool, UpdateTexture only", gpu, ",pool0,lock0,upd1");
    Expect("2 managed, LockRect on 2 levels", managed, ",pool1,lock2,upd0");
    Expect("3 managed, never filled", empty, ",pool1,lock0,upd0");
    Expect("4 systemmem source, locked once", source, ",pool2,lock1,upd0");
    proxy::SetTextureFillCounts(false);
    Expect("5 counters off", gpu, ",poolx,lockx,updx");

    // Cost per call: the wrapper around a no-op call, minus the bare no-op call, with the counters on and off.
    // Counters on: the texture is in the table (as every game texture is), so each call finds it and counts.
    volatile LONG sink = 0;
    const auto noop = [&] { InterlockedIncrement(&sink); return S_OK; };
    const auto timeNs = [&](auto&& body) {
        LARGE_INTEGER f, a, b;
        QueryPerformanceFrequency(&f);
        QueryPerformanceCounter(&a);
        for (int i = 0; i < 1000000; ++i) body();
        QueryPerformanceCounter(&b);
        return double(b.QuadPart - a.QuadPart) * 1e9 / double(f.QuadPart) / 1e6;
    };
    const double bare = timeNs([&] { noop(); });
    const double off = timeNs([&] { proxy::LockRectCounted(managed, noop); }) - bare;
    proxy::SetTextureFillCounts(true);
    const double onLock = timeNs([&] { proxy::LockRectCounted(managed, noop); }) - bare;
    const double onUpdate = timeNs([&] { proxy::UpdateTextureCounted(gpu, noop); }) - bare;
    proxy::SetTextureFillCounts(false);
    D3DLOCKED_RECT locked = {};
    const double realLock = timeNs([&] { if (SUCCEEDED(managed->LockRect(0, &locked, nullptr, 0))) managed->UnlockRect(0); });
    const bool costOk = onLock < 1000 && onUpdate < 1000 && off < 20;
    std::printf("%s cost per call: lock %.1f ns and update %.1f ns with counters on, %.1f ns off "
                "(a real LockRect+UnlockRect pair here: %.0f ns)\n",
                costOk ? "ok  " : "FAIL", onLock, onUpdate, off, realLock);
    if (!costOk) ++g_failures;

    empty->Release();
    managed->Release();
    gpu->Release();
    source->Release();
    device->Release();
    d3d->Release();
    DestroyWindow(window);
    std::printf("texture fill counters: %s\n", g_failures ? "FAIL" : "PASS");
    return g_failures ? 1 : 0;
}
