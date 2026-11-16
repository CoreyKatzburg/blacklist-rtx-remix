"""What Remix did on the frames around each goggles-off (ray tracing back on after a pause inside a level).

Usage: python -I goggles_off_frames.py <rtx_telemetry.jsonl> [min_pause_frames]
A goggles-off is a frame with rayTraced true after at least min_pause_frames (default 10) frames with rayTraced
false, while instances > 0 (a level is loaded). For each one it prints frames -1..+3 with the fields that show a
scene clear: staticBlasBuilds (meshes built from scratch), staticBlasRebuilds, instances, cameraCut, mean luma.
A clear shows as hundreds of staticBlasBuilds on the first ray-traced frame; a kept scene shows near zero.
"""
import base64
import json
import sys

path = sys.argv[1]
min_pause = int(sys.argv[2]) if len(sys.argv) > 2 else 10


def luma_mean(o):
    thumb = o.get("lumaThumb")
    if not thumb:
        return float("nan")
    data = base64.b64decode(thumb)
    return sum(data) / (255.0 * len(data))


window, pending, events = [], [], []
off_run = 0
mid_play = []  # ray-traced frames that rebuilt the whole scene without a pause before them
with open(path, "rb") as f:
    for line in f:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        row = (o["frame"], o.get("rayTraced"), o.get("instances", -1), o.get("staticBlasBuilds", -1),
               o.get("staticBlasRebuilds", -1), o.get("cameraCut"), o.get("frameTimeMs", -1), luma_mean(o))
        window = (window + [row])[-2:]
        for ev in pending:
            ev.append(row)
        pending = [ev for ev in pending if len(ev) < 5]
        if o.get("rayTraced") and off_run >= min_pause and o.get("instances", 0) > 0:
            ev = list(window)  # frame -1 and frame 0
            events.append(ev)
            pending.append(ev)
        elif o.get("rayTraced") and off_run == 0 and row[2] >= 100 and row[3] >= 0.8 * row[2]:
            mid_play.append(row)
        off_run = 0 if o.get("rayTraced") else off_run + 1

print(f"{len(events)} goggles-off events (rayTraced back after >= {min_pause} frames off)")
cleared = 0
for ev in events:
    first = ev[1]
    builds = first[3]
    cleared += builds >= 40
    print(f"frame {first[0]}:")
    for rel, r in zip(range(-1, 4), ev):
        print(f"  {rel:+d} rt={int(bool(r[1]))} inst={r[2]:5} builds={r[3]:4} rebuilds={r[4]:4} cut={int(bool(r[5]))}"
              f" ft={r[6]:7.1f} luma={r[7]:.4f}")
print(f"summary: {cleared} of {len(events)} goggles-off frames built >= 40 static meshes from scratch")
print(f"mid-play full rebuilds (>= 80% of >= 100 instances built, previous frame ray traced): {len(mid_play)}"
      + "".join(f"\n  frame {r[0]} inst={r[2]} builds={r[3]} cut={int(bool(r[5]))} ft={r[6]:.1f}" for r in mid_play[:10]))
