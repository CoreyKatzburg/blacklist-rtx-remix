"""Offline check that the materials pipeline reproduces the texture hashes Remix computes in the running game.

Evidence used (no game run needed):
  1. Algorithm: Remix 1.5.2 names a texture XXH3_64(mip-0 upload buffer) (d3d9_common_texture.cpp,
     SetupForRtxFrom). For every live capture file that survives on disk and is block compressed, XXH3 of its
     mip 0 must equal its file name: Remix's own output confirms the algorithm independently of our archive reader.
  2. Reproduction: every live hash in play-setup/glow/capture_hashes.csv is looked up in texture_hashes.csv (the
     resident and streamed top-mip hashes the pipeline computes for all 77,435 archive textures).
  3. Explanation of the rest: capture_audit.csv (written by the slow scripts/audit_captures.py) gives each
     remaining hash a category with evidence; the cheap evidence is recomputed here (render-target counters,
     all-zero uploads). No live hash may be 'no_evidence' or a game-archive texture we failed to reproduce.

Threshold: 100% of live hashes that are game archive textures must be reproduced (they are the only textures a
material override can target, and a wrong hash silently does nothing). The other live hashes are render
targets, UI art from Scaleform packages, run-dependent dynamic textures, or not Remix names at all; for those
the check requires a verified explanation instead (5 hashes whose files were deleted before this audit are
listed by name as unverifiable; any new unexplained hash fails). Overall reproduction must stay >= 80% of all live hashes
(it is 80.4% now: 308 of 383) so a drop is noticed even if the audit file were regenerated carelessly.
Run: python tests/verify_materials_hashes.py [--self-test]
"""
import csv
import os
import struct
import subprocess
import sys
from collections import Counter
from pathlib import Path

REMIX = Path(__file__).resolve().parents[1]
GLOW = REMIX / 'play-setup' / 'glow'
VENV_PY = GLOW / '.venv' / 'Scripts' / 'python.exe'
try:
    import xxhash
except ImportError:  # xxhash is installed in the pipeline's venv
    if VENV_PY.exists() and Path(sys.executable).resolve() != VENV_PY.resolve():
        raise SystemExit(subprocess.call([str(VENV_PY), __file__, *sys.argv[1:]]))
    raise

MIN_OVERALL = 0.80
MIN_ALGORITHM_FILES = 100
# Live hashes whose capture file was deleted before the audit existed and that no rule reproduces. They are all
# from dx11-lighting/scratch-trackA (a DX11 project folder, so most likely not Remix names at all); new ones fail.
UNVERIFIABLE = {'0EF12DC5B388A24D', '25B06A5EFFF78060', '56C5AA9228B329FA', '7CC6DD54DC35A591', 'D762D0D4B88173B2'}
EXPLAINED = {'archive_texture', 'render_target', 'never_written', 'ui_dynamicflash', 'dx11_addon_name',
             'not_stock_data', 'dynamic_unreproducible'}
FOURCC_BLOCK = {b'DXT1': 8, b'DXT5': 16}


def xx(b):
    return '%016X' % xxhash.xxh3_64_intdigest(b)


