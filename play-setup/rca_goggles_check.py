"""Numeric check that night vision and thermal look like the normal game while ray tracing is on (GogglesGameView).
usage: rca_goggles_check.py <capture folder> [proxy log]
Expects, each pair from the same camera position:
  gg_nv_game / gg_nv_rt      night vision on, ray tracing off / on
  gg_th_game / gg_th_rt      thermal on, ray tracing off / on
Checks:
  1. each _rt picture matches its _game picture: per-channel means within 0.06 and, on a 64x36 thumbnail (averages
     away the animated goggle noise), luminance correlation >= 0.8.
  2. the log shows "Goggles: on" followed by "AutoRaytracing -> off", and the last goggle line is "Goggles: off" followed
     by "AutoRaytracing -> on" (ray tracing came back after the goggles were switched off).
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


def load(label):
    p = os.path.join(folder, label + '.png')
    if not os.path.exists(p):
        return None
    im = Image.open(p).convert('RGB')
    return np.asarray(im).astype(np.float32) / 255, np.asarray(im.resize((64, 36), Image.BOX)).astype(np.float32) / 255


for mode, game, rt in (('night vision', 'gg_nv_game', 'gg_nv_rt'), ('thermal', 'gg_th_game', 'gg_th_rt')):
    a, b = load(game), load(rt)
    if a is None or b is None:
        verdict(mode, False, f"{game}.png or {rt}.png missing")
        continue
    ma, mb = a[0].reshape(-1, 3).mean(0), b[0].reshape(-1, 3).mean(0)
    la, lb = (a[1] @ [0.2126, 0.7152, 0.0722]).ravel(), (b[1] @ [0.2126, 0.7152, 0.0722]).ravel()
    corr = float(np.corrcoef(la, lb)[0, 1])
    diff = float(np.abs(ma - mb).max())
    verdict(mode, diff <= 0.06 and corr >= 0.8,
            f"mean RGB game {np.round(ma, 3)} vs RT on {np.round(mb, 3)} (largest gap {diff:.3f}), thumbnail correlation {corr:.2f}")

events = []
if os.path.exists(log):
    for line in open(log, errors='replace'):
        m = re.search(r'Goggles: (on|off)|AutoRaytracing -> (on|off)', line)
        if m:
            events.append(('goggles ' + m.group(1)) if m.group(1) else ('rt ' + m.group(2)))
on_then_off = any(events[i] == 'goggles on' and 'rt off' in events[i + 1:i + 3] for i in range(len(events)))
last = max((i for i, e in enumerate(events) if e.startswith('goggles')), default=-1)
back_on = last >= 0 and events[last] == 'goggles off' and 'rt on' in events[last + 1:]
verdict('log: game view while goggles on', on_then_off, f"{events.count('goggles on')} 'Goggles: on' lines")
verdict('log: ray tracing back after goggles off', back_on, 'event tail: ' + ', '.join(events[-6:]))
print('OVERALL', 'PASS' if ok else 'FAIL')
sys.exit(0 if ok else 1)
