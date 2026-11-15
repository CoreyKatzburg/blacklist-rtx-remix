"""Checks a rtx_telemetry.jsonl file against the Phase 5 field contract (see rtx_telemetry.h).

Usage: python check_telemetry_contract.py <file.jsonl>   (exit code 0 = every line valid)
Works on the sample written by runtime_logic_test.exe and on the file the game run produces.
"""
import json
import sys

REQUIRED = {
    "frame": int,
    "frameTimeMs": (int, float),
    "gpuTimeMs": (int, float),
    "instances": int,
    "skinnedInstances": int,
    "staticBlasRebuilds": int,
    "cameraCut": bool,
    "textureMemoryMB": (int, float),
    "vramUsedMB": (int, float),
    "skinnedTriangles": int,
    "inwardSkinnedTriangles": int,
    "rayTraced": bool,
    "telemetryCpuMs": (int, float),
    "replacedMaterialDraws": int,
    "legacyMaterialDraws": int,
    "texturesDemoted": int,
    "textureBudgetMB": (int, float),
    "thumbNonFinite": int,
    "thumbClipped": int,
    "skips": dict,
    "passMs": dict,
}
# Keep in step with kRtxSkipNames in rtx_telemetry.h.
SKIP_REASONS = {
    "noVertexCapture", "zeroPrimitives", "unsupportedTopology", "alphaTestOff", "alphaBlendOff", "occlusionQuery",
    "noColorTarget", "colorWriteOff", "shadowMask", "nonPrimaryTarget", "stencilShadow", "preTransformed",
    "noTriangles", "noVertices", "noTextureHash",
}


def check_maps(obj):
    """Problems in one record's skips and passMs maps, as a list of strings."""
    problems = []
    skips = obj.get("skips") if isinstance(obj.get("skips"), dict) else {}
    passes = obj.get("passMs") if isinstance(obj.get("passMs"), dict) else {}
    for reason, count in skips.items():
        if reason not in SKIP_REASONS or type(count) is not int or count <= 0:
            problems.append(f"skips[{reason}] = {count!r}")
    for path, entry in passes.items():
        numbers = isinstance(entry, list) and len(entry) == 3 and all(type(v) in (int, float) for v in entry)
        # gpuMs is -1 when the GPU result never arrived, otherwise not negative.
        if not numbers or (entry[0] < 0 and entry[0] != -1) or entry[1] < 0 or type(entry[2]) is not int or entry[2] < 1:
            problems.append(f"passMs[{path}] = {entry!r}")
    return problems


def check(path):
    errors = []
    previous = None
    count = 0
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            count += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"line {number}: not JSON ({e})")
                continue
            for key, kind in REQUIRED.items():
                if key not in obj:
                    errors.append(f"line {number}: missing {key}")
                elif isinstance(obj[key], bool) and kind is not bool:
                    errors.append(f"line {number}: {key} is a bool")
                elif not isinstance(obj[key], kind):
                    errors.append(f"line {number}: {key} has type {type(obj[key]).__name__}")
            if isinstance(obj.get("frame"), int):
                if obj["frame"] < 0 or (previous is not None and obj["frame"] <= previous):
                    errors.append(f"line {number}: frame {obj['frame']} not increasing")
                previous = obj["frame"]
            errors += [f"line {number}: {p}" for p in check_maps(obj)]
            for key in ("frameTimeMs", "gpuTimeMs", "textureMemoryMB", "vramUsedMB", "telemetryCpuMs", "textureBudgetMB"):
                if isinstance(obj.get(key), (int, float)) and not isinstance(obj.get(key), bool) and obj[key] < 0:
                    errors.append(f"line {number}: {key} negative")
            if len(errors) > 20:
                break
    if count == 0:
        errors.append("file is empty")
    return count, errors


if __name__ == "__main__":
    lines, errs = check(sys.argv[1])
    for e in errs:
        print("FAIL", e)
    print(f"check_telemetry_contract: {lines} lines, {'PASS' if not errs else str(len(errs)) + ' errors'}")
    sys.exit(1 if errs else 0)
