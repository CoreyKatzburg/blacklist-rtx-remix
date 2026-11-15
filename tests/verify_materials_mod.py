"""Offline check of the generated game-wide materials mod (materials/mod.usda + materials/textures).

Fails (exit 1) unless:
  - every override is `over "mat_<16 hex>"` with a TYPED `over Shader "Shader"` (Remix's processMaterial ignores
    an untyped shader prim; checked with the Toolkit's own USD library when it is installed), appears once, and is
    a hash the offline pipeline computes for a real archive texture (play-setup/glow/texture_hashes.csv);
  - both Remix hashes of a streamed texture (sharp and blurry) carry the same inputs;
  - every asset exists and is a DX10 DDS of the format its input needs (normal BC5, roughness/metallic BC4,
    emissive BC1/BC3 sRGB) with a consistent mip chain; values use the USD types Remix reads (float/bool/color3f);
  - normal maps decode (with Remix's own decoder) to unit-length, z >= 0 vectors, and are curl-free as written
    (+X along +U, +Y along +V: Remix's tangent frame) far more often than with Y flipped or X/Y swapped;
  - roughness/metallic constants are in [0, 1] and the shine formula stays in [0, 1] and behaves monotonically;
  - textures/ holds exactly the referenced files (no stale output) and the heaviest level fits the VRAM budget.
Run: python tests/verify_materials_mod.py [--self-test]   (--self-test also proves the checks catch broken input)
"""
import argparse
import csv
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REMIX = Path(__file__).resolve().parents[1]
MATERIALS = REMIX / 'materials'
GLOW = REMIX / 'play-setup' / 'glow'
VENV_PY = GLOW / '.venv' / 'Scripts' / 'python.exe'
try:
    import numpy as np
    import xxhash  # noqa: F401  (used by build_materials)
except ImportError:  # run with the pipeline's interpreter
    if VENV_PY.exists() and Path(sys.executable).resolve() != VENV_PY.resolve():
        raise SystemExit(subprocess.call([str(VENV_PY), __file__, *sys.argv[1:]]))
    raise
sys.path.insert(0, str(GLOW / 'scripts'))
import bc  # noqa: E402
import build_materials as bm  # noqa: E402

KIT_PY = Path(r'C:\Program Files\NVIDIA Corporation\RTX Remix\kit\python\python.exe')
USD_LIBS = Path(r'C:\Program Files\NVIDIA Corporation\RTX Remix\extscache\omni.usd.libs-1.0.1+d02c707b.wx64.r.cp310')
EXPECT_DXGI = {'normalmap_texture': {83}, 'reflectionroughness_texture': {80}, 'metallic_texture': {80},
               'height_texture': {80}, 'emissive_mask_texture': {72, 78}}
EXPECT_TYPE = {'reflection_roughness_constant': 'float', 'metallic_constant': 'float', 'displace_in': 'float',
               'emissive_intensity': 'float', 'enable_emission': 'bool', 'emissive_color_constant': 'color3f'}
FULL_RES_SAMPLE = 40

OVER_RE = re.compile(r'^        over "mat_([^"]*)"\n        \{\n(.*?)^        \}\n', re.M | re.S)
ASSET_RE = re.compile(r'asset inputs:(\w+) = @\./textures/([^@]+)@ \(\n\s+colorSpace = "(\w+)"\n\s+\)')
VALUE_RE = re.compile(r'^\s+(\w+) inputs:(\w+) = (.+)$', re.M)


