"""Offline check that every mod texture has mip-maps.

Fails (exit 1) when a DDS in materials/textures has one level but a longer side above 4 px. Remix would sample that
texture at full size at every distance, which shimmers. A texture of 4 px or less needs no chain.

The chain length is not judged. The game's own chains end at different sizes (most at 4 px, some at 256 px), so a
chain of two or more levels passes. This needs no game and reads only the 148-byte headers.
Run: python tests/verify_materials_mips.py [--self-test]   (--self-test also proves the check catches a file with no
mip-maps and passes the files it should)
"""
import argparse
import sys
import tempfile
from pathlib import Path

REMIX = Path(__file__).resolve().parents[1]
TEXTURES = REMIX / 'materials' / 'textures'
sys.path.insert(0, str(REMIX / 'play-setup' / 'glow' / 'scripts'))
import bc  # noqa: E402

MIN_SIDE = 4


def lacking_mips(path):
    """(w, h) when the file has one level but a longer side above MIN_SIDE, else None."""
    w, h, _, levels = bc.read_dds(path, want=())
    return (w, h) if len(levels) == 1 and max(w, h) > MIN_SIDE else None


def run(folder):
    files = sorted(folder.glob('*.dds'))
    errors = []
    if not files:
        errors.append('no DDS files in %s' % folder)
    for p in files:
        size = lacking_mips(p)
        if size:
            errors.append('%s is %dx%d with one mip level (no mip-maps)' % (p.name, size[0], size[1]))
    return len(files), errors


def self_test():
    """The check must fail a one-level 256 px file and pass a two-level one and a one-level 4 px file."""
    failed = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        def make(name, w, h, sizes):
            levels = [bytes(bc.level_size(max(1, w >> k), max(1, h >> k), 83)) for k in range(sizes)]
            bc.write_dds(td / name, w, h, 83, levels)

        make('one_level_256.dds', 256, 256, 1)
        make('two_levels_256.dds', 256, 256, 2)
        make('one_level_4.dds', 4, 4, 1)
        for name, want_fail in (('one_level_256.dds', True), ('two_levels_256.dds', False),
                                ('one_level_4.dds', False)):
            failed_now = lacking_mips(td / name) is not None
            if failed_now != want_fail:
                failed.append('self-test: %s gave %s, expected %s' % (
                    name, 'a failure' if failed_now else 'a pass', 'a failure' if want_fail else 'a pass'))
    return failed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args()
    count, errors = run(TEXTURES)
    if a.self_test:
        st = self_test()
        errors += st
        if not st:
            print('self-test: a one-level 256 px file is caught; two levels and a one-level 4 px file pass')
    for e in errors[:40]:
        print('FAIL: ' + e)
    print('textures checked: %d, without mip-maps: %d' % (count, sum(1 for e in errors if 'no mip-maps' in e)))
    if errors:
        print('FAIL: %d problem(s)' % len(errors))
        return 1
    print('PASS: verify_materials_mips')
    return 0


if __name__ == '__main__':
    sys.exit(main())
