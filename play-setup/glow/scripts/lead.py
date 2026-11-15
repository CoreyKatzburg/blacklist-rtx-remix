"""Read-only Python port of BlacklistExplorer.LeadArchive (EPCK paged zlib) and Fields.Parse.
Files are opened 'rb' only."""
import struct
import zlib

from leadhash import lead_hash

H_INT = lead_hash('INT')
H_BOOL = lead_hash('bool')


def _compact(read):
    num = read()
    val = num & 0x3F
    more = num & 0x40
    shift = 6
    neg = num & 0x80
    for _ in range(4):
        if not more:
            break
        num = read()
        val |= (num & 0x7F) << shift
        more = num & 0x80
        shift += 7
    if more:
        raise ValueError('Invalid compact integer')
    return -val if neg else val


class LeadArchive:
    def __init__(self, path):
        self.f = open(path, 'rb')
        self.cache = {}
        magic = self.f.read(4)
        if magic != b'EPCK':
            raise ValueError('Not an EPCK archive')
        self.block, self.length, _ = struct.unpack('<III', self.f.read(12))
        b = self.f.read(1)[0]
        if self.block <= 0 or self.block > 16777216 or not (b & 0x80):
            raise ValueError('Unsupported EPCK header')
        num, n2, n3 = struct.unpack('<III', self.f.read(12))
        table = self.f.read(n3 if n3 else n2)
        if n3:
            table = zlib.decompress(table)
        if len(table) != n2:
            raise ValueError('page table length')
        pos = [0]

        def rb():
            v = table[pos[0]]
            pos[0] += 1
            return v
        count = _compact(rb)
        if count != (self.length + self.block - 1) // self.block:
            raise ValueError('page count mismatch')
        off = 29 + num
        self.pages = []
        for i in range(count):
            stored = _compact(rb)
            flags = rb()
            if b & 0x30:
                pos[0] += 8
            size = min(self.block, self.length - i * self.block)
            if stored == 0:
                stored = size
            self.pages.append((off, stored, size, flags))
            off += stored
        if pos[0] != len(table):
            raise ValueError('unexpected page table data')
        self.position = 0

    def close(self):
        self.f.close()

    def _page(self, i):
        v = self.cache.get(i)
        if v is None:
            off, stored, size, flags = self.pages[i]
            self.f.seek(off)
            v = self.f.read(stored)
            if flags & 1:
                v = zlib.decompress(v)
            elif flags:
                raise ValueError('unsupported page compression')
            if len(v) != size:
                raise ValueError('invalid page length')
            if len(self.cache) > 16:
                self.cache.clear()
            self.cache[i] = v
        return v

    def read(self, n):
        if n < 0 or self.position < 0 or self.position + n > self.length:
            raise ValueError('read outside archive')
        out = bytearray()
        while len(out) < n:
            i, o = divmod(self.position, self.block)
            pg = self._page(i)
            take = min(n - len(out), len(pg) - o)
            out += pg[o:o + take]
            self.position += take
        return bytes(out)

    def u32(self):
        return struct.unpack('<I', self.read(4))[0]


def parse_fields(b, start=0, end=-1, depth=0):
    if end < 0:
        end = len(b)
    if start < 0 or end < start or end > len(b) or depth > 24:
        raise ValueError('bounds')
    out = {}
    p = start
    while p < end:
        if end - p < 16:
            raise ValueError('truncated header')
        size, key, kind, typ = struct.unpack_from('<IIII', b, p)
        d = p + 16
        if size > end - d:
            raise ValueError('field outside parent')
        if kind == 0:
            v = parse_fields(b, d, d + size, depth + 1)
        elif (kind & 2) and (kind & 8):
            cnt = struct.unpack_from('<I', b, d)[0]
            if typ == H_INT and size == 4 + 4 * cnt:
                v = list(struct.unpack_from('<%dI' % cnt, b, d + 4))
            else:
                v = bytes(b[d:d + size])
        elif (kind & 7) == 2:
            cnt = struct.unpack_from('<I', b, d)[0]
            if cnt > size // 4:
                raise ValueError('array count')
            q = d + 4
            v = []
            for _ in range(cnt):
                ln = struct.unpack_from('<I', b, q)[0]
                q += 4
                v.append(parse_fields(b, q, q + ln, depth + 1))
                q += ln
            if q != d + size:
                raise ValueError('array length mismatch')
        elif (kind & 8) and typ == H_INT and size == 4:
            v = struct.unpack_from('<I', b, d)[0]
        elif (kind & 8) and typ == lead_hash('bool') and size == 1:
            v = b[d] != 0
        else:
            v = bytes(b[d:d + size])
        if key in out:
            raise ValueError('duplicate field')
        out[key] = v
        p = d + size
    return out


