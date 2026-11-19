"""Is the picture the replay saved the picture Remix made? (WKR-7)
remix_shot_vs_saved.py <Remix screenshot .dds> <saved frames\\f<frame>.png> [<other png> ...]
Remix's own screenshot (rtxImagePostTonemapping, written by DXVK_DUMP_SCREENSHOT_FRAME) is read from the DDS file,
turned into an 8-bit picture two ways (values as stored, and linear turned into sRGB) and compared with each PNG
with apitrace's snapdiff.Comparer: precision in bits (8 = equal to the last bit) and pixels differing by more than
5 percent. Prints one JSON object; decides nothing by itself.
"""
import json
import os
import struct
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, r"D:\BlacklistOverhaul\experiments\replay-rig\tools\apitrace-14.0-win32\lib\scripts")
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from snapdiff import Comparer  # noqa: E402

dds = sys.argv[1]
raw = open(dds, "rb").read()
assert raw[:4] == b"DDS ", "not a DDS file"
hdr = struct.unpack("<31I", raw[4:128])
height, width = hdr[2], hdr[3]
pf_flags, fourcc, bitcount = hdr[19], hdr[20], hdr[21]
masks = hdr[22:26]
off = 128
dxgi = None
if fourcc == 0x30315844:  # "DX10"
    dxgi = struct.unpack("<5I", raw[128:148])[0]
    off = 148
body = raw[off:]
bpp = len(body) // (width * height)
info = {"dds": dds, "width": width, "height": height, "pfFlags": pf_flags, "fourcc": fourcc, "bitCount": bitcount,
        "masks": [hex(m) for m in masks], "dxgi": dxgi, "bytesPerPixel": bpp, "bodyBytes": len(body)}
if bpp == 8 and (fourcc == 113 or dxgi == 10):
    px = np.frombuffer(body, dtype="<f2", count=width * height * 4).reshape(height, width, 4).astype(np.float64)[..., :3]
elif bpp == 8:
    px = np.frombuffer(body, dtype="<u2", count=width * height * 4).reshape(height, width, 4).astype(np.float64)[..., :3] / 65535.0
elif bpp == 16:
    px = np.frombuffer(body, dtype="<f4", count=width * height * 4).reshape(height, width, 4).astype(np.float64)[..., :3]
elif bpp == 4:
    px = np.frombuffer(body, dtype=np.uint8, count=width * height * 4).reshape(height, width, 4).astype(np.float64)[..., :3] / 255.0
    if masks[0] == 0xFF0000:
        px = px[..., ::-1]
else:
    print(json.dumps({**info, "notMeasured": "unknown pixel layout"}))
    sys.exit(2)
info.update({"min": float(np.nanmin(px)), "max": float(np.nanmax(px)), "mean": float(np.nanmean(px)),
             "nonFinite": int((~np.isfinite(px)).sum())})
px = np.nan_to_num(px, nan=0.0, posinf=1.0, neginf=0.0).clip(0.0, 1.0)
srgb = np.where(px <= 0.0031308, px * 12.92, 1.055 * np.power(px, 1 / 2.4) - 0.055)
tmpdir = os.environ.get("TEMP", ".")
results = []
for label, arr in (("asStored", px), ("linearToSrgb", srgb)):
    img8 = np.clip(np.rint(arr * 255.0), 0, 255).astype(np.uint8)
    tmp = os.path.join(tmpdir, "remix_shot_%s_%d.png" % (label, os.getpid()))
    Image.fromarray(img8, "RGB").save(tmp)
    try:
        for png in sys.argv[2:]:
            other = Image.open(png).convert("RGB")
            row = {"encoding": label, "png": png, "pngSize": list(other.size)}
            if other.size != (width, height):
                row["sizeMismatch"] = True
            else:
                c = Comparer(tmp, png)
                o = np.asarray(other, dtype=np.float64)
                row.update({"precisionBits": round(c.precision(), 2), "pixelsOver5Percent": int(c.ae()),
                            "meanAbsDiff8bit": round(float(np.abs(img8.astype(np.float64) - o).mean()), 3),
                            "shotMean8bit": round(float(img8.mean()), 2), "pngMean8bit": round(float(o.mean()), 2)})
            results.append(row)
    finally:
        os.remove(tmp)
info["pixels"] = width * height
info["compares"] = results
print(json.dumps(info))
