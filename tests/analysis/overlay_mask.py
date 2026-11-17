"""The one shared definition of the NVIDIA performance overlay box (FPS, GPU, CPU, LAT, top right of screen captures).

Measured by worker G on Nov 17 (tools\\remix\\.amp\\in\\workerG\\overlay_mask.py, which also holds the measuring modes).
BOX is inclusive pixel bounds at 2560x1440. box_at(w, h) scales it to another picture size, rounding outward so the
scaled box never covers less than BOX. mask_pair() blanks the box in BOTH pictures the same way; use it on every
compare whose source is the screen capture, never on one picture of a pair only.

  --selftest   fake pairs at 2560x1440, 320x180 and 128x72: a pair that differs only inside the box must read
               "no difference" after masking; pairs that differ just outside it (one row below, one column left) must
               still be caught; without the mask the inside-only pair must be caught (so the compare can fail).
"""
import json, math, sys
import numpy as np

BOX = (1990, 0, 2559, 63)        # x0, y0, x1, y1 inclusive at 2560x1440


def box_at(w, h):
    x0, y0, x1, y1 = BOX
    return (math.floor(x0 * w / 2560), math.floor(y0 * h / 1440),
            math.ceil((x1 + 1) * w / 2560) - 1, math.ceil((y1 + 1) * h / 1440) - 1)


def mask_pair(a, b):
    """Copies of a and b (same size, rows x columns[, channels]) with the overlay box set to 0 in both."""
    if a.shape[:2] != b.shape[:2]:
        raise ValueError(f"mask_pair needs equal sizes, got {a.shape[:2]} and {b.shape[:2]}")
    h, w = a.shape[:2]
    x0, y0, x1, y1 = box_at(w, h)
    a, b = a.copy(), b.copy()
    a[y0:y1 + 1, x0:x1 + 1] = 0
    b[y0:y1 + 1, x0:x1 + 1] = 0
    return a, b


def differs(a, b, step=8.0):
    return int((np.abs(a.astype(np.float32) - b.astype(np.float32)) > step).sum())


def selftest():
    ok = []
    for w, h in ((2560, 1440), (320, 180), (128, 72)):
        x0, y0, x1, y1 = box_at(w, h)
        rng = np.random.default_rng(w)
        base = rng.uniform(40, 200, (h, w)).astype(np.float32)
        inside = base.copy(); inside[y0:y1 + 1, x0:x1 + 1] += 50          # differs only inside the box
        below = base.copy(); below[y1 + 1, x0:x1 + 1] += 50               # one row below the box
        left = base.copy(); left[y0:y1 + 1, x0 - 1] += 50                 # one column left of the box
        r = {"box": [x0, y0, x1, y1],
             "insideMasked": differs(*mask_pair(base, inside)),
             "insideUnmasked": differs(base, inside),
             "belowMasked": differs(*mask_pair(base, below)),
             "leftMasked": differs(*mask_pair(base, left))}
        good = r["insideMasked"] == 0 and r["insideUnmasked"] > 0 and r["belowMasked"] > 0 and r["leftMasked"] > 0
        print(f"{w}x{h}: {json.dumps(r)} -> {'PASS' if good else 'FAIL'}")
        ok.append(good)
    # The scaled box must never be smaller than BOX: its edges mapped back to 2560x1440 lie on or outside BOX.
    for w, h in ((320, 180), (128, 72), (1920, 1080)):
        x0, y0, x1, y1 = box_at(w, h)
        good = (x0 * 2560 / w <= BOX[0] and y0 * 1440 / h <= BOX[1]
                and (x1 + 1) * 2560 / w - 1 >= BOX[2] and (y1 + 1) * 1440 / h - 1 >= BOX[3])
        print(f"{w}x{h} box {x0},{y0},{x1},{y1} covers BOX: {good}")
        ok.append(good)
    print(f"{sum(ok)} of {len(ok)} pass")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(selftest())
    print(__doc__)
    sys.exit(2)
