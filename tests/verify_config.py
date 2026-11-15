"""Validate Blacklist RTX Remix configuration files against Remix 1.5.2.

rtx.conf keys must be documented in Remix 1.5.2's RtxOptions.md (the generated option reference; options that
exist only in a patched source tree are rejected), with values of the documented type and range, no duplicates,
and none that Remix or the proxy overwrites at runtime:
- the Quality preset layer when rtx.graphicsPreset is not Custom (UserSetting options), and the DLSS preset;
- the ray-reconstruction path-tracer preset and the ReSTIR GI one (rtx_options.cpp updatePathTracerPreset,
  rtx_restir_gi_rayquery.cpp setToRayReconstructionPreset), written to the Derived layer (6), above rtx.conf (3);
- the NRC quality preset (rtx_neural_radiance_cache.cpp, applied at startup whatever its value);
- options the proxy sets itself (HandoffBeforePost overrides, AutoRaytracing's keep-alive, DecalTag's list).
bridge.conf keys are checked against the bridge sources, and d3d9_proxy.ini for split-file conflicts.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REMIX_ROOT = Path(__file__).resolve().parents[1]
RTX_OPTIONS_MD = REMIX_ROOT / "src" / "dxvk-remix" / "RtxOptions.md"
PROXY_CPP = REMIX_ROOT / "play-setup" / "unified-build" / "handoff_renderer.cpp"
DXVK_SRC = REMIX_ROOT / "src" / "dxvk-remix" / "src"
BRIDGE_SRC = REMIX_ROOT / "src" / "dxvk-remix" / "bridge" / "src"
PLAY_SETUP = REMIX_ROOT / "play-setup"

RTX_MACRO_RE = re.compile(
    r'RTX_OPTION(?:_ARGS|_ENV|_FLAG|_FLAG_ENV)?\s*\(\s*"([^"]+)"\s*,\s*([^,]+?)\s*,\s*([A-Za-z0-9_]+)',
    re.MULTILINE,
)
BRIDGE_OPT_RE = re.compile(
    r'Config::getOption<\s*([^>]+?)\s*>\s*\(\s*"([^"]+)"',
    re.MULTILINE,
)


@dataclass(frozen=True)
class OptionSpec:
    """Metadata for one Remix or Bridge configuration option."""

    name: str
    cpp_type: str
    user_setting: bool = False
    min_value: float | None = None
    max_value: float | None = None


# Options Remix's presets write to a layer above rtx.conf (Remix 1.5.2 source, see the module docstring).
RR_PATH_TRACER_PRESET = (
    "rtx.di.stealBoundaryPixelSamplesWhenOutsideOfScreen", "rtx.di.permutationSamplingNthFrame",
    "rtx.di.enableDenoiserConfidence", "rtx.di.enableBestLightSampling", "rtx.di.initialSampleCount",
    "rtx.di.spatialSamples", "rtx.di.disocclusionSamples", "rtx.di.enableSampleStealing",
    "rtx.minOpaqueDiffuseLobeSamplingProbability", "rtx.minOpaqueSpecularLobeSamplingProbability",
    "rtx.enableFirstBounceLobeProbabilityDithering", "rtx.russianRouletteMode", "rtx.neeCache.enableModeAfterFirstBounce",
    "rtx.demodulate.enableDirectLightBoilingFilter", "rtx.postFilterThreshold", "rtx.usePostFilter",
)
RESTIR_GI_RR_PRESET = tuple(
    "rtx.restirGI." + k for k in (
        "boilingFilterMinThreshold", "boilingFilterMaxThreshold", "historyDiscardStrength",
        "boilingFilterRemoveReservoirThreshold", "useVirtualSample", "virtualSampleMaxDistanceRatio",
        "useReflectionReprojection", "useAdaptiveTemporalHistory", "stealBoundaryPixelSamplesWhenOutsideOfScreen",
        "useSampleStealing", "sampleStealingJitter", "validateVisibilityChange", "useDLSSRRCompatibilityMode")
)
NRC_QUALITY_PRESET = tuple(
    "rtx.neuralRadianceCache." + k for k in (
        "terminationHeuristicThreshold", "smallestResolvableFeatureSizeMeters", "targetNumTrainingIterations",
        "trainingMaxPathBounces")
)
# Set live by the proxy (lightpub AutoRaytracing, ParticleTag); a value in rtx.conf would only mislead.
PROXY_OWNED = ("rtx.sceneKeepAliveFrames", "rtx.particleTextures")

MD_TYPES = {
    "bool": "bool", "int": "int", "float": "float", "float2": "Vector2", "float3": "Vector3", "float4": "Vector4",
    "hash set": "HashSet", "hash vector": "HashVector", "string": "string", "int2": "int2", "virtual keys": "keys",
}


def md_number(text: str) -> float | None:
    """A documented min/max cell ('0', '1', '0\\.5', or empty) as a number."""
    try:
        return float(text.replace("\\", ""))
    except ValueError:
        return None


def load_md_schema(md_path: Path, flags_from: dict[str, OptionSpec]) -> dict[str, OptionSpec]:
    """Every option documented in RtxOptions.md, with its type and range (UserSetting flags from the sources)."""
    schema: dict[str, OptionSpec] = {}
    for line in md_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|rtx."):
            continue
        cells = line.split("|")
        name, md_type, low, high = cells[1], cells[2], cells[4], cells[5]
        source = flags_from.get(name)
        numeric = md_type in ("int", "float")
        schema[name] = OptionSpec(
            name=name,
            cpp_type=MD_TYPES.get(md_type, md_type),
            user_setting=bool(source and source.user_setting),
            min_value=md_number(low) if numeric else None,
            max_value=md_number(high) if numeric else None,
        )
    return schema


def load_proxy_handoff_overrides(proxy_cpp: Path) -> tuple[str, ...]:
    """The rtx.* keys d3d9_proxy.cpp ApplyHandoffOverrides sets while HandoffBeforePost is on."""
    text = proxy_cpp.read_text(encoding="utf-8", errors="ignore")
    start = text.find("void ApplyHandoffOverrides()")
    end = text.find("\n}\n", start)
    return tuple(re.findall(r'\{"(rtx\.[A-Za-z0-9_.]+)"', text[start:end])) if start >= 0 else ()


def load_remix_schema(src_dir: Path) -> dict[str, OptionSpec]:
    """Extract all RTX_OPTION definitions from dxvk-remix C++ headers and sources."""
    schema: dict[str, OptionSpec] = {}
    for root, _, files in os.walk(src_dir):
        for fn in files:
            if not fn.endswith((".h", ".cpp", ".inl")):
                continue
            text = (Path(root) / fn).read_text(encoding="utf-8", errors="ignore")
            for match in RTX_MACRO_RE.finditer(text):
                ns, cpp_type, ident = (
                    match.group(1).strip(),
                    match.group(2).strip(),
                    match.group(3).strip(),
                )
                end = text.find(");", match.end())
                span = text[match.start() : end + 2] if end != -1 else ""
                full_name = f"{ns}.{ident}"
                schema[full_name] = OptionSpec(
                    name=full_name,
                    cpp_type=cpp_type,
                    user_setting="UserSetting" in span,
                )
    return schema


def load_bridge_schema(src_dir: Path) -> dict[str, OptionSpec]:
    """Extract all Bridge configuration options from bridge C++ sources."""
    schema: dict[str, OptionSpec] = {}
    for root, _, files in os.walk(src_dir):
        for fn in files:
            if not fn.endswith((".h", ".cpp")):
                continue
            text = (Path(root) / fn).read_text(encoding="utf-8", errors="ignore")
            for match in BRIDGE_OPT_RE.finditer(text):
                cpp_type, key = match.group(1).strip(), match.group(2).strip()
                schema[key] = OptionSpec(name=key, cpp_type=cpp_type)
    return schema


def parse_flat_conf(text: str, label: str) -> tuple[dict[str, str], list[str]]:
    """Parse a flat key = value config file and flag duplicate keys."""
    values: dict[str, str] = {}
    errors: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if "=" not in line:
            errors.append(f"{label}:{lineno}: malformed line without '=': {line}")
            continue
        key, val = [part.strip() for part in line.split("=", 1)]
        if key in values:
            errors.append(f"{label}:{lineno}: duplicate key '{key}'")
        values[key] = val
    return values, errors


def parse_ini_sections(text: str, label: str) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Parse an INI file into sections and flag duplicate keys within a section."""
    sections: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    current = ""
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            sections.setdefault(current, {})
            continue
        if "=" not in line or not current:
            errors.append(f"{label}:{lineno}: malformed INI entry: {line}")
            continue
        key, val = [part.strip() for part in line.split("=", 1)]
        if key in sections[current]:
            errors.append(f"{label}:{lineno}: duplicate key '[{current}] {key}'")
        sections[current][key] = val
    return sections, errors


