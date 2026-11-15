"""Scan every texture in the game's .ass/.asstrm archives and hash each mip level the way Remix 1.5.2 would.

Remix names a texture by XXH3_64 of the mip-0 upload buffer (see lead.remix_upload_bytes). Blacklist streams:
a texture first exists as a small "resident" texture whose mip 0 is the top level stored in the .ass, then as a
larger texture whose mip 0 is the top level stored in the .asstrm. So one texture shows up under (at least) two
hashes. This script records the hash of EVERY level, largest first, so the pipeline and the checks can say which
level a live hash came from.

Usage: python scan_textures.py <UMDs dir> <out.json>
Read-only on the game folder (archives are opened 'rb'). Slow: reads ~11 GB; runs one process per archive.
"""
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import xxhash

sys.path.insert(0, str(Path(__file__).parent))
from lead import K, FORMATS, LeadArchive, mip_block_size, num, parse_fields, read_index, remix_upload_bytes  # noqa: E402

NO_STREAM = 0xFFFFFFFF


def xx(b):
    return '%016X' % xxhash.xxh3_64_intdigest(b)


def chain(w, h, fmt, n):
    """[(w, h, size)] for n levels starting at w x h."""
    out = []
    for k in range(n):
        lw, lh = max(1, w >> k), max(1, h >> k)
        out.append((lw, lh, mip_block_size(lw, lh, fmt)))
    return out


def stream_levels(w, h, fmt, s_size):
    """Levels above the resident top stored in the .asstrm, largest first. Their sizes must sum to s_size."""
    total, levels = 0, []
    for k in range(1, 6):
        lw, lh = w << k, h << k
        levels.insert(0, (lw, lh, mip_block_size(lw, lh, fmt)))
        total += levels[0][2]
        if total == s_size:
            return levels
    raise ValueError('s_size %d is not a whole number of larger mips' % s_size)


def scan_archive(ass_path):
    ass_path = Path(ass_path)
    idx = read_index(ass_path)
    t2d, mips = idx[K['textures_2d']], idx[K['texture_mips']]
    arc = LeadArchive(ass_path)
    strm_path = ass_path.with_suffix('.asstrm')
    strm = LeadArchive(strm_path) if strm_path.exists() else None
    cache, rows = {}, []
    for i in sorted(range(len(t2d)), key=lambda i: mips[t2d[i][K['m_index']]][K['POS']]):
        t = t2d[i]
        mi = t[K['m_index']]
        s_pos = t.get(K['s_pos'], NO_STREAM)
        rec = {'ass': ass_path.name, 'i': i, 'name': '%08X' % num(t[K['name']]), 'm_index': mi}
        try:
            if any(t[K['offset']]):
                raise ValueError('texture is in an atlas')  # never seen: offset is zero in all records
            if mi not in cache:
                m = mips[mi]
                arc.position = m[K['POS']]
                f = parse_fields(arc.read(m[K['size']]))
                w, h, fmt = num(f[K['u_size']]), num(f[K['v_size']]), num(f[K['FORMAT']])
                if fmt not in FORMATS:
                    raise ValueError('unknown FORMAT %d' % fmt)
                handle = f[K['handle']]
                nb = num(f[K['nb_mips']]) if K['nb_mips'] in f else 1
                levels, pos = [], 0
                for lw, lh, sz in chain(w, h, fmt, nb):
                    if pos + sz > len(handle):
                        raise ValueError('handle shorter than its mip chain')
                    levels.append({'w': lw, 'h': lh, 'src': 'ass', 'off': pos,
                                   'hash': xx(remix_upload_bytes(handle[pos:pos + sz], lw, lh, fmt))})
                    pos += sz
                cache[mi] = {'fmt': fmt, 'w': w, 'h': h, 'mips_pos': m[K['POS']], 'mips_size': m[K['size']],
                             'levels': levels}
            rec.update(cache[mi])
            rec['levels'] = list(rec['levels'])
            if s_pos != NO_STREAM:
                if strm is None:
                    raise ValueError('streamed texture but no .asstrm')
                rec['s_pos'] = s_pos
                streamed, pos = [], s_pos
                for lw, lh, sz in stream_levels(rec['w'], rec['h'], rec['fmt'], t[K['s_size']]):
                    strm.position = pos
                    streamed.append({'w': lw, 'h': lh, 'src': 'asstrm', 'off': pos,
                                     'hash': xx(remix_upload_bytes(strm.read(sz), lw, lh, rec['fmt']))})
                    pos += sz
                s1 = t.get(K['s1_pos'], NO_STREAM)
                if s1 != NO_STREAM and s1 != s_pos + mip_block_size(streamed[0]['w'], streamed[0]['h'], rec['fmt']):
                    raise ValueError('s1_pos is not right after the streamed top mip')
                rec['levels'] = streamed + rec['levels']
                rec['resident_level'] = len(streamed)
            else:
                rec['resident_level'] = 0
        except Exception as e:  # record, never guess
            rec['error'] = '%s: %s' % (type(e).__name__, e)
        rows.append(rec)
    arc.close()
    if strm:
        strm.close()
    return rows


def main():
    umd_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    archives = sorted(str(p) for p in umd_dir.glob('*.ass') if p.suffix.lower() == '.ass')
    rows = []
    with ProcessPoolExecutor() as ex:
        for path, part in zip(archives, ex.map(scan_archive, archives)):
            rows += part
            print('scanned', Path(path).name, len(part), flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows), 'utf-8')
    print('textures', len(rows), 'errors', sum('error' in r for r in rows))


if __name__ == '__main__':
    main()
