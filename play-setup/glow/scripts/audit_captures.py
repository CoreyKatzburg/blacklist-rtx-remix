"""Explain every hash in capture_hashes.csv (live Remix captures) with machine-checked evidence.

Slow (reads ~11 GB of archives, walks D:\\BlacklistOverhaul for capture copies). Run after build_materials.py:
    .venv\\Scripts\\python.exe scripts\\audit_captures.py
Writes play-setup/glow/capture_audit.csv: hash, category, evidence, file. tests/verify_materials_hashes.py
re-checks the cheap parts of that evidence on every run.

Categories (how Remix 1.5.2 named the texture, d3d9_common_texture.cpp):
  archive_texture      XXH3_64 of a mip-0 upload of a texture in the .ass/.asstrm archives (texture_hashes.csv)
  render_target        XXH3_64_withSeed(counter, seed = XXH3_64(width, height, 1)): counter found by search
  never_written        XXH3_64 of an all-zero upload: Remix hashed a CPU-written texture before the game wrote it
  ui_dynamicflash      mip 0 found byte-for-byte in a Scaleform UI package (*.umd outside the level archives)
  dx11_addon_name      not a Remix name: the DX11 lighting add-on's XXH64 of the same game mip (addon/textures.inl)
  not_stock_data       a capture file exists, its content hashes to its name, but no mip of it is in any archive
  dynamic_unreproducible  8-bit texture whose content is not in any archive and whose hash is not its content's
                       (a texture the game writes over time, e.g. a font glyph cache): cannot be computed offline
  no_evidence          no copy of the capture file survives and no rule above reproduces the hash
"""
import csv
import glob
import json
import os
import re
import struct
import sys
from pathlib import Path

import xxhash

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import lead  # noqa: E402

GLOW = SCRIPTS.parent
SYSTEM = Path(r"D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM")
SEARCH_ROOT = Path(r'D:\BlacklistOverhaul')
RT_MAX_COUNTER = 100000
# DXGI formats the DX11 build can use for the archive FORMAT codes (unorm and srgb variants)
DX11_FORMATS = {1: (87, 91, 28, 29), 4: (71, 72), 5: (74, 75), 6: (77, 78), 10: (61, 65), 16: (83,)}
FOURCC_BLOCK = {b'DXT1': 8, b'DXT3': 16, b'DXT5': 16, b'ATI2': 16, b'BC5U': 16}
DX10_BPP = {28: 4, 61: 1, 87: 4, 10: 8}


def xx(b):
    return '%016X' % xxhash.xxh3_64_intdigest(b)


def rt_hash(w, h, counter):
    return '%016X' % xxhash.xxh3_64_intdigest(struct.pack('<I', counter), xxhash.xxh3_64_intdigest(struct.pack('<III', w, h, 1)))


def dds_info(path):
    """-> (w, h, mip0 bytes or None if the format is not one Remix content-hashes 1:1, format label)."""
    b = open(path, 'rb').read()
    h, w = struct.unpack_from('<II', b, 12)
    fcc = b[84:88]
    if fcc == b'DX10':
        dx = struct.unpack_from('<I', b, 128)[0]
        bpp = DX10_BPP.get(dx)
        return w, h, (b[148:148 + w * h * bpp] if bpp else None), 'DXGI_%d' % dx
    if fcc in FOURCC_BLOCK:
        return w, h, b[128:128 + max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * FOURCC_BLOCK[fcc]], fcc.decode()
    return w, h, None, 'fourcc_%d' % struct.unpack('<I', fcc)[0]


def find_copies(hashes):
    want, found = set(hashes), {}
    for root, dirs, files in os.walk(SEARCH_ROOT):
        if 'tools\\remix\\materials' in root or '\\.git' in root:
            continue
        for f in files:
            stem = f[:-4].upper()
            if f.lower().endswith('.dds') and stem in want:
                found.setdefault(stem, []).append(os.path.join(root, f))
    return found


def ui_packages():
    for p in sorted(glob.glob(str(SYSTEM / '*.umd')) + glob.glob(str(SYSTEM / 'UMDs' / '*.umd'))):
        try:
            a = lead.LeadArchive(p)
        except ValueError:
            continue
        data = bytearray()
        for i in range(len(a.pages)):
            data += a._page(i)
        a.close()
        yield os.path.basename(p), bytes(data)


