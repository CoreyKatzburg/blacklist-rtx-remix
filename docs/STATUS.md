# Status of the 20 tracked graphics problems

As of November 20, 2026. These are the problems the owner saw in two play videos (November 13 and 14) plus ones the tests found. Each one is a ticket on our board. A problem counts as fixed only when a code check passes against the original game's number for the same frames, and switching the fix off brings the failure back.

**1 of 20 is closed.**

| # | Problem | State |
|---|---------|-------|
| 1 | Rooms washed out and too bright | In progress |
| 2 | Low frame rate and hitches | In progress |
| 3 | Glowing orbs with no lamp | Waiting |
| 4 | Room stays lit after the light switch | In progress |
| 5 | Gun and laser sight glitches | Not started |
| 6 | Black decals and black squares | In progress |
| 7 | Walls vanish when night vision turns off | Waiting |
| 8 | Glowing white eyes in close-ups | Not started |
| 9 | Missing face on a dead enemy | Needs the owner's decision |
| 10 | Flat puddles and glowing cracks | Not started |
| 11 | Crash when quitting | **Fixed** |
| 12 | Flare orbs that pop, pulse and flicker | Not started |
| 13 | Characters and props crushed to black | Not started |
| 14 | Unlit rooms pitch black | Not started |
| 15 | Exposure surges at doors and cuts | Not started |
| 16 | Missing contact shadows | Not started |
| 17 | White clay-like tanks in the boiler room | In progress |
| 18 | Lighting flickers on Sam and walls boil | Not started |
| 19 | Noisy or black glass | Not started |
| 20 | Mouse lag | Not started |

## Notes on the numbers

- **Frame rate.** In the play videos the ray traced game ran at about 27 to 31 FPS (the original game runs at 150 to 200). With Remix rendering at 1280x720 and upscaling to 2560x1440 and a debug recorder switched off, one test run in one room read about 52 FPS. That is one run in one room on an RTX 3070 Ti, not a measurement of the game as played. Frame times during real play have not been measured yet.
- **Quit crash.** The game used to crash on quit with a lost graphics device. The fix is one Remix setting, `rtx.swapchainRecreateWaitIdle`, added by our patch 0008: Remix waits for the graphics card to go idle before it recreates the swap chain. Proven off, on, off in live runs (crash, clean, crash), then clean again in two more live quits with the setting at its default (on).
- **"In progress"** means a worker is on it and has leads. None of those are fixed by the rule above yet.
