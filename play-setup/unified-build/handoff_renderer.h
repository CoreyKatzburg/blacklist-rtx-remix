#pragma once
// Occlusion-query scene bracket, UI handoff marker injection, and linear-HDR pre-postprocess
// handoff decoding (rt_decode_ps.h) so Blacklist's post chain runs on RTX Remix's image.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d9.h>

#include "camera_solver.h"
#include "pass_graph.h"
#include "proxy_types.h"

namespace proxy {

using FlushSettersFn = void (*)(IDirect3DDevice9* device);

void SetHandoffConfig(bool handoffBeforePost, int headroomStops, bool modernFilter, bool logAndApplyOverrides);
bool IsHandoffBeforePostEnabled();
bool IsModernFilterEnabled();
void ApplyHandoffOverrides();

// Opens or closes the proxy's D3DQUERYTYPE_OCCLUSION bracket so Remix rasterizes non-scene draws.
void SetBracket(IDirect3DDevice9* device, bool open);
bool IsBracketOpen();

void LatchCompositeTarget(IDirect3DSurface9* rt0);
void RememberSceneHdr(IDirect3DSurface9* surface);
bool ReadsSceneHdr(IDirect3DBaseTexture9* texture);
IDirect3DSurface9* CurrentSceneHdrTarget();

void ReleaseHandoffResources();
void HandoffOnDeviceReset();
bool EnsureHandoffResources(IDirect3DDevice9* device, UINT width, UINT height);

// Decodes g_rtImage back into linear HDR inside the game's scene render target, restoring modified D3D9 state.
void DecodeIntoScene(IDirect3DDevice9* device, DeviceHook* hook, UINT width, UINT height);

// Emits a zero-area UI draw on the latched composite target so Remix composites its ray-traced output there.
void EmitHandoffMarker(IDirect3DDevice9* device,
                       DeviceHook* hook,
                       PassGraph& passGraph,
                       const CameraFrameTracker& cameraTracker,
                       IDirect3DSurface9* currentRt0,
                       FlushSettersFn flushSetters);

// Copies Remix's ray-traced image into an FP16 texture and decodes linear HDR into Blacklist's scene target before post-FX.
bool HandoffBeforePost(IDirect3DDevice9* device,
                       DeviceHook* hook,
                       PassGraph& passGraph,
                       const CameraFrameTracker& cameraTracker,
                       IDirect3DSurface9* currentRt0,
                       UINT backbufferWidth,
                       UINT backbufferHeight,
                       FlushSettersFn flushSetters);
bool PresentModernSceneToComposite(IDirect3DDevice9* device, DeviceHook* hook, IDirect3DSurface9* target);

// Finalizes end-of-frame handoff state before Present, emitting a fallback composite handoff when needed.
void HandoffEndFrame(IDirect3DDevice9* device,
                     DeviceHook* hook,
                     PassGraph& passGraph,
                     const CameraFrameTracker& cameraTracker,
                     IDirect3DSurface9* currentRt0,
                     bool raytracingOn);

LONG TakeEarlyHandoffsCount();
LONG TakeBracketOpensCount();

}  // namespace proxy
