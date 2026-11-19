# Frame replay rig

Record the game's Direct3D 9 calls once with apitrace. Play them back with no Steam and no game process.

State on Nov 19, 01:10 ET: the tools, the proxy replay mode and the picture compare work on a small test program.
No game scene is recorded yet. Nothing has gone through Remix yet. Lines marked "not run yet" are untested.

## What is where

- Tools (not in git): `D:\BlacklistOverhaul\experiments\replay-rig\tools\apitrace-14.0-win32` (apitrace 14.0, MIT,
  from github.com/apitrace/apitrace releases).
- Recordings and run folders (not in git): `D:\BlacklistOverhaul\experiments\replay-rig\` (`runs\`, `smoke\`).
- Proxy with replay mode: built from this branch into `experiments\replay-rig\build\d3d9.dll`. The installed proxy in
  `play-setup\` has no replay mode.
- `replay.ps1`: the one command. `smoke\tri.cpp`: the test program (a turning triangle).

## Record (needs one game launch, monitors on) - not run yet

The game is recorded as the original game: our proxy stays loaded but passes every call on unchanged (the existing
`-NoRemix` launch), and apitrace sits between the proxy and Windows' Direct3D 9.

    powershell -NoProfile -ExecutionPolicy Bypass -File D:\BlacklistOverhaul\tools\remix\tests\verify_runtime.ps1 -NoRemix -Spots con_custodial -IniKeys "Proxy.ChainDll=D:\BlacklistOverhaul\experiments\replay-rig\tools\apitrace-chain\SysWOW64\d3d9.dll"

Things to know before running it:

- The spot run already turns the camera (8 stops at 3 heights and a 30 second pan), so the recording covers movement.
- The copy of apitrace's `d3d9.dll` sits in a folder named `SysWOW64` on purpose. `drive.py` only accepts a chained
  DLL whose path ends in `\SysWOW64\d3d9.dll`. This is a workaround; the clean fix is one more accepted name in
  `drive.py`.
- The run folder lands in `experiments\remix-gate\reference-<stamp>`. It is a recording, not a reference. Move it to
  `experiments\replay-rig\` afterwards so no gate run picks it as its reference.
- apitrace writes `Blacklist_game.trace` into the game's working folder (expected from apitrace's rules, not seen
  yet). Move it to `experiments\replay-rig\traces\`. Size is unknown; D: had 695 GB free.
- The launch script refuses below 10.5 GB free on C: and 14.2 GB of memory room. On Nov 19 it refused for both.

## Replay

    powershell -NoProfile -ExecutionPolicy Bypass -File replay.ps1 -Trace <file.trace> -Mode plain
    powershell -NoProfile -ExecutionPolicy Bypass -File replay.ps1 -Trace <file.trace> -Mode proxy -Reference <plain run>\frames
    powershell -NoProfile -ExecutionPolicy Bypass -File replay.ps1 -Trace <file.trace> -Mode stack -Lights <light dump .tsv>

- `plain`: Windows' own Direct3D 9. This is the original game's picture.
- `proxy`: our proxy in replay mode, chained to Windows' Direct3D 9. Shows the proxy changes nothing when its
  rewrites are off.
- `stack`: proxy, Remix bridge and Remix. Not run yet. It takes the game lock and refuses when the game runs or C:
  is below 10.5 GB free.

Each run folder gets `frames\f<frame>.png`, `retrace.log`, `run.json`, and in proxy and stack mode `proxy\` with the
proxy log and `d3d9_proxy_frames.csv` (times per frame). `-Reference` compares the pictures with apitrace's own
`snapdiff.py` and exits 1 on any mismatch or missing frame.

## Proxy replay mode

Set by the environment variable `BLPROXY_REPLAY=1` (`replay.ps1` sets it). The game never has it.

- The proxy looks for its ini, log and the Remix DLL beside its own DLL. Reason: apitrace's replay program reports
  the recorded game's exe path as its own, so the proxy would otherwise use the files in the game folder.
- No game memory is read or patched: no loop timer patch, no texture budget write, no light list walk.
- Lights come from a light dump recorded in the game (`d3d9_proxy_replay_lights.tsv` beside the proxy). Each row's
  raw words rebuild that light's memory, so the normal light code runs on it. One recorded moment serves every
  frame. Not run yet (it needs Remix).

## Known gaps

- Camera: the proxy knows the camera upload by the game code address that made the call. In replay every call comes
  from the replay program, so no camera reaches Remix. apitrace cannot record call addresses on Windows. Plan: the
  installed proxy already writes, per frame, where in the draw order each camera upload came
  (`d3d9_proxy_frames.csv`); replay mode should read that file from the recording run.
- Flares are not replayed.
- Lights do not change over the recording.
- Final Remix pictures: untested whether d3dretrace's snapshots through the bridge show Remix's picture. Remix's own
  hook `DXVK_DUMP_SCREENSHOT_FRAME` writes one chosen frame.
- Frame times in plain mode: d3dretrace prints only the total. PresentMon is the outside clock to add.
- Occlusion queries: the recording holds the answers the game received (`apitrace dump <trace> | findstr GetData`).
  d3dretrace can compare them with a replay's answers (`--query-handling=check`). Not tried yet.

## What the rig cannot show

Anything where the game reacts to our stack: which flares it draws after its occlusion queries, which formats it
picks from device caps, streaming choices. Real input and mouse lag. Quitting.

## Measured so far (test program only, 120 frames, 1280x720)

- plain replay twice: 0 differing pixels in 120 frames.
- plain replay against proxy replay: 0 differing pixels in 120 frames.
- planted fault (pictures shifted by one frame): 119 of 119 frames flagged, exit code 1.
