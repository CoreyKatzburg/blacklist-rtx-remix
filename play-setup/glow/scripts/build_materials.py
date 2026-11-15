"""Build the game-wide RTX Remix materials mod: materials/mod.usda and materials/textures/*.dds.

One run does everything (python build_materials.py, from the glow venv):
  1. Caches the slow archive reads in play-setup/glow/cache/ (only if missing, or with --rescan):
     textures.json (scan_textures.py: every texture, every mip level hashed the way Remix hashes it) and
     catalogs.json (dump_catalogs.ps1: the game's material records).
  2. Pairs every diffuse texture with its normal map, spec map and glow settings using the game's own material
     records (Diff Map / Normal Map / Spec Map / Gloss Map Scale / Specular Mask Scale / EmissiveScale).
  3. Converts them to Remix inputs (formulas below) as BC-compressed DDS files.
  4. Writes one override per Remix texture hash of each diffuse texture: the resident (blurry) top mip AND the
     streamed (sharp) top mip, because Blacklist streams textures and Remix sees both.
  5. Deletes files in materials/textures that this run did not produce, and writes materials/manifest.json and
     play-setup/glow/texture_hashes.csv for the offline checks (tests/verify_materials_*.py).

Formulas (from the game's deferred material shader, ps_06C272E9 in dx11-lighting/captures, which the DX9 build
shares the archives and material data with):
  game:  Blinn-Phong exponent n = (GlossMapScale * B)^R, specular intensity I = G * SpecularMaskScale,
         where R, G, B are the spec map channels in [0, 1]. (The shader multiplies B by lerp(B, 1, data0.w);
         data0.w is not identified, we use 1, i.e. the factor is B.)
  Remix: alpha = sqrt(2 / (n + 2)) (Beckmann-equivalent width of a Blinn-Phong lobe, Walter et al. 2007) and
         Remix's perceptual roughness = sqrt(alpha) (Remix squares it, brdf.slangh).
         Where I is under the 4% dielectric reflectance Remix always uses, the game shows almost no highlight, so
         roughness is pulled toward 1: roughness = 1 - (1 - roughness) * clamp(I / 0.04, 0, 1).
         metallic = clamp((I - 0.5) / 0.5, 0, 1): only surfaces whose game specular is far above any dielectric.
  normal: X = R, Y = G as the shader samples them (for ATI2 that is block 1 and block 0: D3D9 swizzles ATI2),
         Z = sqrt(1 - X^2 - Y^2) exactly as the game shader rebuilds it; written as Remix's unsigned hemi-octahedral
         encoding (packing.slangh). Y is not flipped: the game's maps are curl-free with +Y along +V, which is
         Remix's bitangent direction (math.slangh genTangSpace); verify_materials_mod.py tests all four channel
         orders on the output.
  glow:  the game's emissive shader outputs diffuse * colour * scale with no lighting, so the emissive mask is the
         diffuse texture itself (its BC blocks copied, endpoints tinted by DiffuseColorHSB), and
         emissive_intensity = min(EmissiveScale * EMISSIVE_K, EMISSIVE_MAX).
Overrides never set diffuse_texture: Remix then keeps the game's own colour texture (MaterialData::merge only
copies inputs the mod did not author), so no colour copy is shipped.
"""
import argparse
import colorsys
import json
import math
import os
import struct
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import xxhash

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import bc  # noqa: E402
import lead  # noqa: E402

GLOW = SCRIPTS.parent
REMIX = GLOW.parent.parent
MATERIALS = REMIX / 'materials'
TEXTURES = MATERIALS / 'textures'
CACHE = GLOW / 'cache'
UMD_DIR = Path(r"D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM\UMDs")

# ---- tuning (every number the output depends on) --------------------------------------------------------------
PIPELINE_VERSION = 2          # bump to force every texture to be regenerated
NORMAL_MAX = 4096             # full resolution: keep 2048x2048 character/world bump detail (no blanket 1024 cap)
ROUGH_MAX = 4096              # full resolution: keep 2048x2048 character/world roughness detail (no blanket 512 cap)
EMISSIVE_MAX = 1024           # largest emissive mask side
HEIGHT_MAX = 2048             # largest parallax height map side
MIN_DIFFUSE_SIDE = 16         # diffuse textures smaller than this are shared placeholders: never overridden
DIELECTRIC_F0 = 0.04
MIN_SPEC_VISIBILITY = 0.35    # preserves panel roughness contrast on dark cloth/seams where G*sms < 0.04
METAL_START, METAL_FULL = 0.5, 1.0
EMISSIVE_K, EMISSIVE_MAX_INTENSITY = 1.0, 50.0
UNIFORM_TOLERANCE = 2         # 8-bit steps: a map this flat becomes a constant instead of a texture
FLAT_NORMAL = 0.03            # |x|, |y| below this everywhere: no normal map
VRAM_BUDGET = 1536 * 2 ** 20  # generated textures of the heaviest level (plus shared archives) must fit this
POISON_HASHES = {'2164293A60D148AC'}
ALWAYS_LOADED = ('Blacklist.umd', 'PEC-Main.umd')  # catalog groups every level uses

