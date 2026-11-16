"""Add the missing mip levels to mod textures that were written with one level.

Some game textures are stored with a single level in the archive, so the mod file has one level too. Remix then has
no smaller copies to sample at a distance, and the surface shimmers. For each DDS in materials/textures that has one
level and a longer side above 4 px, this keeps mip 0 byte for byte and appends the lower levels down to a 4 px longer
side (the end point of most of the game's own chains):

  BC5 normal maps     decode the unit vectors, average 2x2 in 3D, renormalise, re-encode octahedral. Box-averaging the
                      octahedral channels would not stay unit length.
  BC4 roughness,      box-average the 8-bit values and re-encode.
  metallic, height
  BC1/BC3 emissive    DirectXTex (tools\\texconv\\texconv.exe) makes levels 1.. from mip 0 with sRGB-aware filtering.

Dry run by default. --write rewrites the files; each is read back and validated before it replaces the original. The
originals of the 200 single-level files (2026-11-15) are in experiments\\mod-texture-backup-20261115 (SHA256SUMS.txt).
--manifest then recomputes the VRAM numbers in materials\\manifest.json the way build_materials.py does (it needs
xxhash, so run it with the glow venv).

Usage:  python add_texture_mips.py [--write | --manifest]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import bc  # noqa: E402

REMIX = SCRIPTS.parents[2]
TEXTURES = REMIX / 'materials' / 'textures'
TEXCONV = REMIX.parent / 'texconv' / 'texconv.exe'
MIN_SIDE = 4
TEXCONV_FORMAT = {72: 'BC1_UNORM_SRGB', 78: 'BC3_UNORM_SRGB'}  # emissive masks (sRGB)


def level_dims(w, h):
    """(w, h) of every level, largest first, down to a 4 px longer side."""
    dims = [(w, h)]
    while max(dims[-1]) > MIN_SIDE:
        dims.append((max(1, dims[-1][0] >> 1), max(1, dims[-1][1] >> 1)))
    return dims


def box2(a):
    """2x2 box average of an (h, w[, c]) array; both sides must be powers of two (edge-padded below 2 px)."""
    h, w = a.shape[:2]
    th, tw = max(1, h // 2), max(1, w // 2)
    pad = [(0, 2 * th - h), (0, 2 * tw - w)] + [(0, 0)] * (a.ndim - 2)
    p = np.pad(a, pad, mode='edge')
    return p.reshape(th, 2, tw, 2, *a.shape[2:]).mean(axis=(1, 3))


def octa_to_vec(u, v):
    """Remix's unsigned hemi-octahedral decode (packing.slangh), as verify_materials_mod.remix_decode_normal."""
    sx, sy = u.astype(np.float32) / 255 * 2 - 1, v.astype(np.float32) / 255 * 2 - 1
    tx, ty = (sx + sy) * 0.5, (sx - sy) * 0.5
    n = np.stack([tx, ty, 1 - np.abs(tx) - np.abs(ty)], -1)
    return n / np.linalg.norm(n, axis=-1, keepdims=True)


def vec_to_octa(n):
    """Exact inverse of octa_to_vec for unit vectors with z >= 0."""
    n = n / np.abs(n).sum(-1, keepdims=True)
    sx, sy = n[..., 0] + n[..., 1], n[..., 0] - n[..., 1]
    u = np.clip(np.rint((sx + 1) * 127.5), 0, 255).astype(np.uint8)
    v = np.clip(np.rint((sy + 1) * 127.5), 0, 255).astype(np.uint8)
    return u, v


def normal_levels(mip0, w, h, n):
    u, v = bc.decode_bc5(mip0, w, h)
    out = []
    for _ in range(1, n):
        vec = octa_to_vec(u, v)
        avg = box2(vec)
        length = np.linalg.norm(avg, axis=-1, keepdims=True)
        # four normals that cancel average to zero; fall back to straight up (the flat surface normal)
        avg = np.where(length > 1e-6, avg / np.maximum(length, 1e-6), np.array([0, 0, 1], np.float32))
        u, v = vec_to_octa(avg)
        out.append(bc.encode_bc5(u, v))
    return out


def scalar_levels(mip0, w, h, n):
    a = bc.decode_bc4(mip0, w, h)
    out = []
    for _ in range(1, n):
        a = np.rint(box2(a.astype(np.float32))).astype(np.uint8)
        out.append(bc.encode_bc4(a))
    return out


