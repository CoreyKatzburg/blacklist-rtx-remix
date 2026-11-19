#pragma once
// Side recording for game-free replays (tests\replay\README.md). The proxy takes three things from the game's code
// addresses rather than from Direct3D calls: which SetVertexShaderConstantF call is the camera upload, and which draws
// come from the game's decal code. A recording run ([Replay] Record=1) writes them to d3d9_proxy_replay_calls.csv,
// keyed by frame and by the call's position within the frame; replay mode (proxy::ReplayMode) answers from that file.
//   C,<frame>,<upload index>,<caller RVA hex>   a camera upload
//   D,<frame>,<draw index>                      a draw from decal code
//   F,<frame>,<draws>,<uploads>                 the frame's totals, so a replay can show it stayed in step
// Replay mode also writes d3d9_proxy_replay_check.csv: frame, draws, uploads, recorded draws, recorded uploads,
// camera tags used, decal tags used. Lamps and flares are recorded by lightpub (d3d9_proxy_replay_lights.bin).

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <cstdint>

namespace proxy {

// Reads [Replay] Record, TraceFile (where apitrace, chained under the proxy, writes its trace) and TestCameraRva (a
// test program's call site to treat as the gameplay camera upload), or in replay mode loads the recorded file.
void ReplaySideInit(const wchar_t* iniPath);
bool ReplayRecording();
// The frame the calls now arriving belong to: 0 until the first Present.
uint32_t ReplaySideFrame();
// Once per SetVertexShaderConstantF call. Returns the caller RVA the proxy acts on: the live one in the game, the
// recorded one in replay mode.
uintptr_t ReplaySideCallerRva(uintptr_t liveCallerRva, UINT startRegister, UINT vector4Count);
// Once per draw. Returns whether the draw comes from decal code: the live answer in the game, the recorded one in
// replay mode.
bool ReplaySideDecalDraw(bool liveIsDecal);
// Once per Present, after the frame's last call.
void ReplaySideEndFrame();

}  // namespace proxy