def main():
    known = {}
    with open(GLOW / 'texture_hashes.csv', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            known.setdefault(r['hash'], r)
    if '--self-test' in sys.argv:  # a pipeline that forgot the streamed (sharp) hashes must fail
        resident_only = {h: r for h, r in known.items() if r['role'] != 'stream_top'}
        if evaluate(resident_only, quiet=True) == 0:
            print('FAIL: self-test: dropping the streamed top-mip hashes was not caught')
            return 1
        print('self-test: dropping the streamed top-mip hashes is caught')
    return evaluate(known)


def evaluate(known, quiet=False):
    errors = []
    caps = [l.split(',', 1) for l in (GLOW / 'capture_hashes.csv').read_text().splitlines() if l.strip()]
    caps = [(h.strip().upper(), p.strip()) for h, p in caps]
    with open(GLOW / 'capture_audit.csv', encoding='utf-8') as f:
        audit = {r['hash']: r for r in csv.DictReader(f)}

    # 1. algorithm, from Remix's own capture files
    checked = mismatched = 0
    for h, _ in caps:
        p = audit.get(h, {}).get('file') or next((q for hh, q in caps if hh == h), '')
        if not p or not os.path.exists(p):
            continue
        b = open(p, 'rb').read()
        hh_, w = struct.unpack_from('<II', b, 12)
        bpb = FOURCC_BLOCK.get(b[84:88])
        if not bpb:
            continue
        checked += 1
        if xx(b[128:128 + max(1, (w + 3) // 4) * max(1, (hh_ + 3) // 4) * bpb]) != h:
            mismatched += 1
            if audit.get(h, {}).get('category') not in ('dx11_addon_name',):
                errors.append('%s: XXH3 of its mip 0 is not its name (%s)' % (h, p))
    if checked < MIN_ALGORITHM_FILES:
        errors.append('only %d capture files could confirm the hash algorithm (need %d)' % (checked, MIN_ALGORITHM_FILES))

    # 2. reproduction
    reproduced = [h for h, _ in caps if h in known]
    roles = Counter(known[h]['role'] for h in reproduced)

    # 3. explanations
    cats = Counter()
    for h, _ in caps:
        a = audit.get(h)
        if a is None:
            errors.append('%s: not in capture_audit.csv (run scripts/audit_captures.py)' % h)
            continue
        c = a['category']
        cats[c] += 1
        if c not in EXPLAINED and not (c == 'no_evidence' and h in UNVERIFIABLE):
            errors.append('%s: unexplained live hash (%s: %s)' % (h, c, a['evidence']))
        if c == 'archive_texture' and h not in known:
            errors.append('%s: audit says archive texture but the pipeline does not reproduce it' % h)
        if c != 'archive_texture' and h in known:
            errors.append('%s: reproduced by the pipeline but audited as %s' % (h, c))
        if c == 'render_target':  # '<w>x<h> <fmt>, counter <n>'
            dims, counter = a['evidence'].split(' ')[0], int(a['evidence'].rsplit(' ', 1)[1])
            w, hh_ = map(int, dims.split('x'))
            seed = xxhash.xxh3_64_intdigest(struct.pack('<III', w, hh_, 1))
            if '%016X' % xxhash.xxh3_64_intdigest(struct.pack('<I', counter), seed) != h:
                errors.append('%s: render-target evidence does not reproduce the hash' % h)
        if c == 'never_written':  # 'all-zero <w>x<h> upload, <bpp> byte(s) per texel'
            parts = a['evidence'].split(' ')
            w, hh_ = map(int, parts[1].split('x'))
            bpp = int(parts[3])
            if xx(bytes(((w * bpp + 3) & ~3) * hh_)) != h:
                errors.append('%s: all-zero evidence does not reproduce the hash' % h)

    rate = len(reproduced) / len(caps)
    archive = cats['archive_texture']
    if quiet:
        return 1 if errors or rate < MIN_OVERALL else 0
    print('algorithm: %d live capture files hash to their names (%d are DX11 add-on files, not Remix names)' % (checked - mismatched, mismatched))
    print('reproduced offline: %d of %d live hashes (%.1f%%): %s' % (len(reproduced), len(caps), 100 * rate,
                                                                    ', '.join('%s %d' % kv for kv in sorted(roles.items()))))
    print('game archive textures reproduced: %d of %d (100%% required)' % (len(reproduced), archive))
    print('not reproducible offline, explained: ' + ', '.join('%s %d' % kv for kv in sorted(cats.items()) if kv[0] not in ('archive_texture', 'no_evidence')))
    print('unverifiable (capture file deleted, no rule reproduces it): %d: %s' % (cats['no_evidence'], ' '.join(sorted(h for h, _ in caps if audit.get(h, {}).get('category') == 'no_evidence'))))
    if rate < MIN_OVERALL:
        errors.append('overall reproduction %.1f%% is under %.0f%%' % (100 * rate, 100 * MIN_OVERALL))
    for e in errors[:40]:
        print('FAIL: ' + e)
    if errors:
        print('FAIL: %d problem(s)' % len(errors))
        return 1
    print('PASS: verify_materials_hashes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