def check_value_type(spec: OptionSpec, raw_val: str) -> str | None:
    """Return an error message if raw_val cannot be parsed as spec.cpp_type."""
    t = spec.cpp_type.replace("std::", "")
    if t == "bool":
        if raw_val not in ("True", "False", "true", "false"):
            return f"expected True/False for bool option '{spec.name}', got '{raw_val}'"
        return None
    if t in ("int", "int32_t", "uint32_t", "uint16_t", "uint8_t", "uint", "size_t", "float"):
        try:
            val = float(raw_val) if t == "float" else int(raw_val, 0)
        except ValueError:
            return f"expected {t} for '{spec.name}', got '{raw_val}'"
        if (t.startswith("u") or t == "size_t") and val < 0:
            return f"expected unsigned integer for '{spec.name}', got '{raw_val}'"
        if spec.min_value is not None and val < spec.min_value:
            return f"'{spec.name}' = {raw_val} is below its minimum {spec.min_value:g}"
        if spec.max_value is not None and val > spec.max_value:
            return f"'{spec.name}' = {raw_val} is above its maximum {spec.max_value:g}"
        return None
    if t in ("Vector2", "Vector3", "Vector4"):
        expected_len = int(t[-1])
        parts = [p.strip() for p in raw_val.split(",") if p.strip()]
        if len(parts) != expected_len:
            return f"expected {expected_len} comma-separated floats for '{spec.name}', got '{raw_val}'"
        try:
            for p in parts:
                float(p)
        except ValueError:
            return f"non-numeric component in {t} '{spec.name}': '{raw_val}'"
        return None
    if "unordered_set" in t or "HashSet" in t or "HashVector" in t:
        if not raw_val:
            return None
        for item in raw_val.split(","):
            tok = item.strip()
            if not re.fullmatch(r"0x[0-9A-Fa-f]{1,16}", tok):
                return f"expected 64-bit hex hash in '{spec.name}', got '{tok}'"
        return None
    return None


