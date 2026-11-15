"""Verify Blacklist's lights as Remix gets them, and the Remix USD material assets, offline.

Lights: the conversion formula lives only in play-setup/unified-build/light_calibration.h. The C++ replay
(tests/verify_proxy_tests.exe) runs it on every recorded LightNode dump in tools/remix/fixtures/ and writes
tests/lightcal_published.tsv. This script checks that output against the game's own math, re-derived here from
the game data (the light shader's falloff, and the SH probes decoded from the dumps' raw memory words):
- fixture lights have a 20-35 cm emitter and are at most 1.5x the game's brightness at 0.5 m;
- every room probe's fill gives a surface at the probe at least the game's average SH value from its bright side,
  without exceeding the game's own value from that side by more than the same cap;
- every level with probes gets a sky dome equal to its level-wide probe's average.

Materials: all mod.usda overrides reference valid 64-bit XXH3 hashes and intact DDS textures on disk.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REMIX_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REMIX_ROOT / "fixtures"
MATERIALS_DIR = REMIX_ROOT / "materials"
GLOW_MOD_DIR = REMIX_ROOT / "play-setup" / "glow" / "mod"
CALIBRATION_H = REMIX_ROOT / "play-setup" / "unified-build" / "light_calibration.h"
PUBLISHED_TSV = REMIX_ROOT / "tests" / "lightcal_published.tsv"

# Shared 8x8 white fallback texture used by 210 unrelated materials (must never be overridden)
POISON_TEXTURE_HASHES = {"2164293A60D148AC"}

# Acceptance limits: close range = 0.5 m (blocks legacy 9x-440x blowout), median 1.8 m room brightness >= 0.50x game,
# every mission with probes >= 3 room fills at >= 0.90x game SH mean, and spotlights cross-checked against 2 witnesses.
CLOSE_DISTANCE_CM = 50.0
ROOM_DISTANCE_CM = 180.0
MAX_CLOSE_RATIO_HARD_LIMIT = 5.50
MAX_PROBE_TOWARD_RATIO_LIMIT = 1.50
MIN_MEDIAN_ROOM_RATIO = 0.50
MIN_FILL_FRACTION_OF_GAME_MEAN = 0.90
MAX_WEAPON_FLARE_DIST_CM = 15.0
MAX_SPOT_SHADER_POS_ERR_CM = 1.0
LUMINANCE = (0.2126, 0.7152, 0.0722)
KIND_SPHERE, KIND_DISTANT, KIND_DOME = 1, 2, 3


def read_calibration_constants(path: Path = CALIBRATION_H) -> dict[str, float]:
    """Read the `constexpr float kName = value;` constants of light_calibration.h (no copies kept here)."""
    text = path.read_text(encoding="utf-8")
    consts = {
        m.group(1): float(m.group(2).rstrip("f"))
        for m in re.finditer(r"constexpr\s+float\s+(k\w+)\s*=\s*([-0-9.eE]+f?)\s*;", text)
    }
    for name in ("kMinFixtureRadiusCm", "kMaxFixtureRadiusCm", "kRoomFillMaxZoneRadiusCm"):
        if name not in consts:
            raise SystemExit(f"FAIL: {path.name} has no {name}")
    return consts


def game_falloff(range_cm: float, falloff_start_cm: float, distance_cm: float) -> float:
    """Blacklist's light shader attenuation: (1 - t^2)^2, t = (d - falloffStart) / (range - falloffStart)."""
    span = range_cm - falloff_start_cm
    t = (distance_cm - falloff_start_cm) / span if span > 1e-3 else 1.0
    t = max(0.0, min(1.0, t))
    return (1.0 - t * t) ** 2


def legacy_unbounded_radiance(range_cm: float, falloff_start_cm: float, peak: float) -> tuple[float, float]:
    """The old lightpub.cpp formula (14 cm sphere matched at half range), used to prove the check catches it."""
    radius = 14.0
    reach = range_cm * 0.5
    return radius, peak * game_falloff(range_cm, falloff_start_cm, reach) * (reach / radius) ** 2


def lum(rgb: tuple[float, float, float]) -> float:
    return sum(w * c for w, c in zip(LUMINANCE, rgb))


def floats(text: str) -> tuple[float, ...]:
    return tuple(float(x) for x in text.split(",")) if text else ()


