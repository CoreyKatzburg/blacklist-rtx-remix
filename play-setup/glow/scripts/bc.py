"""Block-compression codecs (numpy, vectorised) and a DDS writer for the materials pipeline.

Decoders: the game's formats (DXT1/BC1, DXT5/BC3, ATI2/BC5, L8, A8R8G8B8).
Encoders: BC4 (one channel) and BC5 (two channels), the only formats the pipeline re-encodes. Emissive masks
reuse the game's own BC1/BC3 blocks (see tint_color_blocks), so no colour encoder is needed.
"""
import struct

import numpy as np


def _blocks(data, w, h, block_bytes):
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    return np.frombuffer(data, np.uint8, bw * bh * block_bytes).reshape(bh, bw, block_bytes), bw, bh


def _unblock(px, bw, bh, w, h):
    """(bh, bw, 16, C) texels in block order -> (h, w, C) image, cropped to w x h."""
    c = px.shape[-1]
    img = px.reshape(bh, bw, 4, 4, c).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, c)
    return img[:h, :w]


def _to_blocks(img):
    """(h, w, C) -> (bh, bw, 16, C), edge-padded to a multiple of 4."""
    h, w, c = img.shape
    ph, pw = (-h) % 4, (-w) % 4
    if ph or pw:
        img = np.pad(img, ((0, ph), (0, pw), (0, 0)), mode='edge')
    bh, bw = img.shape[0] // 4, img.shape[1] // 4
    return img.reshape(bh, 4, bw, 4, c).transpose(0, 2, 1, 3, 4).reshape(bh, bw, 16, c)


def _rgb565(v):
    v = v.astype(np.uint32)
    r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
    return np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)], -1).astype(np.float32)


def _color_block(b, four_color_always):
    """b: (..., 8) BC1 colour blocks -> (..., 16, 4) uint8 RGBA."""
    c0 = b[..., 0].astype(np.uint16) | (b[..., 1].astype(np.uint16) << 8)
    c1 = b[..., 2].astype(np.uint16) | (b[..., 3].astype(np.uint16) << 8)
    e0, e1 = _rgb565(c0), _rgb565(c1)
    four = (c0 > c1) | four_color_always
    f = four[..., None]
    p2 = np.where(f, (2 * e0 + e1) / 3, (e0 + e1) / 2)
    p3 = np.where(f, (e0 + 2 * e1) / 3, 0)
    pal = np.stack([e0, e1, p2, p3], -2)  # (..., 4, 3)
    alpha = np.stack([np.full(four.shape, 255), np.full(four.shape, 255), np.full(four.shape, 255),
                      np.where(four, 255, 0)], -1)
    bits = b[..., 4].astype(np.uint32) | (b[..., 5].astype(np.uint32) << 8) | (b[..., 6].astype(np.uint32) << 16) | (
        b[..., 7].astype(np.uint32) << 24)
    idx = (bits[..., None] >> (2 * np.arange(16, dtype=np.uint32))) & 3
    rgb = np.take_along_axis(pal, idx[..., None].astype(np.intp).repeat(3, -1), -2)
    a = np.take_along_axis(alpha, idx.astype(np.intp), -1)
    return np.concatenate([np.rint(rgb), a[..., None]], -1).astype(np.uint8)


def _alpha_block(b):
    """b: (..., 8) BC4/BC3-alpha blocks -> (..., 16) uint8."""
    a0, a1 = b[..., 0].astype(np.float32), b[..., 1].astype(np.float32)
    k = np.arange(1, 7, dtype=np.float32)
    eight = (a0 > a1)[..., None]
    inter8 = ((7 - k) * a0[..., None] + k * a1[..., None]) / 7
    k5 = np.arange(1, 5, dtype=np.float32)
    inter6 = ((5 - k5) * a0[..., None] + k5 * a1[..., None]) / 5
    inter6 = np.concatenate([inter6, np.zeros(a0.shape + (1,), np.float32),
                             np.full(a0.shape + (1,), 255, np.float32)], -1)
    pal = np.concatenate([a0[..., None], a1[..., None], np.where(eight, inter8, inter6)], -1)
    bits = np.zeros(b.shape[:-1], np.uint64)
    for i in range(6):
        bits |= b[..., 2 + i].astype(np.uint64) << np.uint64(8 * i)
    idx = (bits[..., None] >> (np.uint64(3) * np.arange(16, dtype=np.uint64))) & np.uint64(7)
    return np.rint(np.take_along_axis(pal, idx.astype(np.intp), -1)).astype(np.uint8)