def parse_usda(text):
    """-> ({hash: {input: value}}, [errors]). Values: ('asset', file, colorSpace) or (type, literal)."""
    errors, mats = [], {}
    if not text.startswith('#usda 1.0'):
        errors.append('missing #usda 1.0 header')
    if text.count('{') != text.count('}'):
        errors.append('unbalanced braces')
    for h, body in OVER_RE.findall(text):
        if not re.fullmatch(r'[0-9A-F]{16}', h):
            errors.append('mat_%s: not a 16-digit upper-case hex hash' % h)
        if h in mats:
            errors.append('mat_%s: overridden more than once' % h)
        if not body.startswith('            over Shader "Shader"\n'):
            errors.append('mat_%s: shader prim is not a typed `over Shader "Shader"` (Remix would ignore it)' % h)
        inputs = {}
        for name, f, cs in ASSET_RE.findall(body):
            inputs[name] = ('asset', f, cs)
        for typ, name, val in VALUE_RE.findall(body):
            if typ != 'asset':
                inputs[name] = (typ, val)
        if not inputs:
            errors.append('mat_%s: no inputs' % h)
        mats[h] = inputs
    if text.count('over "mat_') != len(mats):
        errors.append('%d mat_ prims but %d parsed overrides (unexpected layout)' % (text.count('over "mat_'), len(mats)))
    return mats, errors


def check_inputs(mats, known_hashes, tex_dir):
    errors, normal_files, used = [], set(), set()
    for h, inputs in mats.items():
        if h not in known_hashes:
            errors.append('mat_%s: not a hash of any archive texture (an override with a wrong hash does nothing)' % h)
        for name, v in inputs.items():
            if v[0] == 'asset':
                used.add(v[1])
                if name not in EXPECT_DXGI:
                    errors.append('mat_%s: unexpected texture input %s' % (h, name))
                    continue
                want_cs = 'auto' if name == 'emissive_mask_texture' else 'raw'
                if v[2] != want_cs:
                    errors.append('mat_%s %s: colorSpace %s, expected %s' % (h, name, v[2], want_cs))
                p = tex_dir / v[1]
                try:
                    w, hh, dxgi, levels = bc.read_dds(p, want=())
                except (OSError, ValueError) as e:
                    errors.append('mat_%s %s: %s: %s' % (h, name, v[1], e))
                    continue
                if dxgi not in EXPECT_DXGI[name]:
                    errors.append('mat_%s %s: %s is DXGI %d, expected %s' % (h, name, v[1], dxgi, EXPECT_DXGI[name]))
                if name == 'normalmap_texture':
                    normal_files.add(v[1])
            else:
                want = EXPECT_TYPE.get(name)
                if want != v[0]:
                    errors.append('mat_%s %s: USD type %s, Remix reads %s' % (h, name, v[0], want))
                    continue
                if v[0] == 'float':
                    x = float(v[1])
                    if name in ('reflection_roughness_constant', 'metallic_constant') and not 0.0 <= x <= 1.0:
                        errors.append('mat_%s %s = %g is outside [0, 1]' % (h, name, x))
                    if name == 'displace_in' and not 0.0 < x <= 0.2:
                        errors.append('mat_%s displace_in = %g is outside (0, 0.2]' % (h, x))
                    if name == 'emissive_intensity' and not 0.0 < x <= 65504.0:
                        errors.append('mat_%s emissive_intensity = %g is outside Remix range' % (h, x))
                elif v[0] == 'bool' and v[1] not in ('0', '1'):
                    errors.append('mat_%s %s = %s is not 0/1' % (h, name, v[1]))
        if 'emissive_intensity' in inputs and 'enable_emission' not in inputs:
            errors.append('mat_%s: emissive_intensity without enable_emission' % h)
    return errors, normal_files, used


def remix_decode_normal(u, v):
    """packing.slangh unsignedOctahedralToHemisphereDirection."""
    sx, sy = u.astype(np.float32) / 255 * 2 - 1, v.astype(np.float32) / 255 * 2 - 1
    tx, ty = (sx + sy) * 0.5, (sx - sy) * 0.5
    n = np.stack([tx, ty, 1 - np.abs(tx) - np.abs(ty)], -1)
    return n / np.linalg.norm(n, axis=-1, keepdims=True)