EMPTY_GLOW_LAYER = '''#usda 1.0
(
    customLayerData = {
        string lightspeed_game_name = "Blacklist"
        string lightspeed_layer_type = "replacement"
    }
    doc = "Intentionally empty: glow overrides are in tools/remix/materials/mod.usda (build_materials.py). Kept because Play_Blacklist_with_Remix.ps1 requires this file."
)
'''

PBR_CLASSES = {'Engine.SCX_basic_material', 'Engine.SCX_detail_normal_material', 'Engine.SC5_emissive_material'}
EMISSIVE_CLASSES = {'Engine.SC5_emissive_material'}

KEY = {k: lead.lead_hash(k) for k in (
    'Diff Map', 'Normal Map', 'Spec Map', 'Displacement Map', 'Diff Transform', 'Normal Transform', 'Spec Transform',
    'Gloss Map Scale', 'Specular Mask Scale', 'Displacement Scale', 'EmissiveScale', 'DiffuseColorHSB',
    'Default properties')}
KEY = {k: '%08X' % v for k, v in KEY.items()}

# ---- material records -----------------------------------------------------------------------------------------
def raw(d, key):
    v = d.get(KEY[key])
    return bytes.fromhex(v['x']) if v and v.get('t') == 'b' else None


def ref(d, key):
    b = raw(d, key)
    if not b or len(b) < 4:
        return None
    r = '%08X' % struct.unpack_from('<I', b)[0]
    return None if r == '00000000' else r


def flt(d, key):
    b = raw(d, key)
    if not b or len(b) != 4:
        return None
    f = struct.unpack('<f', b)[0]
    return f if math.isfinite(f) else None


def hsb_rgb(d, key):
    b = raw(d, key)
    if not b or len(b) != 3:
        return (1.0, 1.0, 1.0)
    return colorsys.hsv_to_rgb(b[0] / 256.0, b[1] / 255.0, b[2] / 255.0)


def load_materials(cats):
    """Unique materials (by hash), each resolved through 'Default properties' like Materials.Apply: the last
    target's data replaces everything. Returns {hash: (name, final_class, data, [catalog groups])}."""
    mats = {}
    for c in cats:
        for m in c.get('materials') or []:
            if m['hash'] in mats:
                mats[m['hash']]['umds'].append(c['umd'])
            else:
                mats[m['hash']] = dict(m, umds=[c['umd']])
    out = {}
    for h, m in mats.items():
        d, cls, seen = m['data'], m['cls'], set()
        while (r := ref(d, 'Default properties')) and r in mats and r not in seen:
            seen.add(r)
            d, cls = mats[r]['data'], mats[r]['cls']
        out[h] = (m['name'], cls, d, m['umds'])
    return out


def setup_of(cls, d):
    """The Remix-relevant part of one material, or None if the pipeline does not handle it."""
    if cls not in PBR_CLASSES:
        return None
    diff = ref(d, 'Diff Map')
    if not diff:
        return None
    same_uv = lambda k: raw(d, k) == raw(d, 'Diff Transform')  # noqa: E731
    gloss, sms = flt(d, 'Gloss Map Scale'), flt(d, 'Specular Mask Scale')
    spec = ref(d, 'Spec Map') if same_uv('Spec Transform') and gloss is not None and sms is not None else None
    disp_map, disp_scale = ref(d, 'Displacement Map'), flt(d, 'Displacement Scale')
    has_disp = bool(disp_map and disp_scale is not None and disp_scale > 0.0)
    emis = flt(d, 'EmissiveScale') if cls in EMISSIVE_CLASSES else None
    emis = emis if emis and emis > 0 else None
    return {
        'diffuse': diff,
        'normal': ref(d, 'Normal Map') if same_uv('Normal Transform') else None,
        'spec': spec,
        'gloss': round(gloss, 4) if spec else None,
        'sms': round(max(0.0, sms), 5) if spec else None,
        'height': disp_map if has_disp else None,
        'displace_in': round(min(max(disp_scale, 0.001), 0.2), 4) if has_disp else None,
        'emissive': round(emis, 4) if emis else None,
        'tint': tuple(round(c, 4) for c in hsb_rgb(d, 'DiffuseColorHSB')) if emis else None,
    }


