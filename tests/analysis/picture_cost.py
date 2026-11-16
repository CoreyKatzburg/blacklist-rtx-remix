"""Picture cost of a speed setting, as numbers: a variant's final frame against the base Remix frame at the same pose.

Usage:
  picture_cost.py --self-test
  picture_cost.py BASE.pfm VARIANT.pfm [--floor OTHER_BASE.pfm ...]

Both images are the gate's y0_p0_final.pfm (RGB float). Each is turned into luminance and divided by its own
median, so an exposure change alone does not count as lost detail (the exposure ratio is reported on its own).

Measures, per 64x64 tile that has texture in the base (base detail energy above the 25th percentile):
  detail    variant detail energy / base detail energy, where detail = luminance minus a ~1.5 px blur of it.
            1.00 = same fine detail; below 1 = detail lost (blur, upscale softness); above 1 = added noise
            or sharpening. Reported as the median tile and the 10th percentile tile (the worst areas).
  struct    1 - SSIM between the two normalized luminance images (8 px windows), median over the textured tiles.
            Sensitive to a small pose difference between launches, so it is only meaningful above the floor.
The noise floor is the same numbers for base against another base launch; a variant costs picture only where
it falls outside that floor.

Pose check: the gate's y0_p0 pose is not repeatable between launches (Nov 16 sweep: base against base, the game
depth y0_p0_gamedepth.pfm differs by more than 0.01 in 31 to 50% of pixels; 05 against base in 86%). So each pair
is compared only when the y0_p0_gamedepth.pfm next to both images matches (at most POSE_MAX_PIXELS of pixels
differ by more than 0.01); otherwise the pair prints POSE DIFFERS and no cost. Use two dumps from one launch with
the setting switched live at a still camera.

Not measured here: smear in motion. y0_p0 is a still pose, so ghosting needs two dumps while the camera turns.
"""
import os
import sys
import numpy as np

POSE_MAX_PIXELS = 0.01

TILE = 64


def read_pfm(path):
    with open(path, 'rb') as f:
        kind = f.readline().strip()
        if kind not in (b'PF', b'Pf'):
            raise ValueError(f'{path}: not a PFM file')
        w, h = map(int, f.readline().split())
        scale = float(f.readline().strip())
        data = np.fromfile(f, dtype='<f4' if scale < 0 else '>f4')
    channels = 3 if kind == b'PF' else 1
    img = data[: w * h * channels].reshape(h, w, channels)[::-1]
    return img.astype(np.float64)


def luminance(img):
    if img.shape[2] == 1:
        lum = img[:, :, 0]
    else:
        lum = 0.2126 * img[:, :, 0] + 0.7152 * img[:, :, 1] + 0.0722 * img[:, :, 2]
    return np.nan_to_num(np.maximum(lum, 0.0), nan=0.0, posinf=0.0)


def box(a, r):
    """Mean over a (2r+1)^2 window, edges clamped, via cumulative sums."""
    p = np.pad(a, r + 1, mode='edge')
    c = p.cumsum(0).cumsum(1)
    n = 2 * r + 1
    s = c[n:, n:] - c[:-n, n:] - c[n:, :-n] + c[:-n, :-n]
    return s[: a.shape[0], : a.shape[1]] / (n * n)


def blur(a):
    return box(box(a, 1), 1)  # two 3x3 boxes, close to a 1.5 px Gaussian


