"""Cross-checks the C++ capture analysis (capture_analysis_test.exe --dump) with an independent
Python reading of the game's shader constant tables (vs_layout.py).

For every world shader (declares gWorldToProj) that got geometry hash ranges:
  - no camera-only constant register is hashed;
  - every gLocalToWorld / gBones register is hashed.
Usage: python check_capture_dump.py <dump.jsonl> [VertexShaders.bin]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vs_layout  # noqa: E402

CAMERA = {"gWorldToProj", "gViewportOffset", "gViewportSB", "cViewPos_VS", "cProjSkinMesh"}
MOTION = {"gLocalToWorld", "gBones"}


def main():
    dump = {}
    with open(sys.argv[1], encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            dump[d["offset"]] = d
    shaders = vs_layout.load(sys.argv[2]) if len(sys.argv) > 2 else vs_layout.load()
    errors = []
    checked = 0
    if len(shaders) != len(dump):
        errors.append(f"shader count differs: python {len(shaders)}, C++ {len(dump)}")
    for s in shaders:
        d = dump.get(s.offset)
        if d is None:
            errors.append(f"0x{s.offset:x}: missing from C++ dump")
            continue
        if not s.is_world or not d["hasRanges"]:
            continue
        checked += 1
        hashed = set()
        for first, count in d["ranges"]:
            hashed.update(range(first, first + count))
        for name, (regset, first, count) in s.constants.items():
            if regset != 2:
                continue
            regs = set(range(first, first + count))
            if name in CAMERA and regs & hashed:
                errors.append(f"0x{s.offset:x}: camera constant {name} hashed")
            if name in MOTION and not regs <= hashed:
                errors.append(f"0x{s.offset:x}: {name} not fully hashed")
    for e in errors[:30]:
        print("FAIL", e)
    print(f"check_capture_dump: {checked} world shaders with ranges checked, {'PASS' if not errors else str(len(errors)) + ' errors'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
