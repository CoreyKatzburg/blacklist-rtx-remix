#pragma once
// Light publisher: reads Blacklist's own renderer lights (l3d::LightNode) every frame and
// submits them to RTX Remix through the Remix API exposed by the 32-bit bridge client.
// Needs `exposeRemixApi = True` in .trex\bridge.conf; otherwise Initialize fails cleanly.
//
// Integration in d3d9_proxy.cpp:
//   - after g_realD3D9 is loaded: lightpub::Initialize(g_realD3D9, iniPath, &WriteLog);
//   - in HookPresent, before forwarding:  lightpub::OnPresent();
//   - in DLL_PROCESS_DETACH (optional):   lightpub::Shutdown();
// Settings live in the [Lights] section of d3d9_proxy.ini (see lightpub.ini.example).
#include <windows.h>
#include <stdint.h>

namespace lightpub {

using LogFn = void (*)(const char* format, ...);

// Returns true when the Remix API answered and [Lights] Enable=1. Safe to call once.
bool Initialize(HMODULE remixClient, const wchar_t* iniPath, LogFn log);
// Publishes this frame's lights. Cheap no-op when disabled. `frame` is proxy::ReplaySideFrame(): in replay mode the
// lights and flares come from the recording's block for that frame instead of game memory.
void OnPresent(uint32_t frame);
// A recording run with lights switched off: reads the game's lights and flares only to record them.
void RecordGameFrame(uint32_t frame);
// True when [Proxy] HidePostFx=1: the proxy then drops the game's post-processing draws
// onto the back buffer so they cannot paint over Remix's ray-traced image.
bool HidePostFx();
// The proxy reports each frame whether the game's own picture must show (goggles with GogglesGameView, or a menu
// while PassFilter is off). AutoRaytracing then pauses ray tracing but keeps Remix's scene (auto_raytracing.h).
void SetShowGamePicture(bool show);
// The proxy reports the game's cSHScale (c16 of its character shaders) whenever it sees it; room fill and the sky
// dome use it like the game does (light_calibration.h).
void SetShScale(float scale);
// True unless ray tracing was switched off: by AutoRaytracing (loading, goggles) or by rtx.enableRaytracing in the ini.
bool RaytracingOn();
// Sets one Remix option (rtx.*) through the Remix API. False when the API isn't ready. The bridge only queues it.
bool SetRemixConfig(const char* key, const char* value);
// Replaces the proxy's override list: each entry is {key, value while the mode is on, value to restore when the list
// is replaced}. Overrides are re-sent after [RemixConfig] on every ini reload. count 0 clears (restores) them.
void SetRemixOverrides(const char* const (*list)[3], int count);
// Diagnostic: writes every LightNode in the game's database (qualifying or not) to a tab-separated file.
void DumpLights(const wchar_t* path);
void Shutdown();

}  // namespace lightpub