def num(v):
    return v if isinstance(v, int) else struct.unpack_from('<I', v, 0)[0]


K = {n: lead_hash(n) for n in ('textures_2d', 'texture_mips', 'atlases_2d', 'name', 'offset', 'm_index', 's_pos',
                               's1_pos', 's_size', 'POS', 'size', 'handle', 'u_size', 'v_size', 'FORMAT', 'nb_mips')}


def read_index(path):
    a = LeadArchive(path)
    try:
        ver = a.u32()
        ln = a.u32()
        if ver != 7 or ln > min(a.length - 8, 67108864):
            raise ValueError('Unsupported texture index')
        return parse_fields(a.read(ln))
    finally:
        a.close()


# Game texture FORMAT codes -> (D3D9 format, bytes per 4x4 block, or bytes per texel when block is False).
# 1/4/6/16 match Remix capture DDS headers (A8R8G8B8, DXT1, DXT5, ATI2); 10 is 1 byte per texel: the
# handle length equals the sum of w*h over the chain for all 9971 such records.
FORMATS = {1: ('A8R8G8B8', 4, False), 4: ('DXT1', 8, True), 5: ('DXT3', 16, True), 6: ('DXT5', 16, True),
           10: ('L8', 1, False), 16: ('ATI2', 16, True)}


def mip_block_size(w, h, fmt):
    """Bytes of one tightly packed mip level as stored in the archives, or None for an unknown format."""
    f = FORMATS.get(fmt)
    if f is None:
        return None
    if f[2]:
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * f[1]
    return w * h * f[1]


def remix_upload_bytes(data, w, h, fmt):
    """The bytes Remix 1.5.2 hashes for a mip-0 upload of this level.

    D3D9CommonTexture::SetupForRtxFrom hashes XXH3_64(buffer of subresource 0, buffer size), and GetMipSize sizes
    that buffer as align(elementSize * blocksWide, 4) * blocksHigh. Only rows narrower than 4 bytes (8-bit
    textures under 4 texels wide) differ from the tight archive layout; their padding is assumed to be zero."""
    f = FORMATS[fmt]
    row = (max(1, (w + 3) // 4) * f[1]) if f[2] else w * f[1]
    rows = max(1, (h + 3) // 4) if f[2] else h
    if row % 4 == 0:
        return data
    pitch = (row + 3) & ~3
    return b''.join(data[r * row:(r + 1) * row] + bytes(pitch - row) for r in range(rows))


class LevelReader:
    """Reads mip levels recorded by scan_textures.py. Keeps archives open; call close() when done."""

    def __init__(self, umd_dir):
        self.umd_dir = umd_dir
        self.arcs = {}
        self.handles = {}

    def _arc(self, name):
        a = self.arcs.get(name)
        if a is None:
            a = self.arcs[name] = LeadArchive(self.umd_dir / name)
        return a

    def level(self, rec, k):
        """Bytes of level k of a textures.json record (largest first, streamed levels included)."""
        lv = rec['levels'][k]
        size = mip_block_size(lv['w'], lv['h'], rec['fmt'])
        if lv['src'] == 'asstrm':
            a = self._arc(rec['ass'][:-4] + '.asstrm')
            a.position = lv['off']
            return a.read(size)
        key = (rec['ass'], rec['mips_pos'])
        h = self.handles.get(key)
        if h is None:
            a = self._arc(rec['ass'])
            a.position = rec['mips_pos']
            h = parse_fields(a.read(rec['mips_size']))[K['handle']]
            if len(self.handles) > 64:
                self.handles.clear()
            self.handles[key] = h
        return h[lv['off']:lv['off'] + size]

    def close(self):
        for a in self.arcs.values():
            a.close()
        self.arcs.clear()
