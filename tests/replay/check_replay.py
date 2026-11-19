"""Code-decided checks for a replay run (tests/replay/README.md). Exit code 0 = PASS, 1 = FAIL, 2 = cannot check.

  check_replay.py step <replay run>\\proxy
      The replay stayed in step with the recording: every frame has the recording's number of draws and of
      SetVertexShaderConstantF calls (d3d9_proxy_replay_check.csv, written by the proxy in replay mode).
  check_replay.py camera <recording folder> <replay run>\\proxy
      The proxy treated the same calls as the camera: per frame, the camera uploads it logged in the replay
      (d3d9_proxy_frames.csv: source, draws before, skipped, position, forward) equal those of the recording run.
  check_replay.py lights <light dump .tsv> <replay run>\\proxy
      The proxy would publish the lamps the dump marks wanted=1 (d3d9_proxy_replay_lights_check.csv).
  check_replay.py lamps <recording folder> <replay run>\\proxy [<reference run>\\proxy]
      The lamps and flares the proxy published to Remix (stack mode) or would publish (proxy mode), one line per
      change in d3d9_proxy_replay_lights_check.csv, against the lists recorded in the game
      (d3d9_proxy_replay_lights.bin): the counts of that very frame's list, an answer on every frame where the
      recorded counts change, a wanted count equal to the node ids listed, only recorded lamps; with a reference run
      of the same trace, the same answers line for line.
  check_replay.py counts <recording folder> <replay run>\\proxy
      Stack replay against the live recording run: draws per frame in both d3d9_proxy_frames.csv files are equal.
"""
import bisect
import json
import os
import struct
import sys


def find(folder, *names):
    for name in names:
        path = os.path.join(folder, name)
        if os.path.exists(path):
            return path
    raise FileNotFoundError(f"none of {names} in {folder}")


