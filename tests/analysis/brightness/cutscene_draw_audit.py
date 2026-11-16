#!/usr/bin/env python3
"""Audit a proxy `CaptureDraws=1` frame dump (`f<frame>_draws.tsv`) for post-Remix pass-through draws.

Tests the pre-registered `play-setup/unified-build/draw_filter.cpp` (lines 440-465) hypothesis
for the Benghazi opening cutscene whiteout (`20261116-063147/ben_interrogation`: same-launch
Remix `lumaThumb` mean `0.3217` vs screen capture mean `0.6831`, near-white `0.0001` vs `0.4021`):

  - Physical coverage lower bound: lifting mean brightness from `0.3217` to `0.6831` with `[0, 1]`
    screen pixels requires at least `(0.6831 - 0.3217) / (1.0 - 0.3217) = 0.5328` (`53.3%`) of the
    viewport (and `>= 92.2%` on `0.947`-mean Shot 1 frames).
  - Class A (`draw_filter.cpp:449-455`, while `g_modernAwaitingComposite == true`): draws with
    `handoffdone=1` before composite that write to `g_sceneHdrTarget` (`D3DFMT_A16B16G16R16F`, `f113`)
    without reading `g_sceneHdrTarget` at `t0` (`action=sent`). `PresentModernSceneToComposite`
    encodes `g_sceneHdrTarget` with raw `pow(x, 1/2.2)` (`scale = 1.0`), skipping game exposure.
  - Class B (`draw_filter.cpp:442-455`, after `PresentModernSceneToComposite` clears
    `g_modernAwaitingComposite = false`): draws with `handoffdone=1` after the composite trigger draw
    (`action=sent`), rasterizing directly onto the composite RT or backbuffer (`rtback=1`).
  - Falsification: if cumulative viewport coverage across Class A + Class B pass-through draws is
    `< 0.5328` (`53.3%`), `draw_filter.cpp:441-455` pass-through draws cannot explain the `0.32 -> 0.68`
    jump -> `"pass_through_falsified_coverage_too_small"`.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from typing import Any

MIN_COVERAGE_FOR_MEAN_JUMP = (0.6831 - 0.3217) / (1.0 - 0.3217)  # 0.5328025...


def _parse_surface(val: str) -> dict[str, Any] | None:
    if not val or val == "0":
        return None
    parts = val.split(",")
    out: dict[str, Any] = {"raw": val, "ptr": parts[0]}
    for p in parts[1:]:
        if "x" in p and p[0].isdigit():
            w_s, h_s = p.split("x", 1)
            if w_s.isdigit() and h_s.isdigit():
                out["w"], out["h"] = int(w_s), int(h_s)
        elif p.startswith("f") and p[1:].isdigit():
            out["fmt"] = int(p[1:])
        elif p.startswith("tex"):
            out["tex"] = p[3:]
    return out


def _parse_tex(val: str) -> dict[str, Any] | None:
    if not val:
        return None
    parts = val.split(",")
    out: dict[str, Any] = {"raw": val, "tex": parts[0]}
    for p in parts[1:]:
        if "x" in p and p[0].isdigit():
            w_s, h_s = p.split("x", 1)
            if w_s.isdigit() and h_s.isdigit():
                out["w"], out["h"] = int(w_s), int(h_s)
        elif p.startswith("f") and p[1:].isdigit():
            out["fmt"] = int(p[1:])
    return out


def parse_draws_tsv(path: str) -> list[dict[str, Any]]:
    draws: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("draw\t"):
                continue
            kv: dict[str, str] = {}
            for tok in line.split("\t")[1:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    kv[k] = v
            vp_parts = kv.get("vp", "0,0,0,0,0,1").split(",")
            vp_w = int(vp_parts[2]) if len(vp_parts) >= 4 and vp_parts[2].isdigit() else 0
            vp_h = int(vp_parts[3]) if len(vp_parts) >= 4 and vp_parts[3].isdigit() else 0
            draws.append(
                {
                    "seq": int(kv.get("seq", "0")),
                    "kind": kv.get("kind", ""),
                    "verts": int(kv.get("verts", "0")),
                    "prims": int(kv.get("prims", "0")),
                    "vs": kv.get("vs", ""),
                    "ps": kv.get("ps", ""),
                    "rt0": _parse_surface(kv.get("rt0", "0")),
                    "rt1": _parse_surface(kv.get("rt1", "0")),
                    "t0": _parse_tex(kv.get("t0", "")),
                    "rtback": int(kv.get("rtback", "0")),
                    "vp_w": vp_w,
                    "vp_h": vp_h,
                    "zen": int(kv.get("zen", "0")),
                    "zwr": int(kv.get("zwr", "0")),
                    "bl": int(kv.get("bl", "0")),
                    "src": int(kv.get("src", "0")),
                    "dst": int(kv.get("dst", "0")),
                    "bop": int(kv.get("bop", "0")),
                    "action": kv.get("action", "sent"),
                    "scene": int(kv.get("scene", "0")),
                    "handoffdone": int(kv.get("handoffdone", "0")),
                }
            )
    return draws


def audit_cutscene_draws(
    draws_tsv_path: str,
    composite_ps_hashes: set[str] | None = None,
    min_coverage_bound: float = MIN_COVERAGE_FOR_MEAN_JUMP,
) -> dict[str, Any]:
    draws = parse_draws_tsv(draws_tsv_path)
    if not draws:
        return {"status": "EMPTY_DUMP", "path": os.path.abspath(draws_tsv_path)}

    # Infer primary resolution and g_sceneHdrTarget (forward world A16B16G16R16F f113 without rt1)
    max_w = max((d["vp_w"] for d in draws), default=2560) or 2560
    max_h = max((d["vp_h"] for d in draws), default=1440) or 1440
    screen_area = float(max_w * max_h)

    scene_hdr_ptr: str | None = None
    scene_hdr_tex: str | None = None
    for d in draws:
        if d["handoffdone"] == 0 and d["rt0"] and d["rt1"] is None:
            rt0 = d["rt0"]
            if rt0.get("fmt") == 113 and rt0.get("w") == max_w and rt0.get("h") == max_h and d["scene"] == 1:
                scene_hdr_ptr = rt0["ptr"]
                scene_hdr_tex = rt0.get("tex")
    if scene_hdr_ptr is None:
        for d in draws:
            if d["handoffdone"] == 0 and d["rt0"] and d["rt1"] is None:
                rt0 = d["rt0"]
                if rt0.get("fmt") == 113 and rt0.get("w") == max_w and rt0.get("h") == max_h:
                    scene_hdr_ptr = rt0["ptr"]
                    scene_hdr_tex = rt0.get("tex")

    post_handoff = [d for d in draws if d["handoffdone"] == 1]
    awaiting_composite = scene_hdr_ptr is not None
    composite_seq: int | None = None
    class_a: list[dict[str, Any]] = []
    class_b: list[dict[str, Any]] = []
    modern_skipped: list[int] = []

    for d in post_handoff:
        if d["action"] != "sent":
            continue
        rt0_ptr = d["rt0"]["ptr"] if d["rt0"] else None
        t0_tex = d["t0"]["tex"] if d["t0"] else None
        reads_scene = bool(scene_hdr_tex and t0_tex and t0_tex == scene_hdr_tex)
        is_comp_ps = bool(composite_ps_hashes and d["ps"] in composite_ps_hashes)
        # In ModernFilter=1, composite is the first post-handoff full-screen LDR/backbuffer draw reading post/scene
        # or explicitly matching composite_ps_hashes.
        is_ldr_full = bool(
            d["rt0"]
            and d["rt0"].get("fmt") in (21, 22)
            and d["rt0"].get("w") == max_w
            and d["rt0"].get("h") == max_h
            and (d["rtback"] == 1 or d["bl"] == 0)
        )
        cov = (d["vp_w"] * d["vp_h"]) / screen_area if screen_area > 0 else 0.0
        summary_row = {
            "seq": d["seq"],
            "vs": d["vs"],
            "ps": d["ps"],
            "prims": d["prims"],
            "verts": d["verts"],
            "rt0_fmt": d["rt0"].get("fmt") if d["rt0"] else None,
            "rtback": d["rtback"],
            "vp_coverage": round(cov, 4),
            "bl": d["bl"],
            "src": d["src"],
            "dst": d["dst"],
            "bop": d["bop"],
        }
        if awaiting_composite:
            if is_comp_ps or (is_ldr_full and d["zen"] == 0 and d["bl"] == 0):
                composite_seq = d["seq"]
                awaiting_composite = False
                modern_skipped.append(d["seq"])  # replaced by PresentModernSceneToComposite
                continue
            if rt0_ptr != scene_hdr_ptr or reads_scene:
                modern_skipped.append(d["seq"])
                continue
            class_a.append(summary_row)
        else:
            class_b.append(summary_row)

    cov_a = sum(r["vp_coverage"] for r in class_a)
    cov_b = sum(r["vp_coverage"] for r in class_b)
    max_single_cov = max([r["vp_coverage"] for r in (class_a + class_b)], default=0.0)
    total_cov = cov_a + cov_b

    if total_cov < min_coverage_bound:
        verdict = "pass_through_falsified_coverage_too_small"
    elif cov_a >= min_coverage_bound and cov_b >= min_coverage_bound:
        verdict = "both_class_a_and_class_b_candidates"
    elif cov_a >= min_coverage_bound:
        verdict = "class_a_hdr_effects_into_scene_hdr"
    else:
        verdict = "class_b_post_composite_overlays"

    return {
        "path": os.path.abspath(draws_tsv_path),
        "screen_size": [max_w, max_h],
        "total_draws": len(draws),
        "post_handoff_draws": len(post_handoff),
        "scene_hdr_rt0": scene_hdr_ptr,
        "scene_hdr_tex": scene_hdr_tex,
        "composite_trigger_seq": composite_seq,
        "modern_skipped_count": len(modern_skipped),
        "min_coverage_bound": round(min_coverage_bound, 4),
        "class_a_count": len(class_a),
        "class_a_viewport_coverage_sum": round(cov_a, 4),
        "class_b_count": len(class_b),
        "class_b_viewport_coverage_sum": round(cov_b, 4),
        "max_single_draw_coverage": round(max_single_cov, 4),
        "verdict": verdict,
        "class_a_draws": class_a,
        "class_b_draws": class_b,
    }


def run_self_test() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # 1. Class A pass-through: additive HDR sun-glare quad into g_sceneHdrTarget before composite
        p_a = os.path.join(tmp, "f100_draws.tsv")
        with open(p_a, "w", encoding="utf-8") as f:
            f.write(
                "draw\tseq=1\tkind=DIP\tverts=400\tprims=200\tvs=11111111\tps=22222222"
                "\trt0=0xAA,2560x1440,f113,u1,ms0,tex0xBB\trt1=0\trtback=0\tvp=0,0,2560,1440,0,1"
                "\tzen=1\tzwr=1\tbl=0\tsrc=2\tdst=1\tbop=1\taction=sent\tscene=1\thandoffdone=0\n"
                "draw\tseq=2\tkind=DIP\tverts=4\tprims=2\tvs=33333333\tps=44444444"
                "\trt0=0xAA,2560x1440,f113,u1,ms0,tex0xBB\trt1=0\trtback=0\tvp=0,0,2560,1440,0,1"
                "\tzen=0\tzwr=0\tbl=1\tsrc=5\tdst=2\tbop=1\taction=sent\tscene=0\thandoffdone=1\n"
                "draw\tseq=3\tkind=DIP\tverts=4\tprims=2\tvs=55555555\tps=66666666"
                "\trt0=0xCC,2560x1440,f21,u1,ms0,tex0x0\trt1=0\trtback=1\tvp=0,0,2560,1440,0,1"
                "\tzen=0\tzwr=0\tbl=0\tsrc=2\tdst=1\tbop=1\taction=sent\tscene=0\thandoffdone=1\n"
            )
        r_a = audit_cutscene_draws(p_a)
        assert r_a["verdict"] == "class_a_hdr_effects_into_scene_hdr", r_a
        assert r_a["class_a_count"] == 1 and r_a["class_b_count"] == 0, r_a

        # 2. Class B pass-through: full-screen post-composite flash quad after composite
        p_b = os.path.join(tmp, "f101_draws.tsv")
        with open(p_b, "w", encoding="utf-8") as f:
            f.write(
                "draw\tseq=1\tkind=DIP\tverts=400\tprims=200\tvs=11111111\tps=22222222"
                "\trt0=0xAA,2560x1440,f113,u1,ms0,tex0xBB\trt1=0\trtback=0\tvp=0,0,2560,1440,0,1"
                "\tzen=1\tzwr=1\tbl=0\tsrc=2\tdst=1\tbop=1\taction=sent\tscene=1\thandoffdone=0\n"
                "draw\tseq=2\tkind=DIP\tverts=4\tprims=2\tvs=55555555\tps=66666666"
                "\trt0=0xCC,2560x1440,f21,u1,ms0,tex0x0\trt1=0\trtback=1\tvp=0,0,2560,1440,0,1"
                "\tzen=0\tzwr=0\tbl=0\tsrc=2\tdst=1\tbop=1\taction=sent\tscene=0\thandoffdone=1\n"
                "draw\tseq=3\tkind=DIP\tverts=4\tprims=2\tvs=77777777\tps=88888888"
                "\trt0=0xCC,2560x1440,f21,u1,ms0,tex0x0\trt1=0\trtback=1\tvp=0,0,2560,1440,0,1"
                "\tzen=0\tzwr=0\tbl=1\tsrc=5\tdst=2\tbop=1\taction=sent\tscene=0\thandoffdone=1\n"
            )
        r_b = audit_cutscene_draws(p_b)
        assert r_b["verdict"] == "class_b_post_composite_overlays", r_b
        assert r_b["class_b_count"] == 1, r_b

        # 3. Falsification: only a small 256x144 subtitle/HUD quad (< 1% coverage) runs post-handoff
        p_f = os.path.join(tmp, "f102_draws.tsv")
        with open(p_f, "w", encoding="utf-8") as f:
            f.write(
                "draw\tseq=1\tkind=DIP\tverts=400\tprims=200\tvs=11111111\tps=22222222"
                "\trt0=0xAA,2560x1440,f113,u1,ms0,tex0xBB\trt1=0\trtback=0\tvp=0,0,2560,1440,0,1"
                "\tzen=1\tzwr=1\tbl=0\tsrc=2\tdst=1\tbop=1\taction=sent\tscene=1\thandoffdone=0\n"
                "draw\tseq=2\tkind=DIP\tverts=4\tprims=2\tvs=55555555\tps=66666666"
                "\trt0=0xCC,2560x1440,f21,u1,ms0,tex0x0\trt1=0\trtback=1\tvp=0,0,2560,1440,0,1"
                "\tzen=0\tzwr=0\tbl=0\tsrc=2\tdst=1\tbop=1\taction=sent\tscene=0\thandoffdone=1\n"
                "draw\tseq=3\tkind=DIP\tverts=4\tprims=2\tvs=99999999\tps=AAAAAAAA"
                "\trt0=0xCC,2560x1440,f21,u1,ms0,tex0x0\trt1=0\trtback=1\tvp=100,1200,256,144,0,1"
                "\tzen=0\tzwr=0\tbl=1\tsrc=5\tdst=6\tbop=1\taction=sent\tscene=0\thandoffdone=1\n"
            )
        r_f = audit_cutscene_draws(p_f)
        assert r_f["verdict"] == "pass_through_falsified_coverage_too_small", r_f

    print("cutscene_draw_audit.py: ALL SELF-TESTS PASSED")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true", help="Run synthetic unit tests and exit.")
    ap.add_argument("--draws", default=None, help="Path to f<frame>_draws.tsv from CaptureDraws=1.")
    ap.add_argument("--composite-ps", action="append", default=[], help="Known composite PS hex hash(es).")
    args = ap.parse_args()

    if args.self_test:
        run_self_test()
        return 0
    if not args.draws:
        ap.error("Specify --self-test or --draws <f_draws.tsv>")
        return 2

    res = audit_cutscene_draws(args.draws, composite_ps_hashes={h.upper() for h in args.composite_ps} or None)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