# ---- textures -------------------------------------------------------------------------------------------------
def index_textures(rows):
    """name -> canonical record (most levels, then archive name) and name -> distinct content variants."""
    by_name = defaultdict(list)
    for r in rows:
        if 'error' not in r:
            by_name[r['name']].append(r)
    canon, variants = {}, {}
    for n, rs in by_name.items():
        rs.sort(key=lambda r: (-len(r['levels']), r['ass'].lower(), r['i']))
        canon[n] = rs[0]
        v = {}
        for r in rs:
            v.setdefault(r['levels'][0]['hash'], r)
        variants[n] = list(v.values())
    return canon, variants


def remix_keys(rec):
    """Texture hashes Remix computes for this record: resident top mip, and streamed top mip if it streams."""
    keys = [(rec['levels'][rec['resident_level']]['hash'], 'resident_top')]
    if rec['resident_level'] > 0:
        keys.append((rec['levels'][0]['hash'], 'stream_top'))
    return keys


def pick_levels(rec, cap):
    k0 = next((k for k, lv in enumerate(rec['levels']) if max(lv['w'], lv['h']) <= cap), len(rec['levels']) - 1)
    return list(range(k0, len(rec['levels'])))


def sig(*parts):
    return '%08X' % (xxhash.xxh3_64_intdigest(json.dumps([PIPELINE_VERSION, *parts]).encode()) & 0xFFFFFFFF)


def normal_to_octahedral(img):
    """Game normal (R = X, G = Y, Z rebuilt like the shader) -> two uint8 channels of unsigned hemi-octahedral."""
    x = img[..., 0].astype(np.float32) / 127.5 - 1.0
    y = img[..., 1].astype(np.float32) / 127.5 - 1.0
    z = np.sqrt(1.0 - np.minimum(x * x + y * y, 1.0))
    s = np.abs(x) + np.abs(y) + z
    s = np.where(s > 0, s, 1.0)
    px, py = x / s, y / s
    u = np.clip(np.rint(((px + py) * 0.5 + 0.5) * 255), 0, 255).astype(np.uint8)
    v = np.clip(np.rint(((px - py) * 0.5 + 0.5) * 255), 0, 255).astype(np.uint8)
    return u, v


def spec_to_rough_metal(img, gloss, sms):
    """Game spec map texels -> (roughness, metallic) in [0, 1], float32.
    Preserves roughness contrast across low-specular cloth/rubber panels (e.g. Sam's suit where G*sms < 0.04
    or artist left the B gloss-mask channel zero) while keeping sms <= 0 fully rough (1.0).
    """
    if sms <= 0.0 or gloss <= 0.0:
        return np.ones(img.shape[:2], dtype=np.float32), np.zeros(img.shape[:2], dtype=np.float32)
    r, g, b = (img[..., i].astype(np.float32) / 255.0 for i in range(3))
    b_eff = np.ones_like(b) if (img.shape[0] * img.shape[1] > 16 and img[..., 2].max() == 0) else b
    base = np.maximum(gloss * b_eff, 1.0)
    with np.errstate(divide='ignore', invalid='ignore'):
        n = np.where(r > 0, np.power(base, r), 1.0)
    n = np.clip(np.nan_to_num(n, nan=1.0, posinf=1e6), 1.0, 1e6)
    rough = np.power(2.0 / (n + 2.0), 0.25)
    inten = g * sms
    vis = MIN_SPEC_VISIBILITY + (1.0 - MIN_SPEC_VISIBILITY) * np.sqrt(np.clip(inten / DIELECTRIC_F0, 0.0, 1.0))
    rough = 1.0 - (1.0 - rough) * vis
    metal = np.clip((inten - METAL_START) / (METAL_FULL - METAL_START), 0.0, 1.0)
    return rough.astype(np.float32), metal.astype(np.float32)

def u8(a):
    return np.clip(np.rint(a * 255), 0, 255).astype(np.uint8)


def job_normal(rd, rec, out):
    levels = pick_levels(rec, NORMAL_MAX)
    data = []
    for k in levels:
        lv = rec['levels'][k]
        img = bc.decode(rd.level(rec, k), lv['w'], lv['h'], rec['fmt'])
        if k == levels[0]:
            xy = img[..., :2].astype(np.float32) / 127.5 - 1.0
            if np.abs(xy).max() < FLAT_NORMAL:
                return {'flat': True}
        data.append(bc.encode_bc5(*normal_to_octahedral(img)))
    top = rec['levels'][levels[0]]
    bc.write_dds(out, top['w'], top['h'], bc.DXGI['BC5'], data)
    return {'file': out.name}