def texconv_levels(src, w, h, dxgi, n):
    """Levels 1.. of an emissive BC1/BC3 sRGB mask, made by DirectXTex from the same mip 0."""
    tmp = Path(tempfile.mkdtemp(prefix='mips_'))
    try:
        cmd = [str(TEXCONV), '-nologo', '-y', '-dx10', '-m', str(n), '-f', TEXCONV_FORMAT[dxgi], '-srgbi', '-srgbo',
               '-o', str(tmp), str(src)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        out = tmp / src.name
        if r.returncode != 0 or not out.exists():
            raise RuntimeError('texconv failed (%d): %s' % (r.returncode, (r.stdout + r.stderr).strip()[-300:]))
        ow, oh, odxgi, levels = bc.read_dds(out)
        if (ow, oh, odxgi, len(levels)) != (w, h, dxgi, n):
            raise RuntimeError('texconv output is %dx%d fmt %d with %d levels, expected %dx%d fmt %d with %d'
                               % (ow, oh, odxgi, len(levels), w, h, dxgi, n))
        return levels[1:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def header_levels(path):
    """(w, h, dxgi, level count) from the header only, after the same size validation read_dds does."""
    w, h, dxgi, levels = bc.read_dds(path, want=())
    return w, h, dxgi, len(levels)


def add_mips(path):
    w, h, dxgi, n_now = header_levels(path)
    dims = level_dims(w, h)
    if n_now != 1 or len(dims) == 1:
        raise ValueError('not a single-level texture above %d px' % MIN_SIDE)
    if (w & (w - 1)) or (h & (h - 1)):
        raise ValueError('side is not a power of two (%dx%d)' % (w, h))
    _, _, _, (mip0,) = bc.read_dds(path, want={0})
    if dxgi == 83:
        extra = normal_levels(mip0, w, h, len(dims))
    elif dxgi == 80:
        extra = scalar_levels(mip0, w, h, len(dims))
    elif dxgi in TEXCONV_FORMAT:
        extra = texconv_levels(path, w, h, dxgi, len(dims))
    else:
        raise ValueError('no mip generator for DXGI format %d' % dxgi)
    levels = [mip0] + extra
    tmp = path.with_name(path.name + '.tmp')
    bc.write_dds(tmp, w, h, dxgi, levels)
    tw, th, tdxgi, back = bc.read_dds(tmp)
    if (tw, th, tdxgi) != (w, h, dxgi) or len(back) != len(dims) or back[0] != mip0 or \
            [len(b) for b in back] != [bc.level_size(lw, lh, dxgi) for lw, lh in dims]:
        tmp.unlink()
        raise ValueError('written file does not read back as the expected chain')
    os.replace(tmp, path)
    return len(dims)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--write', action='store_true', help='rewrite the single-level textures')
    g.add_argument('--manifest', action='store_true', help='recompute the VRAM numbers in manifest.json')
    a = ap.parse_args()
    if a.manifest:
        return update_manifest()
    targets = []
    for p in sorted(TEXTURES.glob('*.dds')):
        w, h, dxgi, n = header_levels(p)
        if n == 1 and len(level_dims(w, h)) > 1:
            targets.append(p)
    print('single-level textures above %d px: %d' % (MIN_SIDE, len(targets)))
    if not a.write:
        print('dry run: nothing written (use --write)')
        return 0
    failed = []
    for p in targets:
        try:
            add_mips(p)
        except Exception as e:  # keep going, report at the end
            failed.append('%s: %s' % (p.name, e))
    print('rewritten: %d, failed: %d' % (len(targets) - len(failed), len(failed)))
    for f in failed[:20]:
        print('FAIL: ' + f)
    return 1 if failed else 0


def write_manifest(path, manifest):
    """Same layout as build_materials.py: one override per line."""
    nl = '\n'
    parts = ['%s: %s' % (json.dumps(k), json.dumps(v, indent=1)) for k, v in manifest.items() if k != 'overrides']
    parts.append('"overrides": [' + nl + (',' + nl).join(json.dumps(o, sort_keys=True) for o in manifest['overrides'])
                 + nl + ']')
    path.write_text('{' + nl + (',' + nl).join(parts) + nl + '}' + nl, 'utf-8', newline=nl)


def update_manifest():
    """VRAM numbers as build_materials.py computes them (files_by_group, ALWAYS_LOADED), from the current files."""
    import re
    import build_materials as bm  # noqa: E402  (needs xxhash, which the glow venv has)
    glow = bm.GLOW
    rows = json.loads((glow / 'cache' / 'textures.json').read_text('utf-8'))
    cats = json.loads((glow / 'cache' / 'catalogs.json').read_text('utf-8'))
    mats = bm.load_materials(cats)
    canon, variants = bm.index_textures(rows)
    groups_of = defaultdict(set)
    for _, (name, cls, d, umds) in mats.items():
        s = bm.setup_of(cls, d)
        if s is None or s['diffuse'] not in variants:
            continue
        if max(canon[s['diffuse']]['levels'][0]['w'], canon[s['diffuse']]['levels'][0]['h']) < bm.MIN_DIFFUSE_SIDE:
            continue
        for rec in variants[s['diffuse']]:
            for kh, _ in bm.remix_keys(rec):
                groups_of[kh].update(umds)
    mod_usda = (REMIX / 'materials' / 'mod.usda').read_text('utf-8')
    manifest_path = REMIX / 'materials' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text('utf-8'))
    files_of = {}
    for block in re.split(r'over "mat_', mod_usda)[1:]:
        files_of.setdefault(block[:16], set()).update(re.findall(r'@\./textures/([^@]+)@', block))
    size = {f: (TEXTURES / f).stat().st_size - 148 for fs in files_of.values() for f in fs}
    files_by_group = defaultdict(set)
    for h, fs in files_of.items():
        for gname in groups_of.get(h, ()):
            files_by_group[gname] |= fs
    common = set().union(*(files_by_group.get(gname, set()) for gname in bm.ALWAYS_LOADED))
    per_level = {gname: sum(size[f] for f in fs | common) for gname, fs in files_by_group.items()}
    worst = max(per_level.items(), key=lambda kv: kv[1])
    vram = manifest['vram']
    vram.update({'total_bytes': sum(size.values()), 'worst_level': worst[0], 'worst_level_bytes': worst[1],
                 'always_loaded_bytes': sum(size[f] for f in common),
                 'per_level_bytes': dict(sorted(per_level.items()))})
    write_manifest(manifest_path, manifest)
    print('total %d, always loaded %d, worst %s %d of budget %d' % (
        vram['total_bytes'], vram['always_loaded_bytes'], worst[0], worst[1], vram['budget_bytes']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
