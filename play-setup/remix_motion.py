"""Stutter and flicker from a screen recording of the game (ffmpeg ddagrab at 120 fps, lossless).

What the player actually sees, not what the game or the proxy reports: a new frame on screen = the picture changed
from the previous captured frame. While the camera moves, a run of identical captures = the screen froze (hitch).
Usage: python -I remix_motion.py clip.mkv [out_prefix]
Prints: displayed-frame intervals (avg, p99, max, count > 33 ms / > 50 ms) and the worst hitches with their times;
writes <out_prefix>_hitches.png (frames before/after the worst hitches) and <out_prefix>_timeline.png (change per capture).
"""
import subprocess, sys
import numpy as np
from PIL import Image, ImageDraw

clip = sys.argv[1]
prefix = sys.argv[2] if len(sys.argv) > 2 else clip.rsplit('.', 1)[0]
W, H = 480, 270
raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', clip, '-vf', f'scale={W}:{H}', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                     capture_output=True, check=True).stdout
fps = 120.0
n = len(raw) // (W * H)
f = np.frombuffer(raw[: n * W * H], np.uint8).reshape(n, H, W).astype(np.float32)
# ignore the overlay corner (fps counter) so it doesn't count as a change
f[:, :20, W - 140:] = 0
d = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))          # change between consecutive captures
new = np.concatenate([[True], d > 0.15])                    # a new picture reached the screen
times = np.nonzero(new)[0] / fps * 1000.0
iv = np.diff(times)
moving = d > 0.15
print(f'captures {n} ({n / fps:.1f} s), new pictures {new.sum()} -> {new.sum() / (n / fps):.1f} fps on screen')
if len(iv):
    s = np.sort(iv)
    print(f'interval avg {iv.mean():.1f} ms  p99 {s[int(len(s) * 0.99)]:.1f}  max {s[-1]:.1f}  >33ms {int((iv > 33.4).sum())}  >50ms {int((iv > 50).sum())}')
worst = np.argsort(iv)[::-1][:6]
print('worst hitches (start s, ms):', ', '.join(f'{times[i] / 1000:.2f}s {iv[i]:.0f}' for i in worst))
# timeline image
tl = Image.new('RGB', (min(n, 1200), 200), 'black'); dr = ImageDraw.Draw(tl)
scale = min(n, 1200) / n
for i, v in enumerate(d):
    x = int(i * scale); dr.line([(x, 199), (x, 199 - min(199, v * 20))], fill=(80, 200, 255))
for i in worst:
    x = int(times[i] / 1000 * fps * scale); dr.line([(x, 0), (x, 30)], fill=(255, 60, 60), width=2)
tl.save(prefix + '_timeline.png')
# frames around the worst hitches (in colour)
cols = []
for i in worst[:4]:
    a = int(times[i] / 1000 * fps); b = int(times[i + 1] / 1000 * fps)
    pics = []
    for k in (a, b):
        png = subprocess.run(['ffmpeg', '-v', 'error', '-i', clip, '-vf', f'select=eq(n\\,{k}),scale=480:-2', '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'],
                             capture_output=True).stdout
        if png:
            import io; pics.append(Image.open(io.BytesIO(png)).convert('RGB'))
    if len(pics) == 2:
        row = Image.new('RGB', (960, pics[0].height)); row.paste(pics[0], (0, 0)); row.paste(pics[1], (480, 0)); cols.append(row)
if cols:
    sheet = Image.new('RGB', (960, sum(c.height for c in cols))); y = 0
    for c in cols: sheet.paste(c, (0, y)); y += c.height
    sheet.save(prefix + '_hitches.png')