def job_spec(rd, rec, gloss, sms, out_r, out_m):
    levels = pick_levels(rec, ROUGH_MAX)
    rl, ml = [], []
    res = {}
    for k in levels:
        lv = rec['levels'][k]
        rough, metal = spec_to_rough_metal(bc.decode(rd.level(rec, k), lv['w'], lv['h'], rec['fmt']), gloss, sms)
        r8, m8 = u8(rough), u8(metal)
        if k == levels[0]:
            res['rough_mean'] = float(rough.mean())
            res['metal_mean'] = float(metal.mean())
            r_flat = int(r8.max()) - int(r8.min()) <= UNIFORM_TOLERANCE
            m_flat = int(m8.max()) - int(m8.min()) <= UNIFORM_TOLERANCE
        rl.append(bc.encode_bc4(r8))
        ml.append(bc.encode_bc4(m8))
    top = rec['levels'][levels[0]]
    if r_flat:
        res['rough_const'] = round(res['rough_mean'], 4)
    else:
        bc.write_dds(out_r, top['w'], top['h'], bc.DXGI['BC4'], rl)
        res['rough_file'] = out_r.name
    if m_flat:
        res['metal_const'] = round(res['metal_mean'], 4)
    else:
        bc.write_dds(out_m, top['w'], top['h'], bc.DXGI['BC4'], ml)
        res['metal_file'] = out_m.name
    return res


def job_emissive(rd, rec, tint, out):
    if rec['fmt'] not in (4, 6):  # only BC1/BC3 blocks can be copied and tinted without a colour encoder
        lv = rec['levels'][-1]
        img = bc.decode(rd.level(rec, len(rec['levels']) - 1), lv['w'], lv['h'], rec['fmt'])[..., :3]
        mean = (img.astype(np.float32) / 255.0) ** 2.2
        return {'color_const': [round(float(mean[..., i].mean()) * tint[i], 4) for i in range(3)]}
    srgb_tint = [t ** (1 / 2.2) for t in tint]  # the mask is sampled as sRGB, the game tints in linear
    levels = pick_levels(rec, EMISSIVE_MAX)
    data = [bc.tint_color_blocks(rd.level(rec, k), rec['fmt'], srgb_tint) for k in levels]
    top = rec['levels'][levels[0]]
    bc.write_dds(out, top['w'], top['h'], bc.DXGI['BC1_SRGB' if rec['fmt'] == 4 else 'BC3_SRGB'], data)
    return {'file': out.name}


def job_height(rd, rec, out):
    levels = pick_levels(rec, HEIGHT_MAX)
    data = []
    for k in levels:
        lv = rec['levels'][k]
        h_chan = bc.decode(rd.level(rec, k), lv['w'], lv['h'], rec['fmt'])[..., 0]
        if k == levels[0] and int(h_chan.max()) - int(h_chan.min()) <= UNIFORM_TOLERANCE:
            return {'flat': True}
        data.append(bc.encode_bc4(h_chan))
    top = rec['levels'][levels[0]]
    bc.write_dds(out, top['w'], top['h'], bc.DXGI['BC4'], data)
    return {'file': out.name}


def run_jobs(jobs):
    """jobs: list of (kind, key, args). Runs in worker processes, one archive reader per process."""
    rd = lead.LevelReader(UMD_DIR)
    out = []
    try:
        for kind, key, args in jobs:
            try:
                if kind == 'N':
                    out.append((kind, key, job_normal(rd, *args)))
                elif kind == 'S':
                    out.append((kind, key, job_spec(rd, *args)))
                elif kind == 'H':
                    out.append((kind, key, job_height(rd, *args)))
                else:
                    out.append((kind, key, job_emissive(rd, *args)))
            except Exception as e:  # recorded in the manifest; the override simply goes without this input
                out.append((kind, key, {'error': '%s: %s' % (type(e).__name__, e)}))
    finally:
        rd.close()
    return out


def majority(counter):
    """The value more than half of the votes agree on, else None."""
    (v, n), total = counter.most_common(1)[0], sum(counter.values())
    return v if n * 2 > total else None


def weighted_median(pairs):
    pairs = sorted(pairs)
    half, acc = sum(n for _, n in pairs) / 2.0, 0
    for v, n in pairs:
        acc += n
        if acc >= half:
            return v


