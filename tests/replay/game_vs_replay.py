"""Compare a replay picture with the game's own picture of the same camera stop (WKR-7).
game_vs_replay.py <gate\\<stop>_raster.pfm> <replay frames\\f<frame>.png> [<min precision bits>]
The game picture is a screen grab saved as a float PFM (display-encoded values 0..1, bottom row first). It is turned
into an 8-bit picture and compared with apitrace's own snapdiff.Comparer: precision in bits (8 = equal to the last
bit, 0 = unrelated) and the count of pixels that differ by more than 5 percent. Exit 0 when precision is at least
the limit (default 6 bits), 1 when not, 2 when the two pictures have different sizes.
"""
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, r"D:\BlacklistOverhaul\experiments\replay-rig\tools\apitrace-14.0-win32\lib\scripts")
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from snapdiff import Comparer  # noqa: E402


def read_pfm(path):
    with open(path, "rb") as f:
        kind = f.readline().strip()
        w, h = (int(x) for x in f.readline().split())
        scale = float(f.readline())
        data = np.frombuffer(f.read(), dtype="<f4" if scale < 0 else ">f4")
    ch = 3 if kind == b"PF" else 1
    return np.flipud(data.reshape(h, w, ch))


pfm, png = sys.argv[1], sys.argv[2]
limit = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0
game = read_pfm(pfm)
game8 = Image.fromarray(np.clip(np.rint(game * 255.0), 0, 255).astype(np.uint8), "RGB")
replay = Image.open(png).convert("RGB")
out = {"game": pfm, "replay": png, "gameSize": list(game8.size), "replaySize": list(replay.size)}
if game8.size != replay.size:
    print(json.dumps({**out, "pass": False, "notMeasured": "sizes differ"}))
    sys.exit(2)
tmp = os.path.join(os.environ.get("TEMP", "."), "game_vs_replay_%d.png" % os.getpid())
game8.save(tmp)
try:
    c = Comparer(tmp, png)
    bits = c.precision()
    differ = c.ae()
finally:
    os.remove(tmp)
g, r = np.asarray(game8, dtype=np.float64), np.asarray(replay, dtype=np.float64)
out.update({"precisionBits": round(bits, 2), "pixelsOver5Percent": int(differ), "pixels": game8.size[0] * game8.size[1],
            "meanAbsDiff8bit": round(float(np.abs(g - r).mean()), 3), "gameMean": round(float(g.mean()), 2),
            "replayMean": round(float(r.mean()), 2), "limitBits": limit, "pass": bits >= limit})
print(json.dumps(out))
sys.exit(0 if out["pass"] else 1)
