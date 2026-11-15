#pragma once
// Decal material RTTI scanner, XXH3-64 mip-0 texture hashing for RTX Remix decal tagging,
// top-mip stream hash logging (TexHashLog), and TextureStreamer budget patching.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d9.h>
#include <cstdint>

namespace proxy {

void SetDecalConfig(bool decalTag, bool hideDecals, bool logChanges);
void SetTexHashLogEnabled(bool enabled);
void SetStreamBudgetMB(int budgetMB);

bool IsDecalTagEnabled();
bool IsHideDecalsEnabled();
void NoteTextureReleaseHooked();

// Background thread entry point that scans Blacklist_game.exe RTTI/vtables for l3d::*DecalMaterialImpl methods.
DWORD WINAPI FindDecalCode(void* unused);
bool IsDecalCaller(void* returnAddress);

// Computes row byte width and row count for D3D9 surface formats used by Blacklist.
bool RowLayout(D3DFORMAT format, UINT width, UINT height, UINT* rowBytes, UINT* rows);

// Computes the XXH3-64 mip-0 hash that RTX Remix assigns to a managed 2D texture.
bool RemixTextureHash(IDirect3DBaseTexture9* base, uint64_t* hash, D3DSURFACE_DESC* desc);

// Classifies whether the current draw returns into a decal renderer and queues newly seen decal textures.
// Returns true when the draw comes from one of the game's decal material render methods.
bool DecalTagBeforeDraw(void* returnAddress,
                        IDirect3DBaseTexture9* stage0Texture,
                        bool passFilterEnabled,
                        bool drawIsScene,
                        bool gogglesActive);

// Evicts a destroyed texture pointer from the known-decal table so recycled addresses re-evaluate cleanly.
void ForgetDecalTexture(void* texture);

// Hashes at most one pending decal texture per frame and pushes updated rtx.decalTextures at most once/sec.
void DecalTagEndFrame();
// Auto-tags particle textures as rtx.particleTextures when RaytraceParticles=1.
void SetParticleTagEnabled(bool enabled, bool logChanges);
void ParticleTagBeforeDraw(IDirect3DBaseTexture9* stage0Texture, bool isParticleDraw, bool gogglesActive);
void ForgetParticleTexture(void* texture);
void ParticleTagEndFrame();


// Queues mip-0 texture unlocks for end-of-frame XXH3 hashing when TexHashLog=1.
void OnTextureUnlockRect(IDirect3DTexture9* texture, UINT level, HRESULT unlockResult);
void TexHashLogEndFrame(LONG presentCount);

// Patches l3d::TextureStreamer's top-mip budget when TextureStreamBudgetMB > 0.
void ApplyStreamBudget(LONG presentCount);

// Logs 600-frame DecalTag summary when DecalTag or HideDecals is active.
void LogDecalStatsPeriodic(LONG presentCount);

}  // namespace proxy
