"""Test the two gate-slowdown leads and compare per-window GPU times across gate spots.

Usage:
  python -I check_gate_slowdown.py
"""
import glob
import json
import os
import pickle
import re

import numpy as np


def load_jsonl_fast(path):
    frames, ft, gpu, rr, integ, comp, vram, tbud, rt = [], [], [], [], [], [], [], [], []
    omm = []
    with open(path, "rb") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            frames.append(int(o.get("frame", -1)))
            ft.append(float(o.get("frameTimeMs", -1)))
            gpu.append(float(o.get("gpuTimeMs", -1)))
            vram.append(float(o.get("vramUsedMB", -1)))
            tbud.append(float(o.get("textureBudgetMB", -1)))
            rt.append(bool(o.get("rayTraced", False)))
            pm = o.get("passMs", {})
            rr.append(float(pm.get("InjectRTX/Ray Reconstruction", [-1])[0]))
            integ.append(float(pm.get("InjectRTX/Integrate Raytracing", [-1])[0]))
            comp.append(float(pm.get("InjectRTX/Composite", [-1])[0]))
            omm_keys = [v[0] for k, v in pm.items() if "Opacity" in k and v[0] >= 0]
            omm.append(max(omm_keys) if omm_keys else 0.0)
    return {
        "frame": np.array(frames),
        "ft": np.array(ft),
        "gpu": np.array(gpu),
        "rr": np.array(rr),
        "integ": np.array(integ),
        "comp": np.array(comp),
        "vram": np.array(vram),
        "tbud": np.array(tbud),
        "rt": np.array(rt),
        "omm": np.array(omm),
    }