def decode(data, w, h, fmt):
    """Decode one mip level of a game texture to (h, w, 4) uint8 RGBA. fmt is the archive FORMAT code."""
    if fmt == 4:  # DXT1
        b, bw, bh = _blocks(data, w, h, 8)
        return _unblock(_color_block(b, False), bw, bh, w, h)
    if fmt in (5, 6):  # DXT3 / DXT5 (only the colour part differs in alpha handling)
        b, bw, bh = _blocks(data, w, h, 16)
        rgba = _color_block(b[..., 8:], True)
        if fmt == 6:
            rgba[..., 3] = _alpha_block(b[..., :8])
        else:
            nib = np.stack([b[..., :8] & 15, b[..., :8] >> 4], -1).reshape(bh, bw, 16)
            rgba[..., 3] = nib * 17
        return _unblock(rgba, bw, bh, w, h)
    if fmt == 16:  # ATI2 is BC5 with red and green swapped: D3D9 samples block 1 as red (d3d9_format.cpp swizzle G, R)
        b, bw, bh = _blocks(data, w, h, 16)
        out = np.zeros((bh, bw, 16, 4), np.uint8)
        out[..., 0] = _alpha_block(b[..., 8:])
        out[..., 1] = _alpha_block(b[..., :8])
        out[..., 3] = 255
        return _unblock(out, bw, bh, w, h)
    if fmt == 10:  # L8
        v = np.frombuffer(data, np.uint8, w * h).reshape(h, w)
        return np.stack([v, v, v, np.full_like(v, 255)], -1)
    if fmt == 1:  # A8R8G8B8 is B, G, R, A in memory
        v = np.frombuffer(data, np.uint8, w * h * 4).reshape(h, w, 4)
        return v[..., [2, 1, 0, 3]].copy()
    raise ValueError('cannot decode FORMAT %d' % fmt)


def encode_bc4(chan):
    """(h, w) uint8 -> BC4 bytes. Endpoints are the block min/max (8-value mode, a0 > a1); each texel takes the
    nearest of the 8 palette values, so the error is at most (max - min) / 14 per block."""
    blk = _to_blocks(chan[..., None])[..., 0].astype(np.int32)  # (bh, bw, 16)
    hi, lo = blk.max(-1), blk.min(-1)
    flat = hi == lo
    a0 = np.where(flat, np.minimum(hi + 1, 255), hi)  # a0 > a1 keeps 8-value mode even for flat blocks
    a1 = np.where(flat & (hi == 255), 254, lo)
    a0 = np.where(flat & (hi == 255), 255, a0)
    w0 = np.array([7, 0, 6, 5, 4, 3, 2, 1])  # palette order: a0, a1, then 6 interpolants
    pal = (w0 * a0[..., None] + (7 - w0) * a1[..., None]) / 7.0
    idx = np.abs(blk[..., :, None] - pal[..., None, :]).argmin(-1).astype(np.uint64)  # (bh, bw, 16)
    bits = (idx << (np.uint64(3) * np.arange(16, dtype=np.uint64))).sum(-1, dtype=np.uint64)
    out = np.zeros(blk.shape[:2] + (8,), np.uint8)
    out[..., 0], out[..., 1] = a0, a1
    for i in range(6):
        out[..., 2 + i] = (bits >> np.uint64(8 * i)) & np.uint64(255)
    return out.tobytes()


def encode_bc5(r, g):
    """Two (h, w) uint8 channels -> BC5 bytes (red block then green block per 4x4 tile)."""
    br = np.frombuffer(encode_bc4(r), np.uint8).reshape(-1, 8)
    bg = np.frombuffer(encode_bc4(g), np.uint8).reshape(-1, 8)
    return np.concatenate([br, bg], 1).tobytes()


def decode_bc4(data, w, h):
    b, bw, bh = _blocks(data, w, h, 8)
    return _unblock(_alpha_block(b)[..., None], bw, bh, w, h)[..., 0]


def decode_bc5(data, w, h):
    b, bw, bh = _blocks(data, w, h, 16)
    return (_unblock(_alpha_block(b[..., :8])[..., None], bw, bh, w, h)[..., 0],
            _unblock(_alpha_block(b[..., 8:])[..., None], bw, bh, w, h)[..., 0])