def choose_inputs(setups, disagreements):
    """setups: [(setup, material count)] voting for one hash -> the setup written for it."""
    normal = majority(_count(setups, 'normal'))
    spec = majority(_count(setups, 'spec'))
    height = majority(_count(setups, 'height'))
    glow = majority(_count(setups, 'glow'))
    out = {'diffuse': majority(_count(setups, 'diffuse')) or sorted(s['diffuse'] for s, _ in setups)[0],
           'normal': normal, 'spec': spec, 'gloss': None, 'sms': None,
           'height': height, 'displace_in': None,
           'emissive': glow[0] if glow else None, 'tint': list(glow[1]) if glow else None}
    if spec:
        same = [(s, n) for s, n in setups if s['spec'] == spec]
        out['gloss'] = weighted_median([(s['gloss'], n) for s, n in same])
        out['sms'] = weighted_median([(s['sms'], n) for s, n in same])
    if height:
        same_h = [(s, n) for s, n in setups if s.get('height') == height and s.get('displace_in')]
        out['displace_in'] = weighted_median([(s['displace_in'], n) for s, n in same_h]) if same_h else 0.03
    for part, value in (('normal', normal), ('spec', spec), ('height', height), ('glow', glow)):
        if len(_count(setups, part)) > 1:
            disagreements['%s: %s' % (part, 'majority used' if value is not None else 'no majority, input left out')] += 1
    return out


def _count(setups, part):
    c = Counter()
    for s, n in setups:
        v = (s['emissive'], tuple(s['tint'])) if part == 'glow' and s['emissive'] else (None if part == 'glow' else s[part])
        c[v] += n
    return c


# ---- caches ---------------------------------------------------------------------------------------------------
def ensure_caches(rescan):
    CACHE.mkdir(exist_ok=True)
    tex, cat = CACHE / 'textures.json', CACHE / 'catalogs.json'
    if rescan or not tex.exists():
        print('scanning archives (slow)...', flush=True)
        subprocess.run([sys.executable, str(SCRIPTS / 'scan_textures.py'), str(UMD_DIR), str(tex)], check=True)
    if rescan or not cat.exists():
        print('dumping material catalogs (slow)...', flush=True)
        subprocess.run(['pwsh', '-NoProfile', '-File', str(SCRIPTS / 'dump_catalogs.ps1'), '-UmdDir', str(UMD_DIR),
                        '-OutJson', str(cat)], check=True)
    return json.loads(tex.read_text('utf-8')), json.loads(cat.read_text('utf-8'))


