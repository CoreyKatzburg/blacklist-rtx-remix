"""Code-decided checks for a replay run (tests/replay/README.md). Exit code 0 = PASS, 1 = FAIL, 2 = cannot check.

  check_replay.py step <replay run>\\proxy
      The replay stayed in step with the recording: every frame has the recording's number of draws and of
      SetVertexShaderConstantF calls (d3d9_proxy_replay_check.csv, written by the proxy in replay mode).
  check_replay.py camera <recording folder> <replay run>\\proxy
      The proxy treated the same calls as the camera: per frame, the camera uploads it logged in the replay
      (d3d9_proxy_frames.csv: source, draws before, skipped, position, forward) equal those of the recording run.
  check_replay.py lights <light dump .tsv> <replay run>\\proxy
      The proxy would publish the lamps the dump marks wanted=1 (d3d9_proxy_replay_lights_check.csv).
  check_replay.py counts <recording folder> <replay run>\\proxy
      Stack replay against the live recording run: draws per frame in both d3d9_proxy_frames.csv files are equal.
"""
import json
import os
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


def main(argv):
    checks = {"step": (step, 1), "camera": (camera, 2), "lights": (lights, 2), "counts": (counts, 2)}
    if len(argv) < 2 or argv[1] not in checks or len(argv) != 2 + checks[argv[1]][1]:
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