def validate_all(
    rtx_text: str,
    bridge_text: str,
    ini_text: str,
    rtx_schema: dict[str, OptionSpec],
    bridge_schema: dict[str, OptionSpec],
) -> list[str]:
    """Validate rtx.conf, bridge.conf, and d3d9_proxy.ini contents together."""
    errors: list[str] = []

    rtx_vals, rtx_parse_errs = parse_flat_conf(rtx_text, "rtx.conf")
    bridge_vals, bridge_parse_errs = parse_flat_conf(bridge_text, "bridge.conf")
    ini_secs, ini_parse_errs = parse_ini_sections(ini_text, "d3d9_proxy.ini")
    errors.extend(rtx_parse_errs + bridge_parse_errs + ini_parse_errs)

    has_user_settings = False
    for key, val in rtx_vals.items():
        spec = rtx_schema.get(key)
        if spec is None:
            errors.append(f"rtx.conf: unknown Remix 1.5.2 option '{key}'")
            continue
        if spec.user_setting and key not in ("rtx.graphicsPreset", "rtx.dlssPreset"):
            has_user_settings = True
        type_err = check_value_type(spec, val)
        if type_err:
            errors.append(f"rtx.conf: {type_err}")

    if has_user_settings and rtx_vals.get("rtx.graphicsPreset") != "4":
        errors.append(
            "rtx.conf: UserSetting options present without 'rtx.graphicsPreset = 4' (Custom); "
            "Remix Quality preset layer (0xFFFFFFFF) will silently override them."
        )

    def overridden(keys: tuple[str, ...], by: str) -> None:
        for key in keys:
            if key not in rtx_schema:
                errors.append(f"verify_config: override table names '{key}', which RtxOptions.md does not have")
            elif key in rtx_vals:
                errors.append(f"rtx.conf: '{key}' is overwritten at runtime by {by}; remove it")

    if rtx_vals.get("rtx.upscalerType", "1") == "1" and rtx_vals.get("rtx.enableRayReconstruction", "True") == "True":
        overridden(RR_PATH_TRACER_PRESET, "the ray-reconstruction path-tracer preset")
        if rtx_vals.get("rtx.integrateIndirectMode", "2") == "1":
            overridden(RESTIR_GI_RR_PRESET, "the ReSTIR GI ray-reconstruction preset")
    overridden(NRC_QUALITY_PRESET, "the NRC quality preset (rtx.neuralRadianceCache.qualityPreset)")
    overridden(PROXY_OWNED, "the proxy (lightpub AutoRaytracing / DecalTag)")

    dlss_dependent = {"rtx.qualityDLSS", "rtx.upscalerType", "rtx.reflexMode"}
    if (dlss_dependent & rtx_vals.keys()) and rtx_vals.get("rtx.dlssPreset") != "2":
        errors.append(
            "rtx.conf: DLSS/Reflex options present without 'rtx.dlssPreset = 2' (Custom); "
            "updateUpscalerFromDlssPreset() will silently overwrite them."
        )
    if rtx_vals.get("rtx.lowMemoryGpu") != "False":
        errors.append("rtx.conf: rtx.lowMemoryGpu must be False so Remix loads full-resolution normal/roughness mips")
    if rtx_vals.get("rtx.antiCulling.object.enable") != "True":
        errors.append("rtx.conf: rtx.antiCulling.object.enable must be True to prevent off-screen wall light-leak flicker")
    if rtx_vals.get("rtx.rayreconstruction.model") != "1":
        errors.append("rtx.conf: rtx.rayreconstruction.model must be 1 (Transformer denoiser)")
    try:
        if int(rtx_vals.get("rtx.qualityDLSS", "0")) < 2:
            errors.append("rtx.conf: rtx.qualityDLSS must be >= 2 (Balanced or Quality)")
    except ValueError:
        pass
    decal_raw = rtx_vals.get("rtx.decalTextures", "")
    decal_items = [x.strip() for x in decal_raw.split(",") if x.strip()]
    if len(decal_items) < 100:
        errors.append(f"rtx.conf: rtx.decalTextures must pre-load offline decal hashes (found {len(decal_items)})")

    remix_ini = ini_secs.get("RemixConfig", {})
    if remix_ini:
        errors.append(
            f"d3d9_proxy.ini: [RemixConfig] must be empty (found {sorted(remix_ini.keys())}); "
            "all static rtx.* options belong in rtx.conf."
        )
    if "Lights" in ini_secs and ini_secs["Lights"]:
        errors.append(
            f"d3d9_proxy.ini: [Lights] overrides ({sorted(ini_secs['Lights'].keys())}) are built-in C++ defaults; "
            "keep d3d9_proxy.ini minimal."
        )

    proxy_sec = ini_secs.get("Proxy", {})
    allowed_proxy_keys = {"EnableDebuggingTelemetry"}
    extra_proxy_keys = set(proxy_sec.keys()) - allowed_proxy_keys
    if extra_proxy_keys:
        errors.append(
            f"d3d9_proxy.ini: remove obsolete/built-in [Proxy] keys {sorted(extra_proxy_keys)}; "
            "only EnableDebuggingTelemetry belongs in play-setup/d3d9_proxy.ini"
        )
    if proxy_sec.get("HandoffBeforePost", "1") == "1" and proxy_sec.get("ModernFilter", "1") == "0":
        handoff_keys = load_proxy_handoff_overrides(PROXY_CPP)
        if not handoff_keys:
            errors.append("verify_config: could not read ApplyHandoffOverrides from handoff_renderer.cpp")
        overridden(handoff_keys, "the proxy's HandoffBeforePost mode (ApplyHandoffOverrides)")

    for key, val in bridge_vals.items():
        spec = bridge_schema.get(key)
        if spec is None:
            errors.append(f"bridge.conf: unknown Bridge option '{key}'")
            continue
        type_err = check_value_type(spec, val)
        if type_err:
            errors.append(f"bridge.conf: {type_err}")

    if bridge_vals.get("exposeRemixApi") not in ("True", "true"):
        errors.append("bridge.conf: exposeRemixApi must be True for lightpub to publish lights")

    try:
        if int(bridge_vals.get("presentSemaphoreMaxFrames", "3")) > 1:
            errors.append("bridge.conf: presentSemaphoreMaxFrames must be <= 1 to prevent multi-frame input lag")
    except ValueError:
        errors.append("bridge.conf: presentSemaphoreMaxFrames must be an integer <= 1")

    for qkey in ("clientCmdQueueSize", "clientDataQueueSize"):
        try:
            if int(bridge_vals.get(qkey, "0")) < 32768:
                errors.append(f"bridge.conf: {qkey} must be >= 32768 to avoid per-frame queue stalls")
        except ValueError:
            pass

    return errors