def tint_color_blocks(data, fmt, tint):
    """Scale the RGB565 endpoints of every BC1 (fmt 4) or BC3 (fmt 6) colour block by tint (3 floats in [0, 1]).
    Palette entries are linear blends of the endpoints, so this tints every texel without re-encoding. Endpoint
    order is kept (it selects BC1's 3-colour mode), so a tint that makes c0 == c1 only loses one gradient step."""
    t = np.asarray(tint, np.float32)
    if np.all(t >= 1.0):
        return bytes(data)
    stride, off = (8, 0) if fmt == 4 else (16, 8)
    b = np.frombuffer(data, np.uint8).reshape(-1, stride).copy()
    for e in (0, 2):
        v = b[:, off + e].astype(np.uint32) | (b[:, off + e + 1].astype(np.uint32) << 8)
        r = np.rint(((v >> 11) & 31) * t[0]).astype(np.uint32)
        g = np.rint(((v >> 5) & 63) * t[1]).astype(np.uint32)
        bl = np.rint((v & 31) * t[2]).astype(np.uint32)
        nv = (r << 11) | (g << 5) | bl
        b[:, off + e], b[:, off + e + 1] = nv & 255, nv >> 8
    if fmt == 4:  # keep each block's mode: c0 > c1 (4 colours) or c0 <= c1 (3 colours + transparent)
        c0 = b[:, 0].astype(np.uint32) | (b[:, 1].astype(np.uint32) << 8)
        c1 = b[:, 2].astype(np.uint32) | (b[:, 3].astype(np.uint32) << 8)
        orig = np.frombuffer(data, np.uint8).reshape(-1, 8)
        was4 = (orig[:, 0].astype(np.uint32) | (orig[:, 1].astype(np.uint32) << 8)) > (
            orig[:, 2].astype(np.uint32) | (orig[:, 3].astype(np.uint32) << 8))
        lost = was4 & (c0 <= c1)  # tint collapsed the endpoints: make it a flat block in 4-colour mode
        if lost.any():
            nv0 = np.minimum(c0[lost] + 1, 0xFFFF)
            b[lost, 0], b[lost, 1] = nv0 & 255, nv0 >> 8
            b[lost, 2], b[lost, 3] = c0[lost] & 255, c0[lost] >> 8
            b[lost, 4:8] = 0
    return b.tobytes()


# DXGI formats written by the pipeline
DXGI = {'BC1_SRGB': 72, 'BC3_SRGB': 78, 'BC4': 80, 'BC5': 83}
BLOCK_BYTES = {72: 8, 78: 16, 80: 8, 83: 16}


def level_size(w, h, dxgi):
    return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * BLOCK_BYTES[dxgi]


def write_dds(path, w, h, dxgi, levels):
    """Write a 2D DDS with a DX10 header. levels: bytes per mip, largest first."""
    for k, lv in enumerate(levels):
        if len(lv) != level_size(max(1, w >> k), max(1, h >> k), dxgi):
            raise ValueError('mip %d has %d bytes, expected %d' % (k, len(lv), level_size(max(1, w >> k), max(1, h >> k), dxgi)))
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000  # caps, height, width, pixelformat, mipmapcount, linearsize
    hdr = struct.pack('<4s7I44x', b'DDS ', 124, flags, h, w, len(levels[0]), 1, len(levels))
    pf = struct.pack('<2I4s5I', 32, 0x4, b'DX10', 0, 0, 0, 0, 0)
    caps = struct.pack('<4I4x', 0x1000 | 0x400000 | 0x8, 0, 0, 0)  # texture | mipmap | complex
    dx10 = struct.pack('<5I', dxgi, 3, 0, 1, 0)  # TEXTURE2D, array size 1
    with open(path, 'wb') as f:
        f.write(hdr + pf + caps + dx10)
        for lv in levels:
            f.write(lv)


def read_dds(path, want=None):
    """-> (w, h, dxgi, [level bytes]) for files written by write_dds (DX10 header, 2D, BC formats above).
    Validates the header and that the file size matches the mip chain exactly. want: level indices to read
    (others come back as None); default all."""
    with open(path, 'rb') as f:
        b = f.read(148)
        if len(b) < 148 or b[:4] != b'DDS ' or struct.unpack_from('<I', b, 4)[0] != 124:
            raise ValueError('not a DDS file')
        h, w, _, _, n = struct.unpack_from('<5I', b, 12)
        if b[84:88] != b'DX10':
            raise ValueError('no DX10 header')
        dxgi, dim, _, arr = struct.unpack_from('<4I', b, 128)
        if dim != 3 or arr != 1 or dxgi not in BLOCK_BYTES:
            raise ValueError('unexpected DX10 header (format %d, dimension %d, array %d)' % (dxgi, dim, arr))
        sizes = [level_size(max(1, w >> k), max(1, h >> k), dxgi) for k in range(max(1, n))]
        total = 148 + sum(sizes)
        f.seek(0, 2)
        if f.tell() != total:
            raise ValueError('file is %d bytes, header implies %d' % (f.tell(), total))
        levels, pos = [], 148
        for k, sz in enumerate(sizes):
            if want is None or k in want:
                f.seek(pos)
                levels.append(f.read(sz))
            else:
                levels.append(None)
            pos += sz
    return w, h, dxgi, levels