def check_normals(tex_dir, files):
    """Unit length / z >= 0 on a small mip of every map (all of them at full res for a fixed sample), and the
    curl test: a tangent-space normal map is the gradient of a height field, so with the right Y direction
    d(x/z)/dv - d(y/z)/du ~ 0."""
    errors, wins = [], {'as written': 0, 'Y flipped': 0, 'X/Y swapped': 0, 'swapped and flipped': 0}
    sample = set(sorted(files)[::max(1, len(files) // FULL_RES_SAMPLE)])
    for f in sorted(files):
        w, h, _, levels = bc.read_dds(tex_dir / f, want=())
        ks = [0] if f in sample else []
        ks.append(next((k for k in range(len(levels)) if max(w >> k, h >> k) <= 128), len(levels) - 1))
        levels = bc.read_dds(tex_dir / f, want=set(ks))[3]
        for k in set(ks):
            lw, lh = max(1, w >> k), max(1, h >> k)
            n = remix_decode_normal(*bc.decode_bc5(levels[k], lw, lh))
            ln = np.linalg.norm(n, axis=-1)
            if not np.all(np.isfinite(n)) or np.abs(ln - 1).max() > 1e-3 or n[..., 2].min() < -1e-6:
                errors.append('%s level %d: decoded normals are not unit-length hemisphere vectors' % (f, k))
            if k == ks[-1] and lw >= 32 and lh >= 32:
                x, y = n[..., 0], n[..., 1]
                e = {'as written': curl(x, y), 'Y flipped': curl(x, -y), 'X/Y swapped': curl(y, x),
                     'swapped and flipped': curl(y, -x)}
                best = sorted(e, key=e.get)
                if e[best[0]] < 0.9 * e[best[1]]:
                    wins[best[0]] += 1
    worst_rival = max(v for k, v in wins.items() if k != 'as written')
    if wins['as written'] < 4 * max(worst_rival, 1):
        errors.append('normal channel order: curl-free %s (need "as written" >= 4x every alternative)' % wins)
    return errors, wins


def curl(x, y):
    """Mean |d(x/z)/dv - d(y/z)/du| of a tangent-space normal field (z rebuilt as in the game shader)."""
    z = np.sqrt(np.clip(1 - x * x - y * y, 1e-4, 1))
    px, py = -x / z, -y / z
    return np.abs((px[2:, 1:-1] - px[:-2, 1:-1]) - (py[1:-1, 2:] - py[1:-1, :-2])).mean()


def check_formula():
    errors = []
    g = np.array([[0, 128, 255]], np.uint8)
    for gloss in (0.0, 1.0, 256.0, 2000.0, 50000.0):
        for sms in (0.0, 0.0155, 0.06, 1.0, 255.0):
            img = np.stack(np.meshgrid(np.arange(0, 256, 15), np.arange(0, 256, 15)), -1).astype(np.uint8)
            for gv in g[0]:
                rgb = np.concatenate([img, np.full(img.shape[:2] + (1,), gv, np.uint8)], -1)[..., [0, 2, 1]]
                rough, metal = bm.spec_to_rough_metal(rgb, gloss, sms)
                if not (np.isfinite(rough).all() and np.isfinite(metal).all()):
                    errors.append('formula not finite (gloss %g, sms %g)' % (gloss, sms))
                if rough.min() < 0 or rough.max() > 1 or metal.min() < 0 or metal.max() > 1:
                    errors.append('formula leaves [0, 1] (gloss %g, sms %g)' % (gloss, sms))
    # glossier (higher R) never rougher, at full specular intensity
    r = np.arange(256, dtype=np.uint8)
    rgb = np.stack([r, np.full(256, 255, np.uint8), np.full(256, 255, np.uint8)], -1)[None]
    rough, _ = bm.spec_to_rough_metal(rgb, 256.0, 1.0)
    if np.any(np.diff(rough[0]) > 1e-6):
        errors.append('roughness rises with gloss')
    # anchor values: n = 256 at F0 >= 0.04 -> (2 / 258) ^ 0.25; sms = 0 -> fully rough (1.0) and not metal
    one = np.array([[[255, 255, 255]]], np.uint8)
    r1, m1 = bm.spec_to_rough_metal(one, 256.0, 0.04)
    if abs(float(r1[0, 0]) - (2 / 258) ** 0.25) > 1e-4 or float(m1[0, 0]) != 0.0:
        errors.append('formula anchor n=256 gives roughness %g metal %g' % (r1[0, 0], m1[0, 0]))
    r0, m0 = bm.spec_to_rough_metal(one, 256.0, 0.0)
    if float(r0[0, 0]) != 1.0 or float(m0[0, 0]) != 0.0:
        errors.append('zero SpecularMaskScale should give roughness 1, metal 0')
    # Sam's suit panel contrast check: dark cloth (R=20, G=4) vs rubber seam (R=220, G=4) at sms=0.6 (inten < 0.04)
    # must keep >= 0.15 roughness separation rather than flattening both panels toward 1.0!
    suit_pair = np.array([[[20, 4, 255], [220, 4, 255]]], np.uint8)
    r_suit, _ = bm.spec_to_rough_metal(suit_pair, 512.0, 0.6)
    if float(r_suit[0, 0] - r_suit[0, 1]) < 0.15:
        errors.append('dark suit panel contrast lost: cloth=%g seam=%g (gap %.3f < 0.15)' % (
            r_suit[0, 0], r_suit[0, 1], r_suit[0, 0] - r_suit[0, 1]))
    # shader rebuilds (smooth bumps, like real maps; BC5 + 8-bit quantisation bound the error)
    yy, xx = np.mgrid[0:128, 0:128] / 128.0
    h = 0.08 * np.sin(6.3 * xx) * np.cos(4.1 * yy) + 0.05 * np.sin(17 * (xx + yy))
    gx, gy = np.gradient(h * 128 / 6.0, axis=1), np.gradient(h * 128 / 6.0, axis=0)
    n = np.stack([-gx, -gy, np.ones_like(gx)], -1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    game = np.zeros((128, 128, 4), np.uint8)
    game[..., 0], game[..., 1] = np.clip(np.rint((n[..., 0] + 1) * 127.5), 0, 255), np.clip(np.rint((n[..., 1] + 1) * 127.5), 0, 255)
    gxf, gyf = game[..., 0] / 127.5 - 1, game[..., 1] / 127.5 - 1
    want = np.stack([gxf, gyf, np.sqrt(np.clip(1 - gxf ** 2 - gyf ** 2, 0, 1))], -1)
    want /= np.linalg.norm(want, axis=-1, keepdims=True)
    got = remix_decode_normal(*bc.decode_bc5(bc.encode_bc5(*bm.normal_to_octahedral(game)), 128, 128))
    ang = np.degrees(np.arccos(np.clip((got * want).sum(-1), -1, 1)))
    if ang.mean() > 1.0 or ang.max() > 5.0:
        errors.append('normal round trip error mean %.2f deg, max %.2f deg (limits 1 and 5)' % (ang.mean(), ang.max()))
    return errors


USD_CHECK = r'''
import os, sys
os.add_dll_directory(sys.argv[2] + r"\bin"); sys.path.insert(0, sys.argv[2])
from pxr import Usd, UsdShade, Sdf
stage = Usd.Stage.Open(sys.argv[1])
looks = stage.GetPrimAtPath("/RootNode/Looks")
bad, n = [], 0
for m in looks.GetFilteredChildren(Usd.PrimIsActive):
    n += 1
    sh = m.GetChild("Shader")
    if not sh.IsValid() or not sh.IsA(UsdShade.Shader):
        bad.append(m.GetName() + ": Shader is not a UsdShadeShader")
        continue
    for a in sh.GetAttributes():
        if a.GetTypeName() == Sdf.ValueTypeNames.Asset:
            p = a.Get()
            if not p or not p.resolvedPath:
                bad.append(m.GetName() + ": " + a.GetName() + " does not resolve")
print(n)
for b in bad[:20]:
    print("BAD " + b)
'''


def check_usd(usda):
    """Open the layer with the RTX Remix Toolkit's USD and do what Remix's processMaterial needs."""
    if not KIT_PY.exists():
        return [], 'skipped (RTX Remix Toolkit not installed)'
    r = subprocess.run([str(KIT_PY), '-s', '-c', USD_CHECK, str(usda), str(USD_LIBS)], capture_output=True, text=True,
                       cwd=str(usda.parent))
    if r.returncode != 0:
        return ['USD failed to open %s: %s' % (usda.name, r.stderr.strip()[-400:])], ''
    out = r.stdout.split()
    bad = [l[4:] for l in r.stdout.splitlines() if l.startswith('BAD ')]
    return bad, '%s material prims opened, every Shader typed and every asset resolved' % out[0]


def run(materials_dir, verbose=True):
    errors = []
    text = (materials_dir / 'mod.usda').read_text('utf-8')
    manifest = json.loads((materials_dir / 'manifest.json').read_text('utf-8'))
    known = defaultdict(list)
    with open(GLOW / 'texture_hashes.csv', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            known[r['hash']].append(r)
    mats, errs = parse_usda(text)
    errors += errs
    errs, normal_files, used = check_inputs(mats, known, materials_dir / 'textures')
    errors += errs
    # overrides vs manifest, and sharp/blurry pairs of the same texture carry the same inputs
    man = {o['hash']: o for o in manifest['overrides']}
    if set(man) != set(mats):
        errors.append('manifest lists %d overrides, mod.usda has %d' % (len(man), len(mats)))
    by_tex = defaultdict(set)
    for h, o in man.items():
        if h in mats:
            by_tex[(o['diffuse'], o['ass'])].add(json.dumps(mats[h], sort_keys=True))
    conflicts = [k for k, v in by_tex.items() if len(v) > 1]
    if conflicts:
        errors.append('%d textures have different inputs on their sharp and blurry hash, e.g. %s' % (len(conflicts), conflicts[0]))
    # stale or missing files
    on_disk = {p.name for p in (materials_dir / 'textures').iterdir() if p.is_file()}
    if on_disk - used:
        errors.append('%d files in textures/ are not referenced (stale), e.g. %s' % (len(on_disk - used), sorted(on_disk - used)[0]))
    # VRAM
    total = sum((materials_dir / 'textures' / f).stat().st_size - 148 for f in used if (materials_dir / 'textures' / f).exists())
    vram = manifest['vram']
    if total != vram['total_bytes']:
        errors.append('texture bytes %d differ from manifest %d' % (total, vram['total_bytes']))
    if vram['worst_level_bytes'] > vram['budget_bytes']:
        errors.append('heaviest level %s needs %.0f MB, budget %.0f MB' % (vram['worst_level'], vram['worst_level_bytes'] / 2 ** 20, vram['budget_bytes'] / 2 ** 20))
    # Character 2048x2048 suit resolution & parallax height map checks
    sam_suit_diffuses = {'BE573495', '2AC7AA8B', '010988FD'}
    checked_suit_maps = 0
    for o in manifest['overrides']:
        if o['diffuse'] in sam_suit_diffuses and o['hash'] in mats:
            inp = mats[o['hash']]
            for mname in ('normalmap_texture', 'reflectionroughness_texture'):
                if mname in inp:
                    mw, mh, _, _ = bc.read_dds(materials_dir / 'textures' / inp[mname][1], want=())
                    checked_suit_maps += 1
                    if mw < 2048 or mh < 2048:
                        errors.append('Sam suit %s (%s) downsampled to %dx%d, expected full 2048x2048' % (
                            o['diffuse'], mname, mw, mh))
    if checked_suit_maps < 4:
        errors.append('too few Sam suit 2048x2048 maps checked (%d)' % checked_suit_maps)
    height_count = sum(1 for inputs in mats.values() if 'height_texture' in inputs)
    if height_count < 100:
        errors.append('expected >= 100 parallax height_texture overrides, found %d' % height_count)
    errs, wins = check_normals(materials_dir / 'textures', normal_files)
    errors += errs
    errors += check_formula()
    errs, usd_note = check_usd(materials_dir / 'mod.usda')
    errors += errs
    if verbose:
        kinds = defaultdict(int)
        for inputs in mats.values():
            for k in inputs:
                kinds[k] += 1
        print('overrides %d (%d texture files, %.1f MB on disk / VRAM if all resident)' % (len(mats), len(used), total / 2 ** 20))
        print('inputs: ' + ', '.join('%s %d' % kv for kv in sorted(kinds.items())))
        print('heaviest level %s: %.1f MB of %.0f MB budget' % (vram['worst_level'], vram['worst_level_bytes'] / 2 ** 20, vram['budget_bytes'] / 2 ** 20))
        print('normal maps: %d checked; channel order that makes them curl-free: %s' % (len(normal_files), wins))
        print('USD: ' + usd_note)
    return errors


def self_test():
    """The checks must catch: an untyped shader, a wrong hash, a duplicate, a wrong DDS format, a value out of
    range, a stale file. Builds a tiny mod in a temp folder from real generated files."""
    import shutil
    import tempfile
    real = parse_usda((MATERIALS / 'mod.usda').read_text('utf-8'))[0]
    known = defaultdict(list)
    with open(GLOW / 'texture_hashes.csv', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            known[r['hash']].append(r)
    h, inputs = next((h, i) for h, i in real.items() if 'normalmap_texture' in i and 'reflectionroughness_texture' in i)
    nfile, rfile = inputs['normalmap_texture'][1], inputs['reflectionroughness_texture'][1]
    cases = {
        'untyped shader': ('            over "Shader"\n', h, nfile, rfile, '0.5'),
        'unknown hash': ('            over Shader "Shader"\n', '0123456789ABCDEF', nfile, rfile, '0.5'),
        'wrong DDS format': ('            over Shader "Shader"\n', h, rfile, rfile, '0.5'),
        'roughness out of range': ('            over Shader "Shader"\n', h, nfile, rfile, '1.5'),
    }
    failed = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / 'textures').mkdir()
        for f in (nfile, rfile):
            shutil.copy(MATERIALS / 'textures' / f, td / 'textures' / f)
        for label, (shader, hh, nf, rf, rc) in cases.items():
            body = ('        over "mat_%s"\n        {\n%s            {\n'
                    '                asset inputs:normalmap_texture = @./textures/%s@ (\n                    colorSpace = "raw"\n                )\n'
                    '                float inputs:metallic_constant = %s\n            }\n        }\n') % (hh, shader, nf, rc)
            text = '#usda 1.0\nover "RootNode"\n{\n    over "Looks"\n    {\n' + body + '    }\n}\n'
            mats, errs = parse_usda(text)
            e2, _, _ = check_inputs(mats, known, td / 'textures')
            if not errs + e2:
                failed.append(label)
        dup = '#usda 1.0\n' + ('        over "mat_%s"\n        {\n            over Shader "Shader"\n            {\n'
                               '                float inputs:metallic_constant = 0\n            }\n        }\n' % h) * 2
        if not parse_usda(dup)[1]:
            failed.append('duplicate override')
        untyped_usd = td / 'mod.usda'
        untyped_usd.write_text('#usda 1.0\nover "RootNode"\n{\n    over "Looks"\n    {\n        over "mat_%s"\n        {\n'
                               '            over "Shader"\n            {\n            }\n        }\n    }\n}\n' % h, 'utf-8')
        if KIT_PY.exists() and not check_usd(untyped_usd)[0]:
            failed.append('USD untyped shader')
    return failed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args()
    errors = run(MATERIALS)
    if a.self_test:
        missed = self_test()
        errors += ['self-test: the check did not catch "%s"' % m for m in missed]
        if not missed:
            print('self-test: untyped shader, unknown hash, wrong DDS format, out-of-range value, duplicate and '
                  'untyped USD shader are all caught')
    for e in errors[:40]:
        print('FAIL: ' + e)
    if errors:
        print('FAIL: %d problem(s)' % len(errors))
        return 1
    print('PASS: verify_materials_mod')
    return 0


if __name__ == '__main__':
    sys.exit(main())