def check_spot_windows(name, spot_dir):
    spot_json = json.load(open(os.path.join(spot_dir, "spot.json"), "r", encoding="utf-8"))
    cfg_path = os.path.join(spot_dir, "spot_config.json")
    cfg = json.load(open(cfg_path, "r", encoding="utf-8")) if os.path.exists(cfg_path) else {}
    t = load_jsonl_fast(os.path.join(spot_dir, "telemetry.jsonl"))
    rt = t["rt"]
    print(f"\n=== {name} (rt_frames={int(rt.sum())}, cfg_goggles={cfg.get('goggles')}) ===")
    print(
        f"  ALL RT: ft={np.median(t['ft'][rt]):.2f} ms, gpu={np.median(t['gpu'][rt]):.2f} ms, "
        f"RR={np.median(t['rr'][rt]):.2f} ms, Integ={np.median(t['integ'][rt]):.2f} ms, "
        f"Comp={np.median(t['comp'][rt]):.2f} ms, vram={np.median(t['vram'][rt]):.0f} MB (max {np.max(t['vram'][rt]):.0f}), "
        f"texBudget={np.median(t['tbud'][rt]):.0f} MB (min {np.min(t['tbud'][rt]):.0f})"
    )

    wins = spot_json.get("windows", [])
    # Check frame times immediately around each window boundary (where GateDump ini write + reload fires)
    dump_frames = []
    for w in wins:
        # GateDump fires at the end of each 'still' window
        if w.get("kind") == "still" and w.get("telemetry"):
            dump_frames.append((w["name"], int(w["telemetry"][1])))

    f_to_idx = {int(f): i for i, f in enumerate(t["frame"])}
    deltas = []
    for wname, ef in dump_frames:
        idx = f_to_idx.get(ef)
        if idx is None or idx < 5 or idx + 6 >= len(t["ft"]):
            continue
        before = float(np.median(t["ft"][idx - 5:idx]))
        around = float(np.max(t["ft"][idx - 1:idx + 4]))
        after = float(np.median(t["ft"][idx + 2:idx + 7]))
        deltas.append((wname, ef, before, around, after))

    if deltas:
        med_before = np.median([x[2] for x in deltas])
        med_peak = np.median([x[3] for x in deltas])
        max_peak = np.max([x[3] for x in deltas])
        med_after = np.median([x[4] for x in deltas])
        print(
            f"  GateDump/Reload boundary ({len(deltas)} stops): "
            f"5-frame med before={med_before:.2f} ms, peak around dump[-1..+3]={med_peak:.2f} ms (max {max_peak:.2f} ms), "
            f"5-frame med after={med_after:.2f} ms"
        )

    # Print time-series across windows (first 6, middle 4, last 6)
    sel = wins[:6] + wins[len(wins) // 2: len(wins) // 2 + 4] + wins[-6:]
    seen = set()
    for w in sel:
        wn = w.get("name")
        if wn in seen:
            continue
        seen.add(wn)
        tel = w.get("telemetry") or [-1, -1]
        sf, ef = int(tel[0]), int(tel[1])
        m = rt & (t["frame"] >= sf) & (t["frame"] <= ef)
        if m.sum() == 0:
            m_all = (t["frame"] >= sf) & (t["frame"] <= ef)
            print(f"    win {wn:22s} ({w.get('kind','?'):6s}) f={sf}..{ef} n_all={int(m_all.sum())} (no RT, med ft={np.median(t['ft'][m_all]) if m_all.any() else -1:.2f})")
            continue
        print(
            f"    win {wn:22s} ({w.get('kind','?'):6s}) f={sf:5d}..{ef:5d} n={int(m.sum()):4d}: "
            f"ft={np.median(t['ft'][m]):5.2f} RR={np.median(t['rr'][m]):5.2f} "
            f"Integ={np.median(t['integ'][m]):5.2f} Comp={np.median(t['comp'][m]):4.2f} "
            f"vram={np.median(t['vram'][m]):5.0f} tbud={np.median(t['tbud'][m]):4.0f}"
        )
    rr_rt = t["rr"][rt]
    fr_rt = t["frame"][rt]
    low_rr = rr_rt < 12.0
    high_rr = rr_rt >= 12.5
    mtime = os.path.getmtime(os.path.join(spot_dir, "telemetry.jsonl"))
    import datetime
    ts_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    print(f"  RR split (ended {ts_str}): <12.0ms={int(low_rr.sum())}, >=12.5ms={int(high_rr.sum())}")
    if low_rr.sum() > 20 and high_rr.sum() > 20:
        # Smooth with 5-frame median to find sustained regime shifts
        sm = np.array([np.median(rr_rt[max(0, i - 2):min(len(rr_rt), i + 3)]) for i in range(len(rr_rt))])
        state = (sm >= 12.2).astype(np.int8)
        diffs = np.where(np.diff(state) != 0)[0]
        for idx in diffs[:12]:
            i0, i1 = max(0, idx - 3), min(len(rr_rt), idx + 5)
            print(
                f"    RR regime shift at rt_idx={idx} frame={int(fr_rt[idx])} ({'LOW->HIGH' if state[idx+1] else 'HIGH->LOW'}): "
                f"rr={np.round(rr_rt[i0:i1],2).tolist()} ft={np.round(t['ft'][rt][i0:i1],2).tolist()} "
                f"vram={t['vram'][rt][i0:i1].astype(int).tolist()} tbud={t['tbud'][rt][i0:i1].astype(int).tolist()}"
            )


def check_dxvk_logs():
    print("\n=== Opacity Micromap & Vidmem lines across all rotated remix-dxvk.log files ===")
    paths = sorted(glob.glob(r"D:\BlacklistOverhaul\experiments\playtest-evidence\**\rtx-remix_logs_remix-dxvk.log", recursive=True))
    paths += sorted(glob.glob(r"D:\BlacklistOverhaul\tools\remix\play-setup\logs\**\rtx-remix_logs_remix-dxvk.log", recursive=True))
    for p in paths:
        txt = open(p, "r", errors="replace").read()
        mem = re.findall(r"Memory: [^\n]+", txt)
        omm_zero = "Free Vidmem dropped below a limit" in txt
        omm_oom = "Out of memory budget" in txt
        dev_lost = "VK_ERROR_DEVICE_LOST" in txt
        rel = os.path.relpath(p, r"D:\BlacklistOverhaul")
        print(f"  {rel:85s} | OMM_budget0={str(omm_zero):5s} OMM_oom={str(omm_oom):5s} DEV_LOST={str(dev_lost):5s} | {mem[0] if mem else ''}")


if __name__ == "__main__":
    check_dxvk_logs()
    for label, sdir in [
        ("20261114_con1", r"D:\BlacklistOverhaul\experiments\remix-gate\20261114-153401\consumption_1"),
        ("20261114_con2", r"D:\BlacklistOverhaul\experiments\remix-gate\20261114-153401\consumption_2"),
        ("20261115_1405_pal1", r"D:\BlacklistOverhaul\experiments\remix-gate\20261115-140515\paladin_1"),
        ("20261115_1405_cust", r"D:\BlacklistOverhaul\experiments\remix-gate\20261115-140515\con_custodial"),
        ("20261115_1426_ghall", r"D:\BlacklistOverhaul\experiments\remix-gate\20261115-142638\con_green_hall"),
        ("20261115_1426_bhall", r"D:\BlacklistOverhaul\experiments\remix-gate\20261115-142638\con_bright_hall"),
        ("20261115_1426_pal1", r"D:\BlacklistOverhaul\experiments\remix-gate\20261115-142638\paladin_1"),
    ]:
        if os.path.exists(os.path.join(sdir, "telemetry.jsonl")):
            check_spot_windows(label, sdir)