@dataclass(frozen=True)
class Published:
    """One row of lightcal_published.tsv (a game light as lightpub publishes it)."""

    dump: str
    node: str
    light_type: int
    range_cm: float
    falloff_start_cm: float
    peak: float
    kind: int
    radius: float
    radiance: tuple[float, ...]
    position: tuple[float, ...]
    node_position: tuple[float, ...]
    probe_radius: float
    sh_mean: tuple[float, ...]
    sh_toward: float
    fill_distance: float
    direction: tuple[float, ...] = (0.0, 0.0, -1.0)
    cone_angle: float = 0.0
    near_size: tuple[float, ...] = (0.0, 0.0)
    near_distance: float = 0.0
    weapon_spot: int = 0


def load_published(path: Path = PUBLISHED_TSV) -> list[Published]:
    rows: list[Published] = []
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        p = line.split("\t")
        rows.append(
            Published(p[0], p[1], int(p[2]), float(p[3]), float(p[4]), float(p[5]), int(p[6]), float(p[7]),
                      floats(p[8]), floats(p[9]), floats(p[10]), float(p[11]), floats(p[12]), float(p[13]),
                      float(p[14]), floats(p[15]) if len(p) > 15 else (0.0, 0.0, -1.0),
                      float(p[16]) if len(p) > 16 else 0.0, floats(p[17]) if len(p) > 17 else (0.0, 0.0),
                      float(p[18]) if len(p) > 18 else 0.0, int(p[19]) if len(p) > 19 else 0)
        )
    return rows

