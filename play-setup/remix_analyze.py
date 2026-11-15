"""Analysis for remix_autotest.ps1 (all input is what actually reached the screen, captured with ffmpeg ddagrab).

  motion <clip.mkv> <segments>   segments = "name:start:end,..." in seconds (camera moving the whole time)
      per segment: pictures/s on screen, longest freeze, freezes > 33 ms / > 50 ms, flicker
      (flicker = change between consecutive new pictures after compensating the camera pan, divided by the region's
       texture contrast; edges that shimmer, boil or pop make it go up; the same pan with ray tracing off is the reference)
  views <dir>                    pairs on_<k>.png / off_<k>.png (same camera, ray tracing on / off): per 40x40 block,
      flags blocks where the game's own image has clear structure but the ray-traced one is nearly flat or dark
      (= geometry Remix doesn't show), writes views_sheet.png with flagged blocks outlined in red.
Prints one JSON line per result for the PowerShell side.
"""
import io, json, subprocess, sys
import numpy as np
from PIL import Image, ImageDraw

W, H = 640, 360


def frames(clip):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', clip, '-vf', f'scale={W}:{H}', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                         capture_output=True, check=True).stdout
    n = len(raw) // (W * H)
    f = np.frombuffer(raw[: n * W * H], np.uint8).reshape(n, H, W).astype(np.float32)
    f[:, :24, W - 190:] = 0          # Remix fps overlay
    f[:, H - 90:, :] = 0             # HUD strip (minimap, ammo)
    return f


def shift(a, b):
    F = np.fft.fft2(a - a.mean()) * np.conj(np.fft.fft2(b - b.mean()))
    r = np.fft.ifft2(F / (np.abs(F) + 1e-6)).real
    dy, dx = np.unravel_index(np.argmax(r), r.shape)
    if dy > r.shape[0] // 2: dy -= r.shape[0]
    if dx > r.shape[1] // 2: dx -= r.shape[1]
    return dy, dx


def moving_windows(f, d, fps):
    """Where the camera really moved, found from the pictures themselves (scripted timings drift): consecutive new
    pictures whose content is shifted. Returns [(start_capture, end_capture), ...]."""
    new = [0] + [i + 1 for i in range(len(d)) if d[i] > 0.15]
    cy, cx = slice(60, H - 120), slice(120, W - 120)
    mov = []
    for p, q in zip(new[:-1], new[1:]):
        dy, dx = shift(f[p][cy, cx], f[q][cy, cx])
        mov.append((p, q, abs(dx) + abs(dy) >= 1))
    wins, cur = [], None
    for p, q, m in mov:
        if m:
            cur = [p, q] if cur is None else [cur[0], q]
        else:
            # a single unshifted pair inside a pan is a freeze candidate only if motion resumes: keep window open briefly
            if cur is not None and q - cur[1] > int(0.6 * fps):
                wins.append(tuple(cur)); cur = None
    if cur is not None: wins.append(tuple(cur))
    return [w for w in wins if w[1] - w[0] > int(0.3 * fps)]


def motion(clip, segs, fps=None):
    f = frames(clip)
    if fps is None:   # real capture rate (ddagrab 120; gdigrab only ~30 at full res when the monitor is asleep)
        dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', clip],
                                   capture_output=True, text=True).stdout.strip() or 0)
        fps = len(f) / dur if dur > 0 else 120.0
    d = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))
    out = []
    if segs == 'auto':
        segs = ','.join(f'move{i}:{a / fps:.3f}:{b / fps:.3f}' for i, (a, b) in enumerate(moving_windows(f, d, fps)))
    for seg in segs.split(','):
        name, a, b = seg.split(':'); a = int(float(a) * fps); b = min(int(float(b) * fps), len(d))
        idx = [i + 1 for i in range(a, b) if d[i] > 0.15]
        res = {'kind': 'motion', 'segment': name, 'seconds': round((b - a) / fps, 2)}
        if len(idx) < 3:
            res.update(pictures_per_s=0, longest_freeze_ms=round((b - a) / fps * 1000), freezes_33=1, freezes_50=1, flicker=None)
            out.append(res); continue
        t = np.array(idx) / fps * 1000
        gaps = np.diff(np.concatenate([[a / fps * 1000], t, [b / fps * 1000]]))
        flick = []
        cy, cx = slice(60, H - 120), slice(120, W - 120)
        for p, q in zip(idx[:-1], idx[1:]):
            A, B = f[p][cy, cx], f[q][cy, cx]
            dy, dx = shift(A, B)
            if abs(dx) > 60 or abs(dy) > 40: continue
            Bs = np.roll(B, (dy, dx), axis=(0, 1))
            m = 64
            ra, rb = A[m:-m, m:-m], Bs[m:-m, m:-m]
            contrast = ra.std() + 1e-3
            flick.append(np.abs(ra - rb).mean() / contrast)
        res.update(pictures_per_s=round(len(idx) / ((b - a) / fps), 1), longest_freeze_ms=round(float(gaps.max()), 1),
                   freezes_33=int((gaps > 33.4).sum()), freezes_50=int((gaps > 50).sum()),
                   flicker=round(float(np.median(flick)), 4) if flick else None)
        out.append(res)
    return out


def views(folder):
    import glob, os
    ons = sorted(glob.glob(os.path.join(folder, 'on_*.png')))
    tiles, out = [], []
    for on in ons:
        k = os.path.basename(on)[3:-4]
        off = os.path.join(folder, f'off_{k}.png')
        A = np.asarray(Image.open(on).convert('L').resize((W, H))).astype(np.float32)
        B = np.asarray(Image.open(off).convert('L').resize((W, H))).astype(np.float32)
        img = Image.open(on).convert('RGB').resize((W, H)); dr = ImageDraw.Draw(img)
        flagged = 0; total = 0
        for y in range(40, H - 120, 40):
            for x in range(0, W, 40):
                a, b = A[y:y + 40, x:x + 40], B[y:y + 40, x:x + 40]
                ga = np.abs(np.diff(a, axis=1)).mean() + np.abs(np.diff(a, axis=0)).mean()
                gb = np.abs(np.diff(b, axis=1)).mean() + np.abs(np.diff(b, axis=0)).mean()
                total += 1
                # the game's own image has structure (texture/edges) but the ray-traced block is nearly featureless
                if gb > 6 and ga < 0.25 * gb:
                    flagged += 1; dr.rectangle([x, y, x + 39, y + 39], outline=(255, 40, 40), width=2)
        pair = Image.new('RGB', (2 * W, H)); pair.paste(img, (0, 0)); pair.paste(Image.open(off).convert('RGB').resize((W, H)), (W, 0))
        tiles.append(pair)
        out.append({'kind': 'view', 'view': k, 'missing_blocks': flagged, 'blocks': total})
    if tiles:
        sheet = Image.new('RGB', (2 * W, H * len(tiles)))
        for i, t in enumerate(tiles): sheet.paste(t, (0, i * H))
        sheet.save(os.path.join(folder, 'views_sheet.png'))
    return out


if __name__ == '__main__':
    res = motion(sys.argv[2], sys.argv[3]) if sys.argv[1] == 'motion' else views(sys.argv[2])
    for r in res: print(json.dumps(r))
