"""Offline check that every mod texture has a full mip-map chain.

Fails (exit 1) when a DDS in materials/textures has fewer mip levels than its size and format require:
- block-compressed DXGI formats (BC1..BC7): down to 4 px on the short side (min(w, h) <= 4)
- uncompressed formats: down to 1 px on the short side (min(w, h) <= 1)

Any intended short chain must be listed by filename in ALLOW_LIST with the reason; the rule itself is never loosened.
This needs no game and reads only the 148-byte headers.
Run: python tests/verify_materials_mips.py [--self-test]
"""
import argparse
import struct
import sys
import tempfile
from pathlib import Path

REMIX = Path(__file__).resolve().parents[1]
TEXTURES = REMIX / 'materials' / 'textures'
sys.path.insert(0, str(REMIX / 'play-setup' / 'glow' / 'scripts'))
import bc  # noqa: E402

BC_DXGI_FORMATS = frozenset({71, 72, 74, 75, 77, 78, 80, 81, 83, 84, 95, 96, 98, 99})

# filename -> reason why a shorter mip chain is intended (empty: all mod textures now have full chains)
ALLOW_LIST: dict[str, str] = {}


def expected_levels(w: int, h: int, dxgi: int) -> int:
    """Expected mip-level count: down to 4 px on the short side for block-compressed formats, 1 px otherwise."""
    limit = 4 if dxgi in BC_DXGI_FORMATS else 1
    n = 1
    while min(w, h) > limit:
        w = max(1, w // 2)
        h = max(1, h // 2)
        n += 1
    return n


def read_header(path: Path) -> tuple[int, int, int, int]:
    """Return (w, h, dxgi, mip_count) from a DDS header (-1 for legacy non-DX10 pixel formats)."""
    with open(path, 'rb') as f:
        b = f.read(148)
    if len(b) < 128 or b[:4] != b'DDS ' or struct.unpack_from('<I', b, 4)[0] != 124:
        raise ValueError(f'not a DDS file: {path}')
    h, w, _, _, n = struct.unpack_from('<5I', b, 12)
    dxgi = struct.unpack_from('<I', b, 128)[0] if len(b) >= 148 and b[84:88] == b'DX10' else -1
    return w, h, dxgi, max(1, n)


def short_chain(path: Path) -> tuple[int, int, int, int, int] | None:
    """(w, h, dxgi, have, want) when the file has fewer mip levels than expected and is not allow-listed, else None."""
    if path.name in ALLOW_LIST:
        return None
    w, h, dxgi, have = read_header(path)
    want = expected_levels(w, h, dxgi)
    return (w, h, dxgi, have, want) if have < want else None


def run(folder: Path) -> tuple[int, list[str]]:
    files = sorted(folder.glob('*.dds'))
    errors = []
    if not files:
        errors.append('no DDS files in %s' % folder)
    for p in files:
        bad = short_chain(p)
        if bad:
            w, h, dxgi, have, want = bad
            errors.append('%s is %dx%d (dxgi %d) with %d mip level(s), expected %d' % (p.name, w, h, dxgi, have, want))
    return len(files), errors


def _write_dummy_dds(path: Path, w: int, h: int, dxgi: int, n_levels: int) -> None:
    if dxgi in BC_DXGI_FORMATS:
        levels = [bytes(bc.level_size(max(1, w >> k), max(1, h >> k), dxgi)) for k in range(n_levels)]
        bc.write_dds(path, w, h, dxgi, levels)
        return
    # Minimal DX10 uncompressed DDS header (e.g. DXGI 28 = R8G8B8A8_UNORM) for self-test
    hdr = bytearray(148)
    hdr[0:4] = b'DDS '
    struct.pack_into('<5I', hdr, 4, 124, 0x21007, h, w, 0)
    struct.pack_into('<I', hdr, 28, n_levels)
    struct.pack_into('<I', hdr, 76, 32)
    struct.pack_into('<I', hdr, 80, 0x4)
    hdr[84:88] = b'DX10'
    struct.pack_into('<5I', hdr, 128, dxgi, 3, 0, 1, 0)
    path.write_bytes(bytes(hdr))


def self_test() -> list[str]:
    """Fail 1024x1024 with 2 levels, 256x16 BC with 2 levels, and 256x256 uncompressed with 7 levels;
    pass full chains for each."""
    failed = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cases = (
            ('bc_1024_2mips.dds', 1024, 1024, 83, 2, True),
            ('bc_1024_full.dds', 1024, 1024, 83, 9, False),
            ('bc_256x16_2mips.dds', 256, 16, 72, 2, True),
            ('bc_256x16_full.dds', 256, 16, 72, 3, False),
            ('rgba_256_7mips.dds', 256, 256, 28, 7, True),
            ('rgba_256_full.dds', 256, 256, 28, 9, False),
        )
        for name, w, h, dxgi, levels, want_fail in cases:
            _write_dummy_dds(td / name, w, h, dxgi, levels)
            failed_now = short_chain(td / name) is not None
            if failed_now != want_fail:
                failed.append('self-test: %s gave %s, expected %s' % (
                    name, 'a failure' if failed_now else 'a pass', 'a failure' if want_fail else 'a pass'))
    return failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args()
    count, errors = run(TEXTURES)
    if a.self_test:
        st = self_test()
        errors += st
        if not st:
            print('self-test: 1024x1024@2mips, 256x16@2mips, and rgba256@7mips fail; full chains pass')
    for e in errors[:40]:
        print('FAIL: ' + e)
    short_n = sum(1 for e in errors if 'expected' in e)
    print('textures checked: %d, short mip chains: %d' % (count, short_n))
    if errors:
        print('FAIL: %d problem(s)' % len(errors))
        return 1
    print('PASS: verify_materials_mips')
    return 0


if __name__ == '__main__':
    sys.exit(main())