# ---- main -----------------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--rescan', action='store_true', help='re-read the game archives instead of using the cache')
    ap.add_argument('--jobs', type=int, default=os.cpu_count())
    args = ap.parse_args()

    rows, cats = ensure_caches(args.rescan)
    canon, variants = index_textures(rows)
    mats = load_materials(cats)

    # 1. every handled material votes on the inputs of each Remix hash of its diffuse texture
    votes = defaultdict(Counter)        # remix hash -> Counter(setup json)
    hash_info = {}                       # remix hash -> (diffuse name, role, record)
    groups_of = defaultdict(set)         # remix hash -> catalog groups whose materials use it
    skipped = Counter()
    for h, (name, cls, d, umds) in mats.items():
        s = setup_of(cls, d)
        if s is None:
            skipped['class not handled' if cls not in PBR_CLASSES else 'no diffuse map'] += 1
            continue
        if s['diffuse'] not in variants:
            skipped['diffuse texture not in any .ass'] += 1
            continue
        if max(canon[s['diffuse']]['levels'][0]['w'], canon[s['diffuse']]['levels'][0]['h']) < MIN_DIFFUSE_SIDE:
            skipped['diffuse is a tiny shared placeholder'] += 1
            continue
        for part in ('normal', 'spec', 'height'):
            if s.get(part) and s[part] not in canon:
                s[part] = None
        if not s['spec']:
            s['gloss'] = s['sms'] = None
        if not s.get('height'):
            s['displace_in'] = None
        key = json.dumps(s, sort_keys=True)
        for rec in variants[s['diffuse']]:
            for kh, role in remix_keys(rec):
                votes[kh][key] += 1
                groups_of[kh].update(umds)
                hash_info.setdefault(kh, (s['diffuse'], role, rec))

    # 2. one value per input and hash. Materials sharing a hash (same pixels, maybe different texture names) can
    # disagree; an input is only set when more than half of them agree on it, so a shared texture never gets a
    # bump or glow that belongs to a different object.
    # The sharp and blurry hash of one texture are decided together: hashes are linked when they belong to the
    # same archive record, and every material voting on any hash of a linked group votes for the whole group.
    parent = {kh: kh for kh in votes}

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for rec in (r for vs in variants.values() for r in vs):
        ks = [kh for kh, _ in remix_keys(rec) if kh in parent]
        for a, b in zip(ks, ks[1:]):
            parent[root(a)] = root(b)
    group_votes = defaultdict(Counter)
    for kh, c in votes.items():
        group_votes[root(kh)].update(c)
    chosen, disagreements, decided = {}, Counter(), {}
    for kh in votes:
        g = root(kh)
        if g not in decided:
            decided[g] = choose_inputs([(json.loads(k), n) for k, n in group_votes[g].items()], disagreements)
        chosen[kh] = decided[g]

    # 3. texture jobs, content-addressed so reruns reuse unchanged files
    TEXTURES.mkdir(parents=True, exist_ok=True)
    jobs, job_for = {}, {}
    for kh, s in chosen.items():
        want = []
        if s['normal']:
            rec = canon[s['normal']]
            eff_cap = 1024 if max(rec['levels'][0]['w'], rec['levels'][0]['h']) <= 1024 else NORMAL_MAX
            jk = ('N', s['normal'])
            jobs.setdefault(jk, (rec['ass'], ('N', jk, (rec, TEXTURES / ('N_%s_%s.dds' % (s['normal'], sig(rec['levels'][0]['hash'], eff_cap)))))))
            want.append(jk)
        if s['spec']:
            rec = canon[s['spec']]
            p = sig(rec['levels'][0]['hash'], s['gloss'], s['sms'], ROUGH_MAX, DIELECTRIC_F0, MIN_SPEC_VISIBILITY, METAL_START, METAL_FULL)
            jk = ('S', s['spec'], s['gloss'], s['sms'])
            jobs.setdefault(jk, (rec['ass'], ('S', jk, (rec, s['gloss'], s['sms'], TEXTURES / ('R_%s_%s.dds' % (s['spec'], p)),
                                                        TEXTURES / ('M_%s_%s.dds' % (s['spec'], p))))))
            want.append(jk)
        if s.get('height'):
            rec = canon[s['height']]
            jk = ('H', s['height'])
            jobs.setdefault(jk, (rec['ass'], ('H', jk, (rec, TEXTURES / ('H_%s_%s.dds' % (s['height'], sig(rec['levels'][0]['hash'], HEIGHT_MAX)))))))
            want.append(jk)
        if s['emissive']:
            rec = hash_info[kh][2]
            eff_ecap = 512 if max(rec['levels'][0]['w'], rec['levels'][0]['h']) <= 512 else EMISSIVE_MAX
            jk = ('E', rec['levels'][0]['hash'], tuple(s['tint']))
            jobs.setdefault(jk, (rec['ass'], ('E', jk, (rec, s['tint'], TEXTURES / ('E_%s_%s.dds' % (rec['levels'][0]['hash'], sig(s['tint'], eff_ecap)))))))
            want.append(jk)
        job_for[kh] = want

    # reuse results of jobs whose content-addressed outputs already exist
    job_cache_path = CACHE / 'texture_jobs.json'
    job_cache = json.loads(job_cache_path.read_text('utf-8')) if job_cache_path.exists() else {}
    results, todo = {}, defaultdict(list)
    for jk, (ass, job) in jobs.items():
        names = [a.name for a in job[2] if isinstance(a, Path)]
        hit = job_cache.get(repr(jk))
        if hit and hit['names'] == names and isinstance(hit['res'], dict) and 'error' not in hit['res'] and all(
                (TEXTURES / hit['res'][f]).exists() for f in ('file', 'rough_file', 'metal_file') if f in hit['res']):
            results[jk] = hit['res']
        else:
            todo[ass].append(job)
    batches = [b[i:i + 40] for b in todo.values() for i in range(0, len(b), 40)]
    batches.sort(key=len, reverse=True)
    print('texture jobs: %d (%d reused) in %d archive batches' % (len(jobs), len(results), len(batches)), flush=True)
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        for done, part in enumerate(ex.map(run_jobs, batches), 1):
            for kind, key, res in part:
                results[key] = res
            if done % 5 == 0:
                print('  batches done %d/%d' % (done, len(batches)), flush=True)
    job_cache_path.write_text(json.dumps({repr(jk): {'names': [a.name for a in jobs[jk][1][2] if isinstance(a, Path)],
                                                     'res': results[jk]} for jk in jobs}), 'utf-8')

    # 4. mod.usda
    overrides, written = [], set()
    errors = [{'job': list(map(str, k)), 'error': r['error']} for k, r in results.items() if 'error' in r]
    lines = ['#usda 1.0', '(', '    customLayerData = {', '        string lightspeed_game_name = "Blacklist"',
             '        string lightspeed_layer_type = "replacement"', '    }',
             '    doc = "Generated by play-setup/glow/scripts/build_materials.py. Do not edit by hand."',
             '    timeCodesPerSecond = 24', '    upAxis = "Z"', ')', '', 'over "RootNode"', '{', '    over "Looks"', '    {']
    for kh in sorted(chosen):
        s = chosen[kh]
        body = []
        for jk in job_for[kh]:
            r = results.get(jk, {})
            if jk[0] == 'N' and r.get('file'):
                body.append(('asset', 'normalmap_texture', r['file'], 'raw'))
            elif jk[0] == 'S' and 'error' not in r:
                if 'rough_file' in r:
                    body.append(('asset', 'reflectionroughness_texture', r['rough_file'], 'raw'))
                else:
                    body.append(('float', 'reflection_roughness_constant', r['rough_const']))
                if 'metal_file' in r:
                    body.append(('asset', 'metallic_texture', r['metal_file'], 'raw'))
                elif r['metal_const'] > 0:
                    body.append(('float', 'metallic_constant', r['metal_const']))
            elif jk[0] == 'H' and r.get('file') and s.get('displace_in'):
                body.append(('asset', 'height_texture', r['file'], 'raw'))
                body.append(('float', 'displace_in', s['displace_in']))
            elif jk[0] == 'E' and 'error' not in r:
                body.append(('bool', 'enable_emission', 1))
                body.append(('float', 'emissive_intensity', round(min(s['emissive'] * EMISSIVE_K, EMISSIVE_MAX_INTENSITY), 4)))
                if r.get('file'):
                    body.append(('asset', 'emissive_mask_texture', r['file'], 'auto'))
                else:
                    body.append(('color3f', 'emissive_color_constant', r['color_const']))
        if not body:
            continue
        name, role, rec = hash_info[kh]
        lines += ['        over "mat_%s"' % kh, '        {', '            over Shader "Shader"', '            {']
        for b in body:
            if b[0] == 'asset':
                written.add(b[2])
                lines += ['                asset inputs:%s = @./textures/%s@ (' % (b[1], b[2]),
                          '                    colorSpace = "%s"' % b[3], '                )']
            elif b[0] == 'color3f':
                lines.append('                color3f inputs:%s = (%s)' % (b[1], ', '.join('%g' % v for v in b[2])))
            else:
                lines.append('                %s inputs:%s = %s' % (b[0], b[1], ('%g' % b[2]) if b[0] == 'float' else b[2]))
        lines += ['            }', '        }']
        overrides.append({'hash': kh, 'diffuse': name, 'role': role, 'ass': rec['ass'],
                          'size': [rec['levels'][0]['w'], rec['levels'][0]['h']],
                          'inputs': sorted(b[1] for b in body), 'setup': s, 'materials': sum(group_votes[root(kh)].values())})
    lines += ['    }', '}', '']
    (MATERIALS / 'mod.usda').write_text('\n'.join(lines), 'utf-8', newline='\n')

    # 5. remove stale generated files (everything in textures/ this run did not reference). The old separate glow
    # pack (glow/mod) is replaced by the emissive inputs above; it stays as an empty layer only because the launcher
    # still requires glow/mod/mod.usda to exist.
    stale = [p for p in TEXTURES.iterdir() if p.is_file() and p.name not in written]
    old_glow = GLOW / 'mod'
    if old_glow.exists():
        stale += [p for p in old_glow.rglob('*') if p.is_file() and p.name != 'mod.usda']
    locked = []
    for p in stale:
        for attempt in range(10):  # another process (e.g. a test reading headers) may hold the file briefly
            try:
                p.unlink()
                break
            except PermissionError:
                time.sleep(0.5)
        else:
            locked.append(p.name)
    if locked:
        print('WARNING: %d stale files are locked by another process and were left, e.g. %s' % (len(locked), locked[0]))
    if (old_glow / 'textures').exists():
        (old_glow / 'textures').rmdir()
    old_glow.mkdir(exist_ok=True)
    (old_glow / 'mod.usda').write_text(EMPTY_GLOW_LAYER, 'utf-8', newline='\n')

    # 6. VRAM: bytes of distinct files per catalog group (+ the always-loaded groups)
    size = {f: (TEXTURES / f).stat().st_size - 148 for f in written}
    files_by_group = defaultdict(set)
    for o in overrides:
        files = {r.get(x) for jk in job_for[o['hash']] for r in [results.get(jk, {})] for x in ('file', 'rough_file', 'metal_file')}
        for g in groups_of[o['hash']]:
            files_by_group[g] |= {f for f in files if f}
    common = set().union(*(files_by_group.get(g, set()) for g in ALWAYS_LOADED))
    per_level = {g: sum(size[f] for f in fs | common) for g, fs in files_by_group.items()}
    worst = max(per_level.items(), key=lambda kv: kv[1]) if per_level else ('', 0)

    texture_hashes = sorted({(kh, rec['name'], role, rec['levels'][0 if role == 'stream_top' else rec['resident_level']]['w'],
                              rec['levels'][0 if role == 'stream_top' else rec['resident_level']]['h'], rec['fmt'])
                             for rec in rows if 'error' not in rec for kh, role in remix_keys(rec)})
    with open(GLOW / 'texture_hashes.csv', 'w', newline='\n', encoding='utf-8') as f:
        f.write('hash,texture_name,role,width,height,format\n')
        for t in texture_hashes:
            f.write('%s,%s,%s,%d,%d,%d\n' % t)

    # 7. Export all game-wide decal diffuse texture hashes (bullet holes, blood, signs, scorches) offline so
    # rtx.conf can pre-populate rtx.decalTextures with zero runtime CPU LockRect or config re-parsing.
    decal_tids = set()
    for c in cats:
        for th, tname in (c.get('textures') or {}).items():
            pkg = tname.split('.')[0].lower()
            leaf = tname.split('.')[-1].lower()
            if ('decal' in pkg or 'decal' in leaf or 'bullethole' in leaf) and not leaf.endswith(('_n_l3d', '_s_l3d', '_h_l3d', '_m_l3d', '_n', '_s')):
                decal_tids.add(th)
    decal_hashes = sorted({
        '0x' + kh
        for tid in decal_tids
        for rec in variants.get(tid, ())
        if max(rec['levels'][0]['w'], rec['levels'][0]['h']) >= MIN_DIFFUSE_SIDE
        for kh, _ in remix_keys(rec)
        if kh not in POISON_HASHES
    })
    (GLOW / 'decal_hashes.txt').write_text(', '.join(decal_hashes) + '\n', 'utf-8', newline='\n')

    manifest = {
        'generator': 'play-setup/glow/scripts/build_materials.py', 'pipeline_version': PIPELINE_VERSION,
        'tuning': {k: globals()[k] for k in ('NORMAL_MAX', 'ROUGH_MAX', 'EMISSIVE_MAX', 'HEIGHT_MAX', 'MIN_DIFFUSE_SIDE',
                                               'DIELECTRIC_F0', 'MIN_SPEC_VISIBILITY', 'METAL_START', 'METAL_FULL',
                                               'EMISSIVE_K', 'EMISSIVE_MAX_INTENSITY', 'UNIFORM_TOLERANCE', 'VRAM_BUDGET')},
        'counts': {'materials': len(mats), 'skipped_materials': dict(skipped), 'hashes_voted': len(votes),
                   'input_disagreements': dict(disagreements), 'overrides': len(overrides), 'files': len(written),
                   'decal_hashes': len(decal_hashes), 'stale_files_deleted': len(stale), 'texture_job_errors': len(errors)},
        'vram': {'total_bytes': sum(size.values()), 'budget_bytes': VRAM_BUDGET, 'worst_level': worst[0],
                 'worst_level_bytes': worst[1], 'always_loaded_bytes': sum(size[f] for f in common),
                 'per_level_bytes': dict(sorted(per_level.items()))},
        'errors': errors, 'overrides': overrides,
    }
    # one override per line keeps the file small and its git diffs readable
    parts = ['%s: %s' % (json.dumps(k), json.dumps(v, indent=1)) for k, v in manifest.items() if k != 'overrides']
    nl = '\n'
    parts.append('"overrides": [' + nl + (',' + nl).join(json.dumps(o, sort_keys=True) for o in overrides) + nl + ']')
    (MATERIALS / 'manifest.json').write_text('{' + nl + (',' + nl).join(parts) + nl + '}' + nl, 'utf-8', newline=nl)
    print(json.dumps({'counts': manifest['counts'], 'vram_total_MB': round(sum(size.values()) / 2 ** 20, 1),
                      'worst_level': worst[0], 'worst_level_MB': round(worst[1] / 2 ** 20, 1)}, indent=1))


if __name__ == '__main__':
    main()
