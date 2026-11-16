"""Checks a rtx_telemetry.jsonl file against the Phase 5 field contract (see rtx_telemetry.h).

Usage: python check_telemetry_contract.py <file.jsonl>   (exit code 0 = every line valid)
Works on the sample written by runtime_logic_test.exe and on the file the game run produces.
"""
import json
import sys

REQUIRED = {
    "frame": int,
    "qpcMs": (int, float),
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
    "zeroNormalSkinnedTriangles": int,
    "zeroNormalInstances": int,
    "zeroNormalStaticTriangles": int,
    "zeroNormalStaticInstances": int,
    "topInwardInstances": list,
    "rayTraced": bool,
    "telemetryCpuMs": (int, float),
    "replacedMaterialDraws": int,
    "legacyMaterialDraws": int,
    "texturesDemoted": int,
    "textureBudgetMB": (int, float),
    "thumbNonFinite": int,
    "thumbClipped": int,
    "remixShaderCompiles": int,   # Remix pipelines queued or compiling at frame end; ray tracing waits while > 0
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


HEX16 = set("0123456789ABCDEF")
TOP_KEYS = ("inward", "tris", "geomHash", "albedoHash", "cull", "flip", "blend", "enc")


def check_normal_gate(obj):
    """Problems in one record's normal-gate fields (zero-normal counts and topInwardInstances)."""
    problems = []
    ints = [k for k in ("skinnedTriangles", "inwardSkinnedTriangles", "zeroNormalSkinnedTriangles", "zeroNormalInstances",
                        "zeroNormalStaticTriangles", "zeroNormalStaticInstances") if type(obj.get(k)) is int]
    if len(ints) == 6 and obj["zeroNormalSkinnedTriangles"] > obj["skinnedTriangles"]:
        problems.append("zeroNormalSkinnedTriangles above skinnedTriangles")
    if len(ints) == 6 and (obj["zeroNormalInstances"] > obj["zeroNormalSkinnedTriangles"]
                           or obj["zeroNormalStaticInstances"] > obj["zeroNormalStaticTriangles"]):
        problems.append("more zero-normal instances than zero-normal triangles")
    top = obj.get("topInwardInstances")
    if not isinstance(top, list):
        return problems
    if len(top) > 8:
        problems.append(f"topInwardInstances has {len(top)} entries, at most 8")
    previous = None
    for i, e in enumerate(top):
        if not isinstance(e, dict) or set(e) != set(TOP_KEYS):
            problems.append(f"topInwardInstances[{i}] keys {sorted(e) if isinstance(e, dict) else e!r}")
            continue
        if any(type(e[k]) is not int for k in ("inward", "tris", "cull", "flip", "blend", "enc")) \
                or any(not (isinstance(e[k], str) and len(e[k]) == 16 and set(e[k]) <= HEX16) for k in ("geomHash", "albedoHash")):
            problems.append(f"topInwardInstances[{i}] types {e!r}")
            continue
        if not 0 < e["inward"] <= e["tris"] or e["flip"] not in (0, 1) or e["blend"] not in (0, 1) or e["enc"] not in (0, 1):
            problems.append(f"topInwardInstances[{i}] values {e!r}")
        if previous is not None and e["inward"] > previous:
            problems.append(f"topInwardInstances[{i}] not sorted by inward, most first")
        previous = e["inward"]
    if type(obj.get("inwardSkinnedTriangles")) is int and sum(
            e["inward"] for e in top if isinstance(e, dict) and type(e.get("inward")) is int) > obj["inwardSkinnedTriangles"]:
        problems.append("topInwardInstances inward sum above inwardSkinnedTriangles")
    return problems


def check(path):
    errors = []
    previous = None
    previous_qpc = None
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
            # qpcMs joins this file to d3d9_proxy_frames.csv column 2. 0 means not stamped; every stamp must
            # be later than the previous stamp, or the join would put two Remix frames in one proxy frame.
            qpc = obj.get("qpcMs")
            if isinstance(qpc, (int, float)) and not isinstance(qpc, bool):
                if qpc < 0:
                    errors.append(f"line {number}: qpcMs negative")
                elif qpc > 0:
                    if previous_qpc is not None and qpc <= previous_qpc:
                        errors.append(f"line {number}: qpcMs {qpc} not after {previous_qpc}")
                    previous_qpc = qpc
            errors += [f"line {number}: {p}" for p in check_maps(obj)]
            errors += [f"line {number}: {p}" for p in check_normal_gate(obj)]
            for key in ("frameTimeMs", "gpuTimeMs", "textureMemoryMB", "vramUsedMB", "telemetryCpuMs", "textureBudgetMB",
                        "vramFreeMB", "remixShaderCompiles"):
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