def decode_probes(dump: Path) -> list[tuple[float, tuple[float, float, float]]]:
    """(radius, SH average RGB) of every engine probe in a dump, decoded from its raw words.

    Engine layout (FUN_018c3d80, vs 87D8E2DF): flags & 7 == 4 with flag 0x400; radius at +0x440; 7 float4 at +0x230
    in the order cAr, cAg, cAb, cBr, cBg, cBb, cC. Average over all directions = cA.w + cB.z / 3.
    """
    probes = []
    for line in dump.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split("\t")
        if not parts or parts[0] != "light":
            continue
        kv = dict(item.split("=", 1) for item in parts[1:] if "=" in item)
        flags = int(kv.get("flags", "0"), 16)
        if flags & 7 != 4 or not flags & 0x400:
            continue
        raw20 = [int(w, 16) for w in kv.get("raw20", "").split()]
        raw390 = [int(w, 16) for w in kv.get("raw390", "").split()]
        if len(raw20) < (0x2A0 - 0x20) // 4 or len(raw390) <= (0x440 - 0x390) // 4:
            continue  # oldest dump format (run1): no probe words

        def f32(words: list[int], base: int, offset: int) -> float:
            return struct.unpack("<f", struct.pack("<I", words[(offset - base) // 4]))[0]

        sh = [[f32(raw20, 0x20, 0x230 + 16 * r + 4 * c) for c in range(4)] for r in range(7)]
        mean = tuple(max(0.0, sh[ch][3] + sh[3 + ch][2] / 3.0) for ch in range(3))
        probes.append((f32(raw390, 0x390, 0x440), mean))
    return probes


def _dist3(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a[:3], b[:3])) ** 0.5


def check_published(rows: list[Published], consts: dict[str, float]) -> tuple[dict[str, int], list[str]]:
    """Check every published light against the game's own brightness, room reach, and probe fill."""
    errors: list[str] = []
    counts = {"fixtures": 0, "weapon_spots": 0, "fills": 0, "domes": 0, "capped": 0}
    r_min, r_max = consts["kMinFixtureRadiusCm"], consts["kMaxFixtureRadiusCm"]
    w_rad = consts["kWeaponSpotRadiusCm"]
    room_ratios_by_dump: dict[str, list[float]] = {}
    fills_by_dump: dict[str, int] = {}
    dumps_with_probes: set[str] = set()

    for row in rows:
        where = f"{row.dump} node={row.node}"
        if row.weapon_spot:
            counts["weapon_spots"] += 1
            if abs(row.radius - w_rad) > 0.5 or row.radius > 5.0:
                errors.append(f"{where}: weapon spotlight emitter radius {row.radius:g} cm (expected tight ~{w_rad:g} cm lens)")
            if not (5.0 <= row.cone_angle <= 15.0):
                errors.append(f"{where}: weapon spotlight half-angle {row.cone_angle:.2f} deg outside 5-15 deg (floodlight regression)")
            reach_4m = max(row.radiance) * (row.radius / 400.0) ** 2 if row.peak > 1e-4 else 0.0
            if reach_4m < 0.25 * row.peak:
                errors.append(f"{where}: weapon spotlight too dim at 4 m ({reach_4m:.3g} vs game peak {row.peak:.3g})")
            if _dist3(row.position, row.node_position) > 1.0:
                errors.append(f"{where}: weapon spotlight shifted {_dist3(row.position, row.node_position):.1f} cm from LightNode.position")
        elif row.light_type in (1, 2) and row.peak > 1e-4:
            counts["fixtures"] += 1
            if not (r_min - 1e-3 <= row.radius <= r_max + 1e-3):
                errors.append(f"{where}: emitter radius {row.radius:g} cm outside {r_min:g}-{r_max:g} cm")
            if row.range_cm >= 2 * CLOSE_DISTANCE_CM and row.range_cm < 1.0e5:
                game = game_falloff(row.range_cm, row.falloff_start_cm, CLOSE_DISTANCE_CM)
                remix = max(row.radiance) / row.peak * (row.radius / max(CLOSE_DISTANCE_CM, row.radius)) ** 2
                if game > 1e-4:
                    ratio = remix / game
                    counts["capped"] += ratio > 1.3
                    if ratio > MAX_CLOSE_RATIO_HARD_LIMIT:
                        errors.append(f"{where} range={row.range_cm:g}cm: 0.5 m brightness {ratio:.2f}x the game "
                                      f"(limit {MAX_CLOSE_RATIO_HARD_LIMIT:.2f}x)")
                game_room = game_falloff(row.range_cm, row.falloff_start_cm, ROOM_DISTANCE_CM)
                if game_room > 1e-3:
                    remix_room = max(row.radiance) / row.peak * (row.radius / max(ROOM_DISTANCE_CM, row.radius)) ** 2
                    room_ratios_by_dump.setdefault(row.dump, []).append(remix_room / game_room)
        elif row.light_type == 4 and row.kind == KIND_SPHERE:
            counts["fills"] += 1
            fills_by_dump[row.dump] = fills_by_dump.get(row.dump, 0) + 1
            dumps_with_probes.add(row.dump)
            if row.probe_radius > consts["kRoomFillMaxZoneRadiusCm"]:
                errors.append(f"{where}: level-wide probe ({row.probe_radius:g} cm) published as a room fill")
            facing = lum(row.radiance) * (row.radius / row.fill_distance) ** 2 if row.fill_distance > 0 else 0.0
            game_mean = lum(row.sh_mean)
            if facing < MIN_FILL_FRACTION_OF_GAME_MEAN * game_mean:
                errors.append(f"{where}: fill gives {facing:.4g} at the probe, game average {game_mean:.4g} "
                              f"(characters there would be darker than the game)")
            if row.sh_toward > 0 and facing > MAX_PROBE_TOWARD_RATIO_LIMIT * row.sh_toward:
                errors.append(f"{where}: fill gives {facing:.4g}, more than {MAX_PROBE_TOWARD_RATIO_LIMIT}x the "
                              f"game's {row.sh_toward:.4g} from that side")
            if row.fill_distance - row.radius < 49.5:
                errors.append(f"{where}: fill emitter reaches within 50 cm of the probe centre (inside a character)")
        elif row.kind == KIND_DOME:
            counts["domes"] += 1
            dumps_with_probes.add(row.dump)

    for dump_name in sorted(dumps_with_probes):
        if fills_by_dump.get(dump_name, 0) < 3:
            errors.append(f"{dump_name}: only {fills_by_dump.get(dump_name, 0)} room fill lights published (need >= 3 indoor ambient zones)")
    for dump_name, ratios in sorted(room_ratios_by_dump.items()):
        s_ratios = sorted(ratios)
        median_ratio = s_ratios[len(s_ratios) // 2]
        if median_ratio < MIN_MEDIAN_ROOM_RATIO:
            errors.append(f"{dump_name}: median 1.8 m lamp brightness is {median_ratio:.2f}x the game "
                          f"(minimum {MIN_MEDIAN_ROOM_RATIO:.2f}x)")
    return counts, errors


def check_spotlight_witnesses(rows: list[Published], fixtures_root: Path) -> tuple[dict[str, int], list[str]]:
    """Two-witness cross-check of published spotlights against independent engine records:
      1) FlareNode bulb positions in *_lights.tsv (weapon flashlights sit ~9.07 cm in front of their rail FlareNode).
      2) Deferred spotlight shader constants cSpot0 (c64.xyz = position, c64.w = range) in paired *_draws.tsv.
    """
    errors: list[str] = []
    by_dump: dict[str, list[Published]] = {}
    for r in rows:
        if r.light_type == 2:
            by_dump.setdefault(r.dump, []).append(r)
    flare_pairs = 0
    shader_pairs = 0
    for dump_path in sorted(fixtures_root.glob("run*/*_lights.tsv")):
        label = f"{dump_path.parent.name}/{dump_path.name}"
        spots = by_dump.get(label, [])
        if not spots:
            continue
        flares: list[tuple[float, ...]] = []
        for line in dump_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("flare\t"):
                kv = dict(item.split("=", 1) for item in line.split("\t")[1:] if "=" in item)
                if "pos" in kv:
                    flares.append(floats(kv["pos"]))
        for s in spots:
            if s.weapon_spot and flares:
                nearest = min(_dist3(s.position, fp) for fp in flares)
                flare_pairs += 1
                if nearest > MAX_WEAPON_FLARE_DIST_CM:
                    errors.append(
                        f"{label} node={s.node}: weapon spotlight position {s.position} is {nearest:.1f} cm from "
                        f"nearest FlareNode attachment (limit {MAX_WEAPON_FLARE_DIST_CM:g} cm; detached flashlight bug)"
                    )
        draws_path = dump_path.with_name(dump_path.name.replace("_lights.tsv", "_draws.tsv"))
        if draws_path.is_file():
            for line in draws_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if ("\tps=679A8A28\t" in line or "\tps=EA2B0617\t" in line) and "\tpsc16-167=" in line:
                    psc_hi = [float(x) for x in line.split("\tpsc16-167=", 1)[1].split("\t", 1)[0].split(",")]
                    off = (64 - 16) * 4
                    if len(psc_hi) >= off + 4:
                        c64_pos = (psc_hi[off], psc_hi[off + 1], psc_hi[off + 2])
                        c64_range = psc_hi[off + 3]
                        node_matches = [s for s in spots if _dist3(s.node_position, c64_pos) <= 2.0]
                        for m in node_matches:
                            shader_pairs += 1
                            err = _dist3(m.position, c64_pos)
                            if err > MAX_SPOT_SHADER_POS_ERR_CM:
                                errors.append(
                                    f"{label} node={m.node}: published spotlight {m.position} shifted {err:.1f} cm "
                                    f"from shader cSpot0 c64.xyz {c64_pos}"
                                )
                            if abs(m.range_cm - c64_range) > 1.0:
                                errors.append(
                                    f"{label} node={m.node}: range {m.range_cm:g} disagrees with shader c64.w {c64_range:g}"
                                )
    if flare_pairs == 0 or shader_pairs == 0:
        errors.append(f"spotlight witness check ran too few pairs (flare_pairs={flare_pairs}, shader_pairs={shader_pairs})")
    return {"flare_pairs": flare_pairs, "shader_pairs": shader_pairs}, errors

def check_domes(rows: list[Published], fixtures_root: Path, consts: dict[str, float]) -> list[str]:
    """Each dump with probes gets a dome equal to its widest level-wide probe's average (decoded independently)."""
    errors: list[str] = []
    domes = {r.dump: r for r in rows if r.kind == KIND_DOME}
    for dump in sorted(fixtures_root.glob("run*/*_lights.tsv")):
        label = f"{dump.parent.name}/{dump.name}"
        wide = [p for p in decode_probes(dump) if p[0] > consts["kRoomFillMaxZoneRadiusCm"]]
        if not wide:
            continue
        expected = max(wide, key=lambda p: p[0])[1]
        dome = domes.get(label)
        if dome is None:
            if max(expected) > 1e-6:
                errors.append(f"{label}: level-wide probe but no sky dome published")
            continue
        if any(abs(a - b) > 1e-3 * max(1e-4, b) for a, b in zip(dome.radiance, expected)):
            errors.append(f"{label}: sky dome {dome.radiance} differs from the level-wide probe average {expected}")
    return errors


def verify_lights(published_path: Path = PUBLISHED_TSV) -> tuple[dict[str, int], list[str]]:
    consts = read_calibration_constants()
    if not published_path.is_file():
        return {}, [f"{published_path} missing: build and run tests/verify_proxy_tests.exe first (run_all_offline.ps1)"]
    newest_input = max(CALIBRATION_H.stat().st_mtime, *(p.stat().st_mtime for p in FIXTURES_DIR.glob("run*/*_lights.tsv")))
    if published_path.stat().st_mtime < newest_input:
        return {}, [f"{published_path.name} is older than light_calibration.h or the fixtures: rerun verify_proxy_tests.exe"]
    rows = load_published(published_path)
    dumps = {f"{p.parent.name}/{p.name}" for p in FIXTURES_DIR.glob("run*/*_lights.tsv")}
    missing = dumps - {r.dump for r in rows}
    errors = [f"{d}: not in {published_path.name}" for d in sorted(missing)]
    counts, row_errors = check_published(rows, consts)
    witness_counts, witness_errors = check_spotlight_witnesses(rows, FIXTURES_DIR)
    counts.update(witness_counts)
    errors += row_errors + witness_errors + check_domes(rows, FIXTURES_DIR, consts)
    if counts.get("fixtures", 0) < 400 or counts.get("fills", 0) < 50 or counts.get("weapon_spots", 0) < 2:
        errors.append(f"too few lights checked ({counts}): the fixtures were not all replayed")
    return counts, errors


def run_light_self_tests() -> None:
    """Mutations of the published lights that must be rejected."""
    consts = read_calibration_constants()
    rows = load_published()

    legacy = []
    for r in rows:
        if r.light_type in (1, 2) and not r.weapon_spot and r.peak > 1e-4:
            radius, value = legacy_unbounded_radiance(r.range_cm, r.falloff_start_cm, r.peak)
            r = Published(**{**r.__dict__, "radius": radius, "radiance": (value, value, value)})
        legacy.append(r)
    _, errs = check_published(legacy, consts)
    assert any("0.5 m brightness" in e for e in errs), "Self-test failed: legacy 9x-440x close-range blowout not caught"
    assert any("emitter radius" in e for e in errs), "Self-test failed: 14 cm pinpoint emitters not caught"

    dim_lamps = [
        Published(**{**r.__dict__, "radiance": tuple(c * 0.25 for c in r.radiance)})
        if (r.light_type in (1, 2) and not r.weapon_spot) else r
        for r in rows
    ]
    _, errs = check_published(dim_lamps, consts)
    assert any("median 1.8 m lamp brightness" in e for e in errs), "Self-test failed: dim 0.14x room lamps not caught"

    no_25m_fills = [r for r in rows if not (r.light_type == 4 and r.kind == KIND_SPHERE and r.probe_radius > 1000.0)]
    _, errs = check_published(no_25m_fills, consts)
    assert any("room fill lights published" in e for e in errs), "Self-test failed: dropped 25 m indoor zones not caught"

    dark = [Published(**{**r.__dict__, "radiance": (0.0, 0.0, 0.0)}) if r.light_type == 4 else r for r in rows]
    _, errs = check_published(dark, consts)
    assert any("darker than the game" in e for e in errs), "Self-test failed: missing room fill not caught"

    shifted_spots = [
        Published(**{**r.__dict__, "position": tuple(p - d * r.near_distance for p, d in zip(r.position, r.direction))})
        if r.light_type == 2 else r
        for r in rows
    ]
    _, w_errs = check_spotlight_witnesses(shifted_spots, FIXTURES_DIR)
    assert any("FlareNode attachment" in e for e in w_errs), "Self-test failed: 150 cm flashlight offset not caught by FlareNode witness"
    assert any("shader cSpot0" in e for e in w_errs), "Self-test failed: spotlight offset not caught by cSpot0 shader witness"

    flood_weapon = [
        Published(**{**r.__dict__, "cone_angle": 49.4, "radius": 35.0}) if r.weapon_spot else r
        for r in rows
    ]
    _, errs = check_published(flood_weapon, consts)
    assert any("floodlight regression" in e for e in errs) and any("weapon spotlight emitter radius" in e for e in errs), (
        "Self-test failed: floodlight/40cm weapon spotlight regression not caught"
    )

    no_dome = [r for r in rows if r.kind != KIND_DOME]
    assert check_domes(no_dome, FIXTURES_DIR, consts), "Self-test failed: missing sky dome not caught"

MAT_OVER_RE = re.compile(r'over\s+"mat_([^"]+)"')
ASSET_INPUT_RE = re.compile(
    r'asset\s+inputs:([A-Za-z0-9_]+)\s*=\s*@([^@]+)@(?:\s*\(([^)]*)\))?',
    re.DOTALL,
)
COLOR_SPACE_RE = re.compile(r'colorSpace\s*=\s*"([^"]+)"')


def validate_dds_file(dds_path: Path) -> str | None:
    """Check that dds_path exists, ends in .dds, and has a valid DDS header."""
    if dds_path.suffix.lower() != ".dds":
        return f"Remix rejects non-DDS texture '{dds_path.name}'"
    if not dds_path.is_file():
        return f"missing DDS file '{dds_path}'"
    data = dds_path.read_bytes()
    if len(data) < 128 or data[:4] != b"DDS ":
        return f"corrupt DDS header in '{dds_path}'"
    header_size, _flags, height, width = struct.unpack_from("<IIII", data, 4)
    if header_size != 124 or width == 0 or height == 0:
        return f"invalid DDS dimensions ({width}x{height}) in '{dds_path}'"
    return None


def validate_usda(usda_path: Path) -> tuple[int, list[str]]:
    """Validate one Remix mod.usda replacement layer."""
    errors: list[str] = []
    if not usda_path.is_file():
        return 0, [f"missing USD file: {usda_path}"]

    text = usda_path.read_text(encoding="utf-8")
    if not text.startswith("#usda 1.0"):
        errors.append(f"{usda_path}: missing '#usda 1.0' header")
    if text.count("{") != text.count("}"):
        errors.append(f"{usda_path}: unbalanced curly braces")
    mat_hashes = MAT_OVER_RE.findall(text)
    for h in mat_hashes:
        if not re.fullmatch(r"[0-9A-F]{16}", h):
            errors.append(f"{usda_path.name}: invalid 16-hex XXH3 hash 'mat_{h}'")
        if h in POISON_TEXTURE_HASHES:
            errors.append(f"{usda_path.name}: forbidden shared white 8x8 texture hash 'mat_{h}'")

    base_dir = usda_path.parent
    for prop, rel_asset, meta_block in ASSET_INPUT_RE.findall(text):
        asset_path = (base_dir / rel_asset).resolve()
        dds_err = validate_dds_file(asset_path)
        if dds_err:
            errors.append(f"{usda_path.name} ({prop}): {dds_err}")
        cs_match = COLOR_SPACE_RE.search(meta_block or "")
        color_space = cs_match.group(1) if cs_match else ""
        if prop in ("normalmap_texture", "reflectionroughness_texture", "metallic_texture"):
            if color_space != "raw":
                errors.append(
                    f"{usda_path.name} ({prop} -> {rel_asset}): expected colorSpace='raw', got '{color_space}'"
                )
        elif prop in ("diffuse_texture", "emissive_mask_texture"):
            if color_space not in ("", "auto", "sRGB"):
                errors.append(
                    f"{usda_path.name} ({prop} -> {rel_asset}): expected colorSpace='auto'/'sRGB', got '{color_space}'"
                )

    return len(mat_hashes), errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="Also prove that broken lights and USD are rejected")
    args = parser.parse_args()

    counts, light_errs = verify_lights()
    mat_count, usda_errs = validate_usda(MATERIALS_DIR / "mod.usda")
    glow_count = 0
    if (GLOW_MOD_DIR / "mod.usda").is_file():
        glow_count, glow_errs = validate_usda(GLOW_MOD_DIR / "mod.usda")
        usda_errs.extend(glow_errs)

    all_errs = light_errs + usda_errs
    if all_errs:
        for e in all_errs[:25]:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1

    if args.self_test:
        run_light_self_tests()
        _, poison_errs = validate_usda_string_test()
        assert poison_errs, "Self-test failed: poison white 8x8 hash was not caught"

    print(
        f"PASS: verify_lights_and_materials checked {counts['fixtures']} recorded fixture lights "
        f"(0.5 m <= {MAX_CLOSE_RATIO_HARD_LIMIT:.2f}x the game, {counts['capped']} at the cap), "
        f"{counts['fills']} room fills (>= {MIN_FILL_FRACTION_OF_GAME_MEAN:.0%} of the game's SH average), "
        f"{counts['domes']} sky domes, {mat_count} PBR material overrides and {glow_count} glow overrides."
    )
    return 0


def validate_usda_string_test() -> tuple[int, list[str]]:
    """Negative self-test ensuring the poison 8x8 white texture hash is rejected (temp file, not in materials/)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "selftest_poison.usda"
        path.write_text('#usda 1.0\nover "RootNode" { over "Looks" { over "mat_2164293A60D148AC" {} } }\n',
                        encoding="utf-8")
        return validate_usda(path)


if __name__ == "__main__":
    raise SystemExit(main())