def tiles(a):
    h, w = (a.shape[0] // TILE) * TILE, (a.shape[1] // TILE) * TILE
    return a[:h, :w].reshape(h // TILE, TILE, w // TILE, TILE).mean(axis=(1, 3))


def ssim_map(x, y):
    c1, c2 = 0.01 ** 2, 0.03 ** 2
    mx, my = box(x, 4), box(y, 4)
    vx = box(x * x, 4) - mx * mx
    vy = box(y * y, 4) - my * my
    cxy = box(x * y, 4) - mx * my
    return ((2 * mx * my + c1) * (2 * cxy + c2)) / ((mx * mx + my * my + c1) * (vx + vy + c2))


def compare(base_lum, var_lum):
    bm, vm = np.median(base_lum), np.median(var_lum)
    b = base_lum / max(bm, 1e-9)
    v = var_lum / max(vm, 1e-9)
    # Compress highlights the same way for both, so a few bright pixels do not own the energy sums.
    b, v = b / (1 + b), v / (1 + v)
    eb = tiles((b - blur(b)) ** 2)
    ev = tiles((v - blur(v)) ** 2)
    textured = eb > np.percentile(eb, 25)
    ratio = ev[textured] / np.maximum(eb[textured], 1e-12)
    st = tiles(1 - ssim_map(b, v))[textured]
    return {
        'exposure': vm / max(bm, 1e-9),
        'detail_median': float(np.median(ratio)),
        'detail_p10': float(np.percentile(ratio, 10)),
        'struct_median': float(np.median(st)),
        'tiles': int(textured.sum()),
    }


def fmt(name, r):
    return (f"{name:<28} detail median {r['detail_median']:.3f}  worst10% {r['detail_p10']:.3f}  "
            f"struct {r['struct_median']:.4f}  exposure x{r['exposure']:.3f}  ({r['tiles']} tiles)")


def self_test():
    rng = np.random.default_rng(7)
    # A synthetic "scene": smooth shading, hard edges, and fine texture, with a few bright highlights.
    h, w = 512, 768
    yy, xx = np.mgrid[0:h, 0:w]
    scene = 0.3 + 0.2 * np.sin(xx / 90.0) * np.cos(yy / 70.0)
    scene += 0.25 * ((xx // 48 + yy // 48) % 2)
    scene += 0.08 * rng.standard_normal((h, w))
    scene[100:104, 200:400] = 20.0
    scene = np.maximum(scene, 0.0)
    noisy_base = scene + 0.004 * rng.standard_normal((h, w))  # "another launch": tiny noise only
    cases = {
        'identical': (scene.copy(), lambda r: abs(r['detail_median'] - 1) < 1e-9 and r['struct_median'] < 1e-9),
        'another base (floor)': (noisy_base, lambda r: 0.95 < r['detail_median'] < 1.05 and r['struct_median'] < 0.02),
        'exposure x2 only': (scene * 2.0, lambda r: 0.95 < r['detail_median'] < 1.05 and r['struct_median'] < 0.02
                             and 1.9 < r['exposure'] < 2.1),
        'blurred (detail lost)': (blur(blur(scene)), lambda r: r['detail_median'] < 0.6),
        'noise added': (scene + 0.08 * rng.standard_normal((h, w)), lambda r: r['detail_median'] > 1.3),
    }
    ok = True
    results = {}
    for name, (img, rule) in cases.items():
        r = compare(scene, np.maximum(img, 0.0))
        results[name] = r
        good = rule(r)
        ok &= good
        print(('ok    ' if good else 'FAIL  ') + fmt(name, r))
    # The measure must separate a real loss from the floor: blur must fall outside base-vs-base.
    floor = results['another base (floor)']
    sep = results['blurred (detail lost)']['detail_median'] < floor['detail_median'] - 0.2
    ok &= sep
    print(('ok    ' if sep else 'FAIL  ') + 'blurred copy falls outside the base-vs-base floor')
    # Pose check: the same depth passes; a depth from a camera moved by one pixel must be refused.
    depth = 150 + 250 * (yy / h) + 30 * ((xx // 40) % 2)
    same = pose_differs(depth, depth.copy())
    moved = pose_differs(depth, np.roll(depth, 1, axis=1))
    pose_ok = same == 0.0 and moved > POSE_MAX_PIXELS
    ok &= pose_ok
    print(('ok    ' if pose_ok else 'FAIL  ') + f'pose check: same depth {same:.3f}, camera moved 1 px {moved:.3f} '
          f'(limit {POSE_MAX_PIXELS})')
    print('self-test PASS' if ok else 'self-test FAIL')
    return 0 if ok else 1


def main(argv):
    if argv[:1] == ['--self-test']:
        return self_test()
    if len(argv) < 2:
        print(__doc__)
        return 2
    floors = []
    if '--floor' in argv:
        i = argv.index('--floor')
        floors = argv[i + 1:]
        argv = argv[:i]
    base = luminance(read_pfm(argv[0]))
    base_depth = game_depth(argv[0])
    for label, paths in (('floor ', floors), ('', argv[1:])):
        for path in paths:
            name = label + path.split('\\')[-3]
            moved = pose_differs(base_depth, game_depth(path))
            if moved is not None and moved > POSE_MAX_PIXELS:
                print(f'{name:<28} POSE DIFFERS: game depth differs in {moved * 100:.1f}% of pixels, no cost')
                continue
            print(fmt(name, compare(base, luminance(read_pfm(path)))))
    return 0


def game_depth(final_path):
    path = os.path.join(os.path.dirname(final_path), 'y0_p0_gamedepth.pfm')
    return read_pfm(path)[:, :, 0] if os.path.exists(path) else None


def pose_differs(a, b):
    """Share of pixels whose game depth differs by more than 0.01, or None when a depth dump is missing."""
    if a is None or b is None or a.shape != b.shape:
        return None
    return float((np.abs(a - b) > 0.01).mean())


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
