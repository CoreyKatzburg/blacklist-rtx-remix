"""Numeric check that the game's decals reach Remix as decals (no looking at pictures).
usage: rca_decal_check.py <capture folder> [proxy log]
Expects these captures from rca_capture.ps1, all from the same camera position:
  dk_game        ray tracing off                         (the game's own picture)
  dk_game2       the same again, right after dk_game     (what differs between the two is motion, left out)
  dk_game_hide   ray tracing off, Proxy:HideDecals=1      (the game's picture without its decals)
  dk_dbg_tag     ray tracing on, DecalTag=1, rtx.debugView.debugViewIdx=88 (Remix's decal colour only)
  dk_dbg_notag   ray tracing on, DecalTag=0, debug view 88
  dk_rt          ray tracing on, DecalTag=1               (final picture)
  dk_rt_hide     ray tracing on, DecalTag=1, HideDecals=1
Checks:
  A. frame dump of dk_rt: every draw from a decal renderer is sent to ray tracing (scene=1, no bracket, before the
     hand-off), and the log has a hash for its texture and a "sent ... hashes" line.
  B. Remix shows decal colour only when tagged: decal pixels in dk_dbg_tag vs dk_dbg_notag.
  C. Remix's decal pixels sit where the game draws decals: overlap with the game's own decal mask.
  D. Darkening: how much the decals darken the picture in the game vs in the ray-traced picture.
"""
import os
import re
import sys
import numpy as np
from PIL import Image
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
folder = sys.argv[1]
log = sys.argv[2] if len(sys.argv) > 2 else os.path.join(folder, 'd3d9_proxy.log')
T = 12          # 8-bit difference that counts as "changed"
ok = True


def img(label):
    p = os.path.join(folder, label + '.png')
    if not os.path.exists(p):
        return None
    return np.asarray(Image.open(p).convert('RGB')).astype(np.int16)


def dilate(mask, r):
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= np.roll(np.roll(mask, dy, 0), dx, 1)
    return out


def verdict(name, good, text):
    global ok
    ok &= good
    print(f"[{'PASS' if good else 'FAIL'}] {name}: {text}")


# A. dump + log
dump = os.path.join(folder, 'dk_rt_draws.tsv')
if os.path.exists(dump):
    decals = []
    for line in open(dump, errors='replace'):
        if line.startswith('draw\t'):
            d = dict(x.split('=', 1) for x in line.rstrip('\n').split('\t')[1:] if '=' in x)
            if d.get('decal') == '1' and d.get('cw') != '0':   # colour write off: Remix skips it ("colour write disabled")
                decals.append(d)
    traced = [d for d in decals if d.get('scene') == '1' and d.get('bracket') == '0' and d.get('handoffdone') == '0']
    textures = sorted({d['t0'].split(',')[0].upper().lstrip('0') for d in decals if d.get('t0')})
    hashed, sent = {}, 0
    if os.path.exists(log):
        for line in open(log, errors='replace'):
            m = re.search(r'DecalTag: texture ([0-9A-Fa-f]+) .* -> hash (0x[0-9A-F]{16})', line)
            if m:
                hashed[m.group(1).upper().lstrip('0')] = m.group(2)
            m = re.search(r'DecalTag: sent (\d+) decal texture hashes to Remix \(ok\)', line)
            if m:
                sent = max(sent, int(m.group(1)))
    missing = [t for t in textures if t not in hashed]
    verdict('A decal draws', bool(decals) and len(traced) == len(decals),
            f"{len(decals)} decal draws in the frame, {len(traced)} sent to ray tracing before the hand-off")
    verdict('A texture hashes', bool(textures) and not missing and sent > 0,
            f"{len(textures)} decal textures, {len(textures) - len(missing)} hashed {[hashed.get(t) for t in textures]}, "
            f"largest list sent: {sent}" + (f", missing {missing}" if missing else ''))
else:
    verdict('A frame dump', False, f"{dump} not found")

game, hide = img('dk_game'), img('dk_game_hide')
tag, notag = img('dk_dbg_tag'), img('dk_dbg_notag')
rt, rt_hide = img('dk_rt'), img('dk_rt_hide')
if game is None or hide is None or tag is None or notag is None:
    verdict('images', False, 'one of dk_game, dk_game_hide, dk_dbg_tag, dk_dbg_notag is missing')
    sys.exit(1)
# Sam's idle animation and other motion differ between any two captures (run 4: the "decal" mask was mostly Sam).
# dk_game2 is a second capture with exactly dk_game's settings: whatever differs between those two is motion.
game2 = img('dk_game2')
motion = np.zeros(game.shape[:2], bool)
if game2 is not None:
    motion = dilate(np.abs(game - game2).max(axis=2) > T, 8)
    print(f"motion pixels excluded (dk_game vs dk_game2): {motion.sum()}")
else:
    print("[WARN] no dk_game2 capture: motion between captures is not excluded")
T = 6           # decals in this game are faint (they darken by about a quarter)
game_mask = (np.abs(game - hide).max(axis=2) > T) & ~motion       # where the game's decals change the picture
# Remix decal pixels: bright in the decal debug view, and not there when untagged (removes HUD and overlays)
remix_mask = (tag.max(axis=2) > T) & (np.abs(tag - notag).max(axis=2) > T) & ~motion
untagged = (notag.max(axis=2) > T) & ~(game.max(axis=2) > 250)   # anything decal-like without tagging
print(f"game decal pixels {game_mask.sum()}, Remix decal pixels tagged {remix_mask.sum()}, "
      f"debug-view pixels untagged {untagged.sum()} (HUD included)")
# B
verdict('B Remix decals only when tagged', remix_mask.sum() > 200,
        f"{remix_mask.sum()} pixels show decal colour with DecalTag=1 that are absent with DecalTag=0")
# C (dilate: Remix renders at a lower resolution and is upscaled, edges shift a few pixels)
if game_mask.sum() and remix_mask.sum():
    precision = (remix_mask & dilate(game_mask, 6)).sum() / remix_mask.sum()
    recall = (game_mask & dilate(remix_mask, 6)).sum() / game_mask.sum()
    verdict('C same place as the game', precision >= 0.8 and recall >= 0.5,
            f"{precision:.0%} of Remix's decal pixels lie on the game's decals; {recall:.0%} of the game's decal pixels are covered")
else:
    verdict('C same place as the game', False, 'no decal pixels to compare (no decal on screen?)')
# D
if rt is not None and rt_hide is not None and game_mask.sum():
    lum = lambda a: (a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722) / 255.0
    m = game_mask if game_mask.sum() else game_mask | True
    g_ratio = lum(game)[m].mean() / max(lum(hide)[m].mean(), 1e-3)
    r_ratio = lum(rt)[m].mean() / max(lum(rt_hide)[m].mean(), 1e-3)
    print(f"[INFO] D darkening on the decal pixels: game x{g_ratio:.2f}, ray traced x{r_ratio:.2f} (1.00 = no change)")
print('OVERALL', 'PASS' if ok else 'FAIL')
sys.exit(0 if ok else 1)
