"""Numeric check of [Proxy] HandoffBeforePost=1 (ray-traced image fed into the game's own post-processing).
usage: rca_handoff_check.py <capture folder> [proxy log]
Expects (camera still within each group):
  hb_game, hb_game2   ray tracing off, twice (the second gives the motion mask)
  hb_old              ray tracing on, HandoffBeforePost=0 (hand-off after the composite)
  hb_new              ray tracing on, HandoffBeforePost=1
  nv_new, nv_game     night vision on: HandoffBeforePost=1 with ray tracing on / ray tracing off
Checks:
  1. log: while the mode is on, the PassFilter stats lines report hand-offs "before post-processing".
  2. hb_new is ray traced (differs from the game's picture outside motion) yet has the game's look: mean brightness
     within 25% of hb_game and thumbnail correlation >= 0.85. hb_old is printed for comparison.
  3. nv_new shows the game's night vision: green dominant (G >= 1.5 x R and B) and close to nv_game (thumbnail
     correlation >= 0.7, channel means within 0.10).
"""
import os
import re
import sys
import numpy as np
from PIL import Image
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
folder = sys.argv[1]
log = sys.argv[2] if len(sys.argv) > 2 else os.path.join(folder, 'd3d9_proxy.log')
ok = True


def verdict(name, good, text):
    global ok
    ok &= good
    print(f"[{'PASS' if good else 'FAIL'}] {name}: {text}")


def img(label):
    p = os.path.join(folder, label + '.png')
    return np.asarray(Image.open(p).convert('RGB')).astype(np.float32) / 255 if os.path.exists(p) else None


def thumb(a):
    return np.asarray(Image.fromarray((a * 255).astype(np.uint8)).resize((64, 36), Image.BOX)).astype(np.float32) / 255


lum = lambda a: a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722


def corr(a, b):
    return float(np.corrcoef(lum(thumb(a)).ravel(), lum(thumb(b)).ravel())[0, 1])


# 1. log
on, early, total = False, [], []
if os.path.exists(log):
    for line in open(log, errors='replace'):
        m = re.search(r'HandoffBeforePost: now (\d)', line)
        if m:
            on = m.group(1) == '1'
        m = re.search(r'PassFilter: last 600 frames (\d+) with a 3D scene.*?(\d+) hand-offs \((\d+) before post', line)
        if m and on and int(m.group(1)) > 0:
            early.append(int(m.group(3))); total.append(int(m.group(2)))
verdict('1 log: early hand-off happens', bool(early) and max(early) > 0,
        f"{len(early)} stats lines while on; before post-processing {early[:8]} of hand-offs {total[:8]}")

# 2. normal view
game, game2, old, new = img('hb_game'), img('hb_game2'), img('hb_old'), img('hb_new')
if game is None or new is None:
    verdict('2 normal view', False, 'hb_game.png or hb_new.png missing')
else:
    motion = np.zeros(game.shape[:2], bool)
    if game2 is not None:
        motion = np.abs(game - game2).max(axis=2) > 0.05
    still = ~motion
    diff_new = float(np.abs(new - game).max(axis=2)[still].mean())
    ratio_new = float(lum(new)[still].mean() / max(lum(game)[still].mean(), 1e-4))
    c_new = corr(new, game)
    verdict('2 hb_new is ray traced', diff_new > 0.01, f"mean difference from the game's picture {diff_new * 255:.1f}/255 "
            f"outside {motion.sum()} moving pixels")
    verdict('2 hb_new has the game look', 0.75 <= ratio_new <= 1.25 and c_new >= 0.85,
            f"brightness x{ratio_new:.2f} of the game's, thumbnail correlation {c_new:.2f}")
    if old is not None:
        print(f"[INFO] hb_old (hand-off after the composite): brightness x{lum(old)[still].mean() / max(lum(game)[still].mean(), 1e-4):.2f}, "
              f"correlation {corr(old, game):.2f}")

# 3. night vision
nv, nvg = img('nv_new'), img('nv_game')
if nv is None or nvg is None:
    verdict('3 night vision', False, 'nv_new.png or nv_game.png missing')
else:
    m, mg = nv.reshape(-1, 3).mean(0), nvg.reshape(-1, 3).mean(0)
    green = m[1] >= 1.5 * m[0] and m[1] >= 1.5 * m[2]
    c = corr(nv, nvg)
    verdict('3 night vision over the RT image', green and c >= 0.7 and float(np.abs(m - mg).max()) <= 0.10,
            f"mean RGB RT {np.round(m, 3)} vs game {np.round(mg, 3)}, thumbnail correlation {c:.2f}")
print('OVERALL', 'PASS' if ok else 'FAIL')
sys.exit(0 if ok else 1)