def dx11_addon_hashes(targets, rows):
    """XXH64 the DX11 add-on way (seed = dxgi << 48 ^ w << 24 ^ h) over every archive level >= 64 px."""
    rd, found = lead.LevelReader(SYSTEM / 'UMDs'), {}
    try:
        for rec in rows:
            for k, lv in enumerate(rec['levels']):
                if max(lv['w'], lv['h']) < 64:
                    continue
                data = rd.level(rec, k)
                forms = [('', data)]
                if rec['fmt'] == 16:  # the DX11 build stores ATI2 as BC5, i.e. with the two block halves swapped
                    b = [data[i:i + 16] for i in range(0, len(data), 16)]
                    forms.append((', ATI2 halves swapped to BC5', b''.join(x[8:] + x[:8] for x in b)))
                for note, dd in forms:
                    for f in DX11_FORMATS.get(rec['fmt'], ()):
                        hv = '%016X' % xxhash.xxh64_intdigest(dd, (f << 48) ^ (lv['w'] << 24) ^ lv['h'])
                        if hv in targets:
                            found[hv] = '%s level %dx%d in %s, DXGI %d%s' % (rec['name'], lv['w'], lv['h'], rec['ass'], f, note)
    finally:
        rd.close()
    return found


def main():
    caps = [l.split(',', 1) for l in (GLOW / 'capture_hashes.csv').read_text().splitlines() if l.strip()]
    known = {}
    with open(GLOW / 'texture_hashes.csv', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            known.setdefault(r['hash'], r)
    out = {}
    for h, p in caps:
        h = h.upper()
        if h in known:
            r = known[h]
            out[h] = ('archive_texture', '%s %s %sx%s format %s' % (r['texture_name'], r['role'], r['width'], r['height'], r['format']), '')
    rest = [h.upper() for h, _ in caps if h.upper() not in out]
    copies = find_copies(rest)
    for h, p in caps:
        if os.path.exists(p) and h.upper() in rest:
            copies.setdefault(h.upper(), []).insert(0, p)
    print('to explain', len(rest), 'with a surviving file', len(copies), flush=True)

    rows = json.loads((GLOW / 'cache' / 'textures.json').read_text('utf-8'))
    level_hash = {lv['hash'] for r in rows for lv in r['levels']}
    needles = {}
    for h in rest:
        for path in copies.get(h, []):
            w, hh, mip0, fmt = dds_info(path)
            if mip0 is not None and xx(mip0) == h:
                needles[h] = (path, w, hh, fmt, mip0)
                break
        if h in needles:
            continue
        path = (copies.get(h) or [''])[0]
        if path:
            w, hh, mip0, fmt = dds_info(path)
            c = next((c for c in range(RT_MAX_COUNTER) if rt_hash(w, hh, c) == h), None)
            if c is not None:
                out[h] = ('render_target', '%dx%d %s, counter %d' % (w, hh, fmt, c), path)
                continue
            for bpp in (1, 2, 4, 8):
                if xx(bytes(((w * bpp + 3) & ~3) * hh)) == h:
                    out[h] = ('never_written', 'all-zero %dx%d upload, %d byte(s) per texel' % (w, hh, bpp), path)
                    break
            if h in out:
                continue
            if fmt in ('DXGI_61',):
                out[h] = ('dynamic_unreproducible', '%dx%d 8-bit, not a render target, content not in archives' % (w, hh), path)
            continue
    # content-hashed captures: Scaleform UI packages or not stock
    if needles:
        for pkg, data in ui_packages():
            for h, (path, w, hh, fmt, mip0) in list(needles.items()):
                i = data.find(mip0)
                if i >= 0 and xx(data[i:i + len(mip0)]) == h:
                    out[h] = ('ui_dynamicflash', '%s %dx%d at byte %d of %s' % (fmt, w, hh, i, pkg), path)
                    del needles[h]
        for h, (path, w, hh, fmt, mip0) in needles.items():
            b = open(path, 'rb').read()
            n, off, hits = struct.unpack_from('<I', b, 28)[0], 128, []
            for k in range(max(1, n)):
                lw, lh = max(1, w >> k), max(1, hh >> k)
                sz = max(1, (lw + 3) // 4) * max(1, (lh + 3) // 4) * FOURCC_BLOCK.get(fmt.encode(), 8)
                hits.append(xx(b[off:off + sz]) in level_hash)
                off += sz
            if not any(hits):
                out[h] = ('not_stock_data', '%s %dx%d, %d mips, none of them in any archive' % (fmt, w, hh, len(hits)), path)
    # whatever is left: try the DX11 add-on's naming
    left = {h for h, _ in caps if h.upper() not in out}
    if left:
        print('checking %d hashes against the DX11 add-on naming (slow)' % len(left), flush=True)
        for h, ev in dx11_addon_hashes(left, rows).items():
            out[h] = ('dx11_addon_name', ev, (copies.get(h) or [''])[0])
    for h, p in caps:
        out.setdefault(h.upper(), ('no_evidence', 'no surviving copy of %s and no rule reproduces it' % p, ''))
    with open(GLOW / 'capture_audit.csv', 'w', newline='\n', encoding='utf-8') as f:
        f.write('hash,category,evidence,file\n')
        for h, _ in caps:
            c, e, p = out[h.upper()]
            f.write('%s,%s,"%s","%s"\n' % (h.upper(), c, e.replace('"', "'"), p))
    from collections import Counter
    print(Counter(v[0] for v in out.values()))


if __name__ == '__main__':
    main()