def frames_csv(folder):
    """frame -> (draws, [publish text without the trailing view-projection difference])"""
    out = {}
    with open(find(folder, "d3d9_proxy_frames.csv", "proxy_frames.csv"), encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\r\n").split(",")
            if len(parts) < 6 or not parts[0].isdigit():
                continue
            publishes = [p.rsplit(":", 1)[0] for p in ",".join(parts[6:]).split("|") if p]
            out[int(parts[0])] = (int(parts[4]), publishes)
    return out


def step(replay):
    frames = bad = 0
    first = []
    with open(find(replay, "d3d9_proxy_replay_check.csv"), encoding="utf-8") as f:
        for line in f:
            frame, draws, uploads, rec_draws, rec_uploads, cameras, decals = (int(x) for x in line.split(","))
            frames += 1
            if draws != rec_draws or uploads != rec_uploads:
                bad += 1
                if len(first) < 5:
                    first.append({"frame": frame, "draws": draws, "recordedDraws": rec_draws, "uploads": uploads, "recordedUploads": rec_uploads})
    return {"check": "step", "frames": frames, "framesOutOfStep": bad, "first": first, "pass": frames > 0 and bad == 0}


def camera(recording, replay):
    a, b = frames_csv(recording), frames_csv(replay)
    common = sorted(set(a) & set(b))
    with_camera = sum(1 for n in common if a[n][1])
    differ = [n for n in common if a[n][1] != b[n][1]]
    first = [{"frame": n, "recording": a[n][1], "replay": b[n][1]} for n in differ[:5]]
    return {"check": "camera", "framesCompared": len(common), "framesWithCamera": with_camera, "framesDiffering": len(differ),
            "first": first, "pass": with_camera > 0 and not differ}


def counts(recording, replay):
    a, b = frames_csv(recording), frames_csv(replay)
    common = sorted(set(a) & set(b))
    differ = [n for n in common if a[n][0] != b[n][0]]
    first = [{"frame": n, "recordingDraws": a[n][0], "replayDraws": b[n][0]} for n in differ[:5]]
    return {"check": "counts", "framesCompared": len(common), "framesDiffering": len(differ), "first": first,
            "pass": bool(common) and not differ}


def lights(dump, replay):
    wanted = set()
    rows = 0
    with open(dump, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith("light\t"):
                continue
            rows += 1
            fields = dict(p.split("=", 1) for p in line.rstrip("\r\n").split("\t")[1:] if "=" in p)
            if fields.get("wanted") == "1":
                wanted.add(int(fields["node"], 16))
    lines = bad = 0
    first = []
    with open(find(replay, "d3d9_proxy_replay_lights_check.csv"), encoding="utf-8") as f:
        for line in f:
            frame, seen, count, flares, stand_ins, nodes = line.rstrip("\r\n").split(",")
            got = {int(n, 16) for n in nodes.split()}
            lines += 1
            if got != wanted or int(seen) != rows:
                bad += 1
                if len(first) < 5:
                    first.append({"frame": int(frame), "seen": int(seen), "wanted": int(count),
                                  "missing": sorted(f"{n:08X}" for n in wanted - got)[:8],
                                  "extra": sorted(f"{n:08X}" for n in got - wanted)[:8]})
    return {"check": "lights", "dumpLights": rows, "dumpWanted": len(wanted), "replayAnswers": lines,
            "answersDiffering": bad, "first": first, "pass": lines > 0 and len(wanted) > 0 and bad == 0}


def read_answers(replay):
    """d3d9_proxy_replay_lights_check.csv: [(frame, seen, wanted count, flares, stand-ins, [node ids])] in file order."""
    rows = []
    with open(find(replay, "d3d9_proxy_replay_lights_check.csv"), encoding="utf-8") as f:
        for line in f:
            frame, seen, count, flares, stand_ins, nodes = line.rstrip("\r\n").split(",")
            rows.append((int(frame), int(seen), int(count), int(flares), int(stand_ins), [int(n, 16) for n in nodes.split()]))
    return rows


def lamps(recording, replay, reference=None):
    """Each answer the proxy gave (stack mode: published to Remix; plain proxy mode: would publish) against the lamp
    and flare lists recorded in the game:
      - in step: the lamp and flare counts of the recorded list in force at that frame;
      - on time: every frame inside the replay where the recorded lamp or flare count changes has its own answer;
      - whole: the wanted count equals the number of node ids listed, and every node id is a recorded lamp;
      - with <reference proxy dir> (a run of the same trace): the same answers on the same frames, so a lamp
        dropped from the published list is caught."""
    blocks = {}   # frame -> (lamp count, flare count, lamp node ids)
    with open(find(recording, "d3d9_proxy_replay_lights.bin"), "rb") as f:
        while True:
            head = f.read(16)
            if len(head) < 16:
                break
            magic, frame, light_count, flare_count = struct.unpack("<4I", head)
            if magic != 0x4C524C42:
                raise ValueError(f"bad block header at byte {f.tell() - 16}")
            body = f.read(light_count * 224)
            if len(body) < light_count * 224:
                break
            f.seek(flare_count * 56, 1)
            blocks[frame] = (light_count, flare_count, {struct.unpack_from("<I", body, i * 224)[0] for i in range(light_count)})
    answers = read_answers(replay)
    with open(find(replay, "d3d9_proxy_replay_check.csv"), encoding="utf-8") as f:
        last_frame = max((int(line.split(",", 1)[0]) for line in f if line[:1].isdigit()), default=-1)
    problems = []

    def bad(kind, **what):
        problems.append({"problem": kind, **what})

    order = sorted(blocks)
    answered = set()
    for frame, seen, count, flares, stand_ins, nodes in answers:
        answered.add(frame)
        if frame > last_frame:
            bad("answer after the replay's last frame", frame=frame, lastFrame=last_frame)
        at = bisect.bisect_right(order, frame) - 1   # the recorded list in force at that frame
        if at < 0:
            bad("answer before the first recorded list", frame=frame)
            continue
        block = blocks[order[at]]
        if seen != block[0] or flares != block[1]:
            bad("counts differ from the recorded list", frame=frame, seen=seen, recordedLamps=block[0], flares=flares,
                recordedFlares=block[1])
        if count != len(nodes):
            bad("wanted count differs from the node ids listed", frame=frame, count=count, nodesListed=len(nodes))
        if not set(nodes) <= block[2]:
            bad("lamp not in the recorded list", frame=frame, notInRecording=sorted(f"{n:08X}" for n in set(nodes) - block[2])[:8])
    previous = None
    changes = 0
    for frame in (n for n in order if n <= last_frame):
        counts = blocks[frame][:2]
        if previous is not None and counts != previous:
            changes += 1
            if frame not in answered:
                bad("recorded counts change with no answer on that frame", frame=frame, recorded=list(counts),
                    before=list(previous))
        previous = counts
    out = {"check": "lamps", "recordedLists": len(blocks), "replayLastFrame": last_frame, "replayAnswers": len(answers),
           "recordedCountChanges": changes, "problems": len(problems)}
    if reference is not None:
        ref = read_answers(reference)
        differ = [i for i in range(max(len(ref), len(answers))) if i >= len(ref) or i >= len(answers) or ref[i] != answers[i]]
        out.update({"referenceAnswers": len(ref), "answersDifferingFromReference": len(differ)})
        for i in differ[:3]:
            a = answers[i] if i < len(answers) else None
            r = ref[i] if i < len(ref) else None
            bad("answer differs from the reference run", line=i + 1, replay=a and list(a[:5]) + [len(a[5])],
                reference=r and list(r[:5]) + [len(r[5])],
                onlyInReference=sorted(f"{n:08X}" for n in set(r[5] if r else []) - set(a[5] if a else []))[:8],
                onlyInReplay=sorted(f"{n:08X}" for n in set(a[5] if a else []) - set(r[5] if r else []))[:8])
    out["problems"] = len(problems)
    out["first"] = problems[:5]
    out["pass"] = bool(answers) and bool(blocks) and last_frame >= 0 and not problems
    return out


def main(argv):
    checks = {"step": (step, 1), "camera": (camera, 2), "lights": (lights, 2), "lamps": (lamps, (2, 3)), "counts": (counts, 2)}
    if len(argv) < 2 or argv[1] not in checks or len(argv) - 2 not in (checks[argv[1]][1] if isinstance(checks[argv[1]][1], tuple) else (checks[argv[1]][1],)):
        print(__doc__)
        return 2
    try:
        result = checks[argv[1]][0](*argv[2:])
    except (OSError, ValueError) as error:
        print(json.dumps({"check": argv[1], "pass": False, "notMeasured": str(error)}))
        return 2
    print(json.dumps(result))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