def run_self_tests(
    rtx_text: str,
    bridge_text: str,
    ini_text: str,
    rtx_schema: dict[str, OptionSpec],
    bridge_schema: dict[str, OptionSpec],
) -> None:
    """Verify that intentional config bugs are deterministically rejected."""
    bad_type = rtx_text.replace(
        "rtx.volumetrics.enable = True",
        "rtx.volumetrics.enable = 0.30",
    )
    assert any(
        "rtx.volumetrics.enable" in e
        for e in validate_all(bad_type, bridge_text, ini_text, rtx_schema, bridge_schema)
    ), "Self-test failed: bool/float mismatch was not caught"

    bad_preset = rtx_text.replace("rtx.graphicsPreset = 4", "rtx.graphicsPreset = 0")
    assert any(
        "graphicsPreset = 4" in e
        for e in validate_all(bad_preset, bridge_text, ini_text, rtx_schema, bridge_schema)
    ), "Self-test failed: Auto preset override was not caught"

    bad_ini = ini_text + "\n[RemixConfig]\nrtx.tonemappingMode = 0\n"
    assert any(
        "[RemixConfig]" in e
        for e in validate_all(rtx_text, bridge_text, bad_ini, rtx_schema, bridge_schema)
    ), "Self-test failed: split [RemixConfig] key was not caught"

    split_trace = ini_text + "\nFrameTrace=1\n"
    assert any(
        "FrameTrace" in e
        for e in validate_all(rtx_text, bridge_text, split_trace, rtx_schema, bridge_schema)
    ), "Self-test failed: legacy/extra INI key was not caught"

    bad_low_mem = rtx_text.replace("rtx.lowMemoryGpu = False", "rtx.lowMemoryGpu = True")
    assert any(
        "lowMemoryGpu" in e
        for e in validate_all(bad_low_mem, bridge_text, ini_text, rtx_schema, bridge_schema)
    ), "Self-test failed: lowMemoryGpu = True regression was not caught"

    bad_rr = rtx_text.replace("rtx.rayreconstruction.model = 1", "rtx.rayreconstruction.model = 0")
    assert any(
        "rayreconstruction.model" in e
        for e in validate_all(bad_rr, bridge_text, ini_text, rtx_schema, bridge_schema)
    ), "Self-test failed: CNN denoiser model=0 regression was not caught"

    bad_lag = bridge_text.replace("presentSemaphoreMaxFrames = 1", "presentSemaphoreMaxFrames = 3")
    assert any(
        "presentSemaphoreMaxFrames" in e
        for e in validate_all(rtx_text, bad_lag, ini_text, rtx_schema, bridge_schema)
    ), "Self-test failed: 3-frame input lag queue was not caught"

    def rejects(extra: str, needle: str, what: str) -> None:
        errs = validate_all(rtx_text + "\n" + extra + "\n", bridge_text, ini_text, rtx_schema, bridge_schema)
        assert any(needle in e for e in errs), f"Self-test failed: {what} was not caught ({errs})"

    # A real option of the patched source tree that Remix 1.5.2 does not have (Track B's cameraCutDistance).
    rejects("rtx.cameraCutDistance = 1200.0", "unknown Remix 1.5.2 option", "an option missing from RtxOptions.md")
    rejects("rtx.enableVsync = 0", "duplicate key", "a duplicate key")
    rejects("rtx.di.initialSampleCount = 4", "ray-reconstruction path-tracer preset", "an RR-preset override")
    rejects("rtx.neuralRadianceCache.trainingMaxPathBounces = 4", "NRC quality preset", "an NRC-preset override")
    rejects("rtx.sceneKeepAliveFrames = 5", "the proxy", "a proxy-owned option")
    rejects("rtx.volumetrics.anisotropy = 2.0", "above its maximum", "an out-of-range value")

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="Run negative mutation checks")
    args = parser.parse_args()

    if not DXVK_SRC.is_dir() or not BRIDGE_SRC.is_dir():
        print(f"FAIL: dxvk-remix source tree missing at {DXVK_SRC}", file=sys.stderr)
        return 1

    if not RTX_OPTIONS_MD.is_file():
        print(f"FAIL: {RTX_OPTIONS_MD} missing", file=sys.stderr)
        return 1
    rtx_schema = load_md_schema(RTX_OPTIONS_MD, load_remix_schema(DXVK_SRC))
    bridge_schema = load_bridge_schema(BRIDGE_SRC)
    # Add standalone bridge options checked via Config::getOption in other bridge files
    bridge_schema.setdefault("exposeRemixApi", OptionSpec("exposeRemixApi", "bool"))
    bridge_schema.setdefault(
        "eliminateRedundantSetterCalls",
        OptionSpec("eliminateRedundantSetterCalls", "bool"),
    )
    bridge_schema.setdefault("sharedHeapPolicy", OptionSpec("sharedHeapPolicy", "string"))

    rtx_text = (PLAY_SETUP / "rtx.conf").read_text(encoding="utf-8")
    bridge_text = (PLAY_SETUP / "bridge.conf").read_text(encoding="utf-8")
    ini_text = (PLAY_SETUP / "d3d9_proxy.ini").read_text(encoding="utf-8")

    errors = validate_all(rtx_text, bridge_text, ini_text, rtx_schema, bridge_schema)
    if errors:
        for err in errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1

    if args.self_test:
        run_self_tests(rtx_text, bridge_text, ini_text, rtx_schema, bridge_schema)

    print(
        f"PASS: verify_config validated rtx.conf, bridge.conf, and d3d9_proxy.ini "
        f"against {len(rtx_schema)} RtxOptions.md options (no preset or proxy overrides) "
        f"and {len(bridge_schema)} Bridge options."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
