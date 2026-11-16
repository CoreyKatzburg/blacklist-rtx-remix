#pragma once
// How each 2D texture was created and filled, for the draw dump's tN field (worker E's white-surfaces item):
//   poolP  the D3DPOOL given at CreateTexture (0 default, 1 managed, 2 systemmem, 3 scratch)
//   lockL  successful IDirect3DTexture9::LockRect calls on any level since creation
//   updU   successful UpdateTexture calls with this texture as the destination since creation
// Not counted: fills through a surface (GetSurfaceLevel then IDirect3DSurface9::LockRect, UpdateSurface, StretchRect)
// and rendering into it. Textures this proxy did not see created (cube, volume, or created before the hooks) print
// poolx,lockx,updx, as does every texture while the counters are off ([Proxy] TextureFillCounts, default
// EnableDebuggingTelemetry). HookCreateTexture, HookLockRect and HookUpdateTexture (d3d9_proxy.cpp) call the
// *Counted wrappers; tests\hitch\texture_fill_test.cpp calls them with a real D3D9 device.

#include <windows.h>
#include <d3d9.h>

#include <cstdio>
#include <new>
#include <unordered_map>

namespace proxy {

struct TextureFill {
    int pool = -1;
    unsigned locks = 0;
    unsigned updates = 0;
};

inline bool g_textureFillOn = false;
inline SRWLOCK g_textureFillLock = SRWLOCK_INIT;
inline std::unordered_map<void*, TextureFill>* g_textureFill = nullptr;

inline void SetTextureFillCounts(bool on) { g_textureFillOn = on; }
inline bool TextureFillCountsEnabled() { return g_textureFillOn; }

// Created: a new record replaces whatever an earlier texture at the same address left behind.
template <class CreateCall>
HRESULT CreateTextureCounted(D3DPOOL pool, IDirect3DTexture9** texture, CreateCall&& create) {
    const HRESULT result = create();
    if (g_textureFillOn && SUCCEEDED(result) && texture && *texture) {
        AcquireSRWLockExclusive(&g_textureFillLock);
        if (!g_textureFill) g_textureFill = new (std::nothrow) std::unordered_map<void*, TextureFill>();
        if (g_textureFill) (*g_textureFill)[*texture] = TextureFill{static_cast<int>(pool), 0, 0};
        ReleaseSRWLockExclusive(&g_textureFillLock);
    }
    return result;
}

inline void CountTextureFill(void* texture, bool lock) {
    AcquireSRWLockExclusive(&g_textureFillLock);
    if (g_textureFill) {
        const auto it = g_textureFill->find(texture);
        if (it != g_textureFill->end()) ++(lock ? it->second.locks : it->second.updates);
    }
    ReleaseSRWLockExclusive(&g_textureFillLock);
}

template <class LockCall>
HRESULT LockRectCounted(IDirect3DTexture9* texture, LockCall&& lock) {
    const HRESULT result = lock();
    if (g_textureFillOn && SUCCEEDED(result)) CountTextureFill(texture, true);
    return result;
}

template <class UpdateCall>
HRESULT UpdateTextureCounted(IDirect3DBaseTexture9* destination, UpdateCall&& update) {
    const HRESULT result = update();
    if (g_textureFillOn && SUCCEEDED(result) && destination) CountTextureFill(destination, false);
    return result;
}

// Writes ",poolP,lockL,updU" (or ",poolx,lockx,updx" when unknown) for appending to a tN token.
inline void FormatTextureFill(IDirect3DBaseTexture9* texture, char* out, size_t size) {
    TextureFill fill;
    bool known = false;
    if (g_textureFillOn && texture && texture->GetType() == D3DRTYPE_TEXTURE) {
        AcquireSRWLockShared(&g_textureFillLock);
        if (g_textureFill) {
            const auto it = g_textureFill->find(texture);
            if (it != g_textureFill->end()) { fill = it->second; known = true; }
        }
        ReleaseSRWLockShared(&g_textureFillLock);
    }
    if (known) std::snprintf(out, size, ",pool%d,lock%u,upd%u", fill.pool, fill.locks, fill.updates);
    else std::snprintf(out, size, ",poolx,lockx,updx");
}

}  // namespace proxy
