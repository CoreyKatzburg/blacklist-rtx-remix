# Frame replay rig

Record the game's Direct3D 9 calls once with apitrace. Play them back with no Steam and no game process, through
Windows' own Direct3D 9 (the original game's picture) or through our proxy, the Remix bridge and Remix.

## State (Nov 19, 07:30 ET): which mode is proven on what

- **stack (proxy + bridge + Remix): works on the 06:41 recording at 2560x1440.** Four runs: three of frames 0 to
  2,099 and one of frames 0 to 6,419. Remix ray traced every level frame (280, 280, 280 and 4,600), one Remix frame per
  recorded frame, never fell behind, never ran out of video memory. The runs agree: see "Noise limit".
- **The saved stack pictures are Remix's picture (one run).** Remix's own screenshot of frame 6,350 against the
  saved picture of that frame: 78,450 of 3,686,400 pixels differ by more than 5 percent (mean difference 1.5 of
  255). Against the plain picture of the same frame: 2,872,572 pixels. See "Remix's own screenshot".
- **Faithful to the game on frames 2,282 to 6,419 (one run).** Cameras and draw counts equal the game run's log on
  4,138 of 4,138 frames, in step on 6,420 of 6,420.
- **plain (Windows' Direct3D 9): works, and shows the game's picture (one stop).** Plain frame 6,350 against the
  game's own screen grab at the first camera stop: 31,201 of 3,686,400 pixels differ by more than 5 percent,
  precision 11.97 bits, mean brightness 32.03 against 32.06 of 255 (no brightness curve difference shows). The
  game's grabs of two other stops against the same frame fail (5.8 and 5.07 bits). Two plain runs of the same
  frames are equal to the last bit (42 of 42 pictures).
- **proxy (proxy chained to Windows' Direct3D 9): does not cover level frames. Do not spend replays on it.** The
  pictures come out, but the proxy's own files stop at the first level frame (three runs, two proxy builds). Cause
  not found. Stack mode walks the proxy through every frame, so use stack mode.
- **Timing is not faithful.** A level frame takes 77 to 89 ms in replay (median of each run) against about 36 ms in
  a live launch at the same spot and size (GPU time is close: 36 to 40 against 35 ms). Each saved picture also costs
  about 0.3 s on the next frame. Use the rig for pictures and counts. Frame times stay on live launches.
- **Record at 2560x1440.** The 01:45 recording made its device at 3840x2160 (cause unknown). Remix ran out of
  video memory on it and fell far behind. It is retired for stack mode and for pictures.

## One command

    powershell -NoProfile -ExecutionPolicy Bypass -File D:\BlacklistOverhaul\experiments\replay-rig\remix-wkr7\tests\replay\replay.ps1 -Mode stack -Trace D:\BlacklistOverhaul\experiments\replay-rig\traces\20261119-064135-con_custodial-f0-2099.trace -Side D:\BlacklistOverhaul\experiments\replay-rig\recordings\recording-20261119-064135\con_custodial -SnapshotInterval 50 -Out <your run folder>

About 90 seconds. It takes the shared game lock by itself for the whole run and refuses while anyone has the game.
Run the display check first (`experiments\wkr-9-scratch\overnight_display_check.py` must say OK): Remix opens a window.

Arguments:

- `-Mode plain|proxy|stack`.
- `-Trace`: the recording. `-Side`: the spot folder of the same recording (camera, decals, lamps, flares by frame).
  Without `-Side` Remix gets no camera.
- `-SnapshotInterval N`: save a picture every N frames (1 = every frame, slow).
- `-Proxy <d3d9.dll>`: your proxy build. It must have replay mode (built from this branch or a branch that holds
  commits 6153aec and d3eeec4). Default: `experiments\replay-rig\build\d3d9.dll`.
- `-Setup <play-setup folder>`: take `d3d9_proxy.ini`, `rtx.conf` and `bridge.conf` from your worktree's
  `play-setup` instead of the installed ones. The Remix runtime always comes from `tools\remix\pkg`. Proven to
  pick up a changed file (WKR-16, BLK-7, run `experiments\blk7-scratch\replays\s-ae_off1`): its `stage\rtx.conf` is
  byte-equal to the `rtx.conf` of its `-Setup` folder and differs from the installed one, and the picture changed
  (1.61 times the plain picture's brightness, against 3.42 with the installed file). Check `stage\rtx.conf` in your
  run folder against your file.
- `-Materials <mod folder>` (stack mode): link this folder as `rtx-remix\mods\materials` instead of the main tree's
  `tools\remix\materials` (the default, unchanged). It must hold a `mod.usda`, or the replay stops before it takes the
  lock. To test one changed file without copying 5 GB, make a folder with copies of the files you change and
  junctions for the rest (`deps`, `textures`); never hard-link a file you will edit. Check your run: `run.json` has
  `materials`, and `stage\rtx-remix\mods\materials` is a junction to your folder. Proof: not yet run on a replay
  (a folder with no `mod.usda` is refused, checked offline).
- `-Reference <folder with f*.png>`: compare the pictures with apitrace's `snapdiff.py`; exit 1 on any difference
  above `-Fuzz` (0.05). Use it for plain against plain. For Remix pictures use the noise limit below.

## Which trace

| Trace (in `experiments\replay-rig\traces\`) | Frames | What is in it | Stack replay |
| --- | --- | --- | --- |
| `20261119-064135-con_custodial-f0-2099.trace` (0.96 GB) | 0 to 2,099 | loading, menu, then 280 level frames at the checkpoint, camera still | about 90 s |
| `20261119-064135-con_custodial-f0-6419.trace` (3.6 GB) | 0 to 6,419 | the same, on to the first camera stop (frames 6,312 to 6,396) | 7.5 minutes (plain: 4.2 minutes) |
| `20261119-064135-con_custodial.trace` (14.5 GB) | 0 to 70,063 | 24 camera stops (level frames 1,820 to 14,039); the rest is a minimized game | not run |
| `20261119-014505-con_custodial*.trace` | | 3840x2160, no camera turns: retired | no |

The frame numbers of every camera stop are in `recordings\recording-20261119-064135\con_custodial\spot.json`
(`windows`, field `proxy`). The game's own picture of each stop is beside it in `gate\<stop>_raster.pfm`.
The light switch step alone: `20261119-083109-con_custodial_switch_rec-f0-6919.trace` with side folder
`recordings\recording-20261119-083109\con_custodial_switch_rec` (window `light_switch`, frames 5,207 to 6,191).

## How to read the result

In `<Out>`:

- `run.json`: `exitCode` 0, `framesRendered`, `pictures`, `seconds`.
- `frames\f<frame>.png`: the saved pictures, named by frame number.
- `proxy\rtx_telemetry.jsonl` (stack): one line per Remix frame. `rayTraced` must be true and `instances` above 0
  on level frames. `vramFreeMB` at 0 means Remix ran out of video memory and the run is void.
- `proxy\d3d9_proxy_replay_check.csv` and `proxy\d3d9_proxy_frames.csv`: what the proxy saw per frame.
- `stage\rtx-remix\logs\`: Remix and bridge logs. "Still waiting on the Present semaphore" in `bridge32.log` means
  Remix fell behind: void.

Checks (exit 0 pass, 1 fail, 2 cannot check):

    D:\BlacklistOverhaul\tools\upscalers\py314\Scripts\python.exe -I check_replay.py step   <Out>\proxy
    D:\BlacklistOverhaul\tools\upscalers\py314\Scripts\python.exe -I check_replay.py camera <recording spot folder> <Out>\proxy
    D:\BlacklistOverhaul\tools\upscalers\py314\Scripts\python.exe -I check_replay.py counts <recording spot folder> <Out>\proxy

- `step`: every replayed frame has the recorded number of draws and shader constant uploads. Caught a planted
  fault (one draw taken from frame 1,900 of a copy: fails, names the frame).
- `camera`: the proxy sent Remix the same cameras as in the game run, frame by frame. Caught a planted fault on the
  real recording (one camera moved by 0.1 unit on frame 4,000 of a copy: fails, names the frame). The game run's
  own log starts at frame 2,282, so a trace must reach past that frame for this check to have anything to compare
  (with nothing to compare it exits 1).
- `counts`: draws per frame equal to the game run's log. Same frame limit. Caught a planted fault (one draw taken
  from frame 4,000 of a copy: fails, names the frame).
- `lamps <recording spot folder> <Out>\proxy [<reference run>\proxy]`: the lamps and flares the proxy published to
  Remix (stack mode, proxy build from commit a9890fd on; the default `build\d3d9.dll` predates it) against the
  lists recorded in the game: counts of the list in force at each frame, an answer on every frame where the recorded
  counts change, a wanted count equal to the node ids listed, only recorded lamps. With a reference run of the same
  trace, also the same answers line for line. Planted faults (`lamp-planted\`): unrecorded lamp, wrong count, lamp
  removed, answer late at the last frame and in mid run all fail. A lamp removed together with its count fails only
  against a reference run. It reads what the proxy sent, not what Remix drew.

## Scoring tools (in this folder, run with the same python as the checks, after the replay has ended)

- `compare_runs.py <run A> <run B> [<first frame>]`: every picture both runs have, with apitrace's snapdiff
  (precision in bits, pixels over 5 percent), plus the proxy's per-frame counts and Remix's instances per frame.
- `score_stack.py <run> <first level frame>`: ray traced frames, frame and GPU time, free video memory, bridge waits.
  For the 06:41 recording the first level frame is 1820.
- `game_vs_replay.py <gate\<stop>_raster.pfm> <frames\f<frame>.png>`: the game's own screen grab against a replay
  picture. Exit 0 at 6 bits or better.
- `remix_shot_vs_saved.py <Remix .dds> <frames\f<frame>.png> ...`: Remix's own screenshot against saved pictures.

Each prints one JSON line. Only `game_vs_replay.py` decides pass or fail by itself.

## Noise limit (four stack replays of the same frames, 0 to 2,099)

Runs `runs\cust2-stack-f2099`, `runs\cust2-stack-f2099-b`, `runs\cust2-stack-f2099-setup` and the first 2,100 frames
of `runs\cust2-stack-f6419`, each against the first:

- Proxy per-frame counts and cameras: 0 of 2,099 frames differ. Remix instances per frame: 0 of 2,100 differ.
- Loading and menu pictures: equal to the last bit (37 of 37).
- Level pictures (5): at most 615 of 3,686,400 pixels differ by more than 5 percent (first pair 211, 84, 25, 11, 0;
  second pair 197, 331, 116, 2, 1; third pair 61, 615, 566, 168, 1); snapdiff precision 16.1 bits or better.
  The largest count grew with each added run (211, 331, 615), so the true limit is not known yet.
- For scale: the same level frames in plain against stack differ in 659,599 to 827,844 pixels, precision 8.4 to 8.9.

So a change that moves fewer than about a thousand pixels cannot be told from noise with whole-picture counts.
The limit is measured on the first 280 level frames only, not at a camera stop.

## Remix's own screenshot

Set `DXVK_DUMP_SCREENSHOT_FRAME=<frame>` and `DXVK_SCREENSHOT_PATH=<folder>/` before a stack replay and Remix
writes its finished picture of that frame as `rtxImagePostTonemapping_*.dds` (2560x1440, 16-bit float).

- Remix leaves out its last brightness step (linear to sRGB) on that one frame (`rtx_context.cpp`,
  `performSRGBConversion = !captureScreenImage`). So the saved picture of that frame comes out dark (mean 54 of 255
  against 115 on the frames around it). Do not score that frame against other runs.
- That dark frame is also the proof that the saved pictures come from Remix: the screenshot as stored matches the
  saved picture of frame 6,350 (precision 8.54 bits, 78,450 pixels over 5 percent), and turned into sRGB it matches
  the neighbours 6,300 and 6,400 (8.2 and 8.35 bits). A frame from another camera position does not match (5.04
  bits, 3,024,073 pixels). What the 78,450 pixels are was not looked into. One run.
- Remix's picture at the first camera stop, with the brightness step put back:
  `experiments\replay-rig\remix-shots\cust2-stack-f6419\f0000006350_remix_srgb.png`.

## Record (one game launch, about 7 minutes for the whole con_custodial spot)

From a worktree that holds this branch and the launch fixes (`affe770`, `b7a2c8e`, `960bc93`):

    set REMIX_TEST_PKG=D:\BlacklistOverhaul\tools\remix\pkg
    powershell -NoProfile -ExecutionPolicy Bypass -File <worktree>\tests\verify_runtime.ps1 -Record -Spots con_custodial

- The script takes the shared lock by itself. Do not take it by hand first.
- The worktree's `play-setup` needs `d3d9.dll` (a proxy build with the side recorder, matching `$ProxyHash` in
  `RemixPlayCommon.ps1`), `resting\d3d9.dll.off` and the three config files byte-equal to the installed ones.
- It records the original game: our proxy passes every call on unchanged, and apitrace's `d3d9.dll` sits between
  the proxy and Windows' Direct3D 9 (`drive.py` accepts it as the one named recorder).
- Output: `experiments\replay-rig\traces\<stamp>-<spot>.trace` and
  `experiments\replay-rig\recordings\recording-<stamp>\<spot>\` (side files, `spot.json`, the game's pictures).
- Size: about 1.1 MB per level frame at about 46 level frames a second. The whole spot was 14.5 GB.
- Check the device size before using a recording:
  `apitrace dump --calls=0-400 <trace> | findstr CreateDevice` must show `BackBufferWidth = 2560`.
- Nobody may run the full `test_drive.py` or a build during the launch (see CREW.md).
- Nothing may call the game's device from our side during a recording: apitrace sits below the proxy and records
  every call as the game's. The proxy's light and draw dump (`[Proxy] CaptureDraws`) did, and the stack replay of
  that recording crashed on the dump frame while plain replay ran through. Since commit 5daebb2 a recording launch
  takes no dump; the side file holds the lamps of every frame anyway. In a stack run's `retrace.log`, "unexpected
  object destruction" warnings mean the trace holds such calls.
- Short spots made for recording (switchOnly, no sweep): `con_custodial_switch_rec`, `con_custodial_goggles_rec`,
  `con_bright_hall_rec`, `con_boiler_rec` in `tests\runtime\spots.json`.

## Replaying a short frame range

A replay always starts at frame 0: the trace holds every texture and shader from the moment the device was made,
and apitrace cannot start in the middle. So a short test is a trace cut after N frames:

    D:\BlacklistOverhaul\experiments\replay-rig\tools\apitrace-14.0-win32\bin\apitrace.exe trim --frames=0-2099 -o <short.trace> <full.trace>

Cutting reads the file up to frame N (23 s for 0 to 2,099, 2.5 minutes for 0 to 6,419). Cost of a replay: loading
and menu frames 0 to 1,819 take about one minute in stack mode; each level frame then takes about 85 ms in stack
mode and about 45 ms in plain mode (with a picture every 50 frames). A test of one moment costs the frames before it. To keep a problem's recording
short, record a spot that goes to the problem right after the checkpoint loads.

## Proxy replay mode: what the proxy took from the game, and where it comes from in replay

Set by the environment variable `BLPROXY_REPLAY=1` (`replay.ps1` sets it). The game never has it.

| In the game the proxy uses | In replay |
| --- | --- |
| The game code address of the caller, to pick the camera upload (0x015D8E71, 0x0160A972) | `d3d9_proxy_replay_calls.csv`, `C` rows: frame and call position of each camera upload |
| The caller's code range, to tag decal draws | the same file, `D` rows: frame and call position of each decal draw |
| Lamp and flare lists read from game memory | `d3d9_proxy_replay_lights.bin`: one block per frame with the raw lamp and flare records |
| The game's loop timer patch (0x006FE4B7) and the texture stream budget write | not applied: no game memory is read or written |
| The game's folder for its ini, log and the Remix DLL | the folder of the proxy DLL (apitrace's replay program reports the game's exe path as its own) |

`BLPROXY_REPLAY_SIZE=<width>x<height>` (replay only, never committed; its build and patch were deleted on Nov 19)
made the device another size than recorded. Tried once on the 3840x2160 recording: the replay ran, but Remix did
not ray trace.

## What the rig cannot show

- Frame times (see State).
- Anything where the game reacts to our stack: which flares it draws after its occlusion queries, which formats it
  picks from device caps, streaming choices. Real input and mouse lag. Quitting.
- Whether Remix drew the lamps the proxy published: `lamps` reads the proxy's side only.

## Test program

`smoke\tri.cpp` (a turning triangle, 120 frames) and `traces\tri_rehearsal.trace` proved the tools first: plain
replay twice 0 differing pixels, a planted one-frame shift flagged in 119 of 119 frames, camera check 96 of 96.
