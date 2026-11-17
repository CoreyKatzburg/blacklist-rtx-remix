"""Synthetic verify_runtime runs, in exactly the format drive.py writes, for gate.py's tests and the dry run.

  fakerun.py <folder>   writes one good run and one run per broken case under <folder>, evaluates each with gate.py,
                        prints what happened, exits 1 unless the good run passes and every broken run fails on the
                        check it breaks (and on nothing it should not).

The fake world: a fixed 360-degree panorama seen through the 128x72 luma thumbnail (the camera's yaw picks the
crop, pitch shifts it vertically), tiny per-frame noise, a camera trace that follows the requested pose with the
proxy's sign convention flipped (so the gate must learn it), and depth maps with a Sam-shaped near blob.
"""
from __future__ import annotations

import base64
import json
import math
import os
import shutil
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate  # noqa: E402
import display_state  # noqa: E402

TW, TH = gate.THUMB_W, gate.THUMB_H
PX_PER_DEG = 2.0
CAP_W, CAP_H = 64, 36


def panorama(seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    coarse = rng.uniform(30, 220, (12, 72))
    big = np.kron(coarse, np.ones((12, 10)))               # 144 x 720: 360 deg at 2 px/deg, room for pitch
    k = np.ones(5) / 5
    big = np.apply_along_axis(lambda r: np.convolve(r, k, "same"), 1, big)
    return big + rng.normal(0, 6, big.shape)


PANO = panorama()


def view(yaw: float, pitch: float, rng) -> np.ndarray:
    x0 = int(round(yaw * PX_PER_DEG)) % PANO.shape[1]
    y0 = int(round(36 - pitch * 0.5))
    cols = (np.arange(TW) + x0) % PANO.shape[1]
    img = PANO[y0:y0 + TH][:, cols]
    return np.clip(img + rng.normal(0, 0.8, img.shape), 0, 255)


# case -> (frame time ms, GPU time ms) for every frame. Healthy runs are 10 ms and 8 ms: 100 FPS, CPU wait 2 ms.
FRAME_TIMES = {
    "low_fps": (14.0, 12.0),           # 71 FPS: WARN under the 90 target, the gate still passes
    "near_target_fps": (1000.0 / 89.96, 8.0),    # 89.96 FPS: WARN, must print "89.96 FPS" not "90.0 FPS"
    "near_target_89_996": (1000.0 / 89.996, 8.0), # 89.996 FPS: WARN, must print "89.996 FPS" not "90.00 FPS"
    "never_ray_traced": (35.0, 5.0),               # 28.6 FPS, 30 ms CPU wait with rayTraced=false -> compile_wait_stuck
    "never_rt_no_compiles": (35.0, 5.0),           # same frames with remixShaderCompiles=0 -> fails never_ray_traced
    "high_latency": (17.5, 9.0),       # 57 FPS, CPU wait 8.5 ms -> fails latency_overhead_ms
    "fps_regression": (11.8, 8.0),     # 84.7 FPS: 15% below the 100 FPS previous run: FAIL
    "fps_drop_9pct": (10.98, 8.0),     # 91 FPS: 9% below the previous run: passes
}


class Builder:
    """Writes one spot: windows, telemetry, proxy trace/gate CSVs, dumps, captures."""

    def __init__(self, folder: str, name: str, case: str):
        self.dir = os.path.join(folder, name)
        os.makedirs(os.path.join(self.dir, "gate"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "frames"), exist_ok=True)
        self.name, self.case = name, case
        self.rng = np.random.default_rng(1)
        self.tframe = self.pframe = 1000
        self.clk, self.prev_pms = 1000.0, 0.5
        self.telemetry, self.proxy, self.pgate, self.windows = [], [], [], []
        self.yaw = self.pitch = 0.0
        self.base_cam = 40.0   # the proxy's yaw for the reference pose (any value)

    def frame(self, kind: str, wname: str, i: int, goggles: bool = False) -> None:
        self.tframe += 1; self.pframe += 1
        img = view(self.yaw, self.pitch, self.rng)
        c = self.case
        pms = 120.0 if (c == "present_stall" and kind == "pan" and i == 30) else 0.5
        self.clk += max(10.0, self.prev_pms + 2.0)
        self.prev_pms = pms
        rec = {"frame": self.tframe, "qpcMs": 0.0 if c == "no_qpc_ms" else round(self.clk - 1.0, 3),
               "frameTimeMs": 10.0, "gpuTimeMs": 8.0, "instances": 900, "skinnedInstances": 3,
               "staticBlasRebuilds": 0, "staticBlasBuilds": 0,
               "rayTraced": not (goggles and wname.endswith("_rtoff") and i < 3),
               "cameraCut": False, "textureMemoryMB": 1800, "vramUsedMB": 6100,
               "skinnedTriangles": 60000, "inwardSkinnedTriangles": 12, "thumbNonFinite": 0,
               "zeroNormalSkinnedTriangles": 0, "zeroNormalInstances": 0,
               "zeroNormalStaticTriangles": 0, "zeroNormalStaticInstances": 0}
        # recorder fields (gate.recorder_checks): healthy values, with one fault per case below
        rec.update({"replacedMaterialDraws": 354, "legacyMaterialDraws": 25, "texturesDemoted": 0,
                    "skips": {"occlusionQuery": 300},
                    "passMs": {"InjectRTX": [10.0, 4.0, 1], "InjectRTX/Ray Reconstruction": [5.0, 0.2, 1],
                               "InjectRTX/Ray Reconstruction/Prepare DLSS": [0.3, 0.02, 1]}})
        c = self.case
        if c in ("never_ray_traced", "never_rt_no_compiles"):
            rec["rayTraced"] = False
        if c == "never_rt_no_compiles":
            rec["remixShaderCompiles"] = 0
        if c == "one_stop_not_ray_traced" and wname == "y0_p0":
            rec["rayTraced"] = False
        if c == "pan_not_ray_traced" and kind == "pan":
            rec["rayTraced"] = False
        if c == "goggles_off_only" and goggles and i < 3:
            rec["rayTraced"] = False
        if c == "nan_frame" and kind == "still" and i == 3: rec["thumbNonFinite"] = 40
        if c == "goggles_toggle_rebuilds" and wname == "goggles1_rtoff" and i == 3: rec["staticBlasBuilds"] = 300
        if c == "goggles_dark_frame" and wname == "goggles1_rtoff" and i == 3: img = img * 0.2
        if c in ("slow_frame", "noremix_scenedraws0") and kind == "pan" and i == 30: rec["frameTimeMs"] = 120.0 if c == "noremix_scenedraws0" else 61.0
        if c in ("missing_cam_col", "cut_short_cam_col") and wname == "to_y45_p0" and i == 2: rec["frameTimeMs"] = 120.0
        if c == "load_count_mismatch" and wname == "to_y45_p0" and i in (0, 1): rec["frameTimeMs"] = 120.0
        if c == "slow_beside_nonload" and wname == "to_y45_p0" and i == 0: rec["frameTimeMs"] = 120.0
        loading = ((c == "loading_in_window" and wname == "to_y45_p0" and i in (2, 3))
                   or (c == "load_count_mismatch" and wname == "to_y45_p0" and i == 0))
        if c == "loading_in_window" and loading: rec["frameTimeMs"] = 120.0
        if c in FRAME_TIMES: rec["frameTimeMs"], rec["gpuTimeMs"] = FRAME_TIMES[c]
        if c in ("no_skinned", "proxy_no_char_ok") and kind == "still" and i == 3: rec["skinnedInstances"] = 0
        if c == "goggles_no_char_ok" and wname == "goggles1_rtoff" and i == 1: rec["skinnedInstances"] = 0
        if c == "zero_normal_skinned_tris" and kind == "still" and i == 3: rec["zeroNormalSkinnedTriangles"] = 10
        if c == "zero_normal_inst" and kind == "still" and i == 3: rec["zeroNormalInstances"] = 2
        if c == "zero_normal_static_tris" and kind == "still" and i == 3: rec["zeroNormalStaticTriangles"] = 25
        if c == "zero_normal_static_inst" and kind == "still" and i == 3: rec["zeroNormalStaticInstances"] = 3
        if c == "blas_rebuild" and kind == "pan" and i == 50: rec["staticBlasRebuilds"] = 1
        if c == "inward" and kind == "move" and i == 2: rec["inwardSkinnedTriangles"] = 900
        if c == "flash_pan" and kind == "pan" and i == 40: img = np.clip(img + 70, 0, 255)
        if c == "pop_still" and wname == "y90_p0" and i == 7: img[20:40, 40:64] = 255 - img[20:40, 40:64]
        if c == "flicker_still" and wname == "y90_p0" and i % 2 == 1:
            img[20:36, 40:56] = np.clip(img[20:36, 40:56] + 10, 0, 255)
        if c == "pulse_still" and wname == "y90_p0":
            img = np.clip(img + 12.0 * math.sin(i * 2.0 * math.pi / 12.0), 0, 255)
        if c == "local_pulse_still" and wname == "y90_p0" and 6 <= i < 11:
            img[16:32, 40:64] = np.clip(img[16:32, 40:64] + 32.0, 0, 255)
        if c == "sparse_strobe_still" and wname == "y90_p0" and i >= 2 and (i - 2) % 4 == 0:
            img[16:32, 40:56] = np.clip(img[16:32, 40:56] + 10.0, 0, 255)
        if c == "untimed_steps" and kind == "still" and i % 5 == 0:
            rec["passMs"]["InjectRTX/Ray Reconstruction"][0] = -1.0          # the recorder's sentinel: never timed
        if c == "child_over_parent" and kind == "move" and i == 2:
            rec["passMs"]["InjectRTX/Ray Reconstruction/Prepare DLSS"][0] = 6.0   # 1.0 ms over its parent's 5.0
        if c == "child_timer_jitter" and kind == "move" and i == 2:
            rec["passMs"]["InjectRTX/Ray Reconstruction/Prepare DLSS"][0] = 5.4   # 0.4 ms over: within tolerance
        if c == "occlusion_heavy": rec["skips"] = {"occlusionQuery": 975}          # 65% of the 1500 proxy draws
        if c in ("other_skips_6pct", "other_skips_4pct") and kind == "move":
            rec["skips"]["unsupportedTopology"] = 90 if c == "other_skips_6pct" else 60   # 6% and 4% of 1500 draws
        if c == "no_mod_materials": rec["replacedMaterialDraws"], rec["legacyMaterialDraws"] = 0, 379
        if c == "demoted_textures" and kind == "still" and i % 2 == 0: rec["texturesDemoted"] = 1
        if c == "renamed_fields":
            rec["frameMs"] = rec.pop("frameTimeMs")
        rec["lumaThumb"] = base64.b64encode(img.astype(np.uint8).tobytes()).decode()
        if not (c == "sparse_telemetry" and self.tframe % 15) and not (c == "stop_missing_telemetry" and wname == "y45_p0"):
            self.telemetry.append(rec)
        # proxy camera trace: sign of yaw flipped against the request, pitch kept
        cy = math.radians(self.base_cam - self.yaw + (10.0 if c == "pose_off" and wname == "y135_p0" else 0.0))
        cp = math.radians(self.pitch)
        if c == "pan_short" and kind == "pan":
            cy = math.radians(self.base_cam - self.yaw * 0.5)
        if loading:
            self.proxy.append(f"{self.pframe},{self.clk:.3f},1.6,{pms},29,0,")
            self.pgate.append(f"{self.pframe},0,0,0,0,1")
        elif c == "missing_cam_col" and wname == "to_y45_p0" and i == 2:
            self.proxy.append(f"{self.pframe},{self.clk:.3f},14.0,{pms},1500,1")  # 6 columns: no camera column -> unknown
            self.pgate.append(f"{self.pframe},1400,40,1,{1 if goggles else 0},1")
        elif c == "cut_short_cam_col" and wname == "to_y45_p0" and i == 2:
            self.proxy.append(f"{self.pframe},{self.clk:.3f},14.0,{pms},1500,1,G:0:0:1.")  # cut short in camera column -> unknown
            self.pgate.append(f"{self.pframe},1400,40,1,{1 if goggles else 0},1")
        elif c == "short_row_in_window" and wname == "to_y45_p0" and i == 2:
            self.proxy.append(f"{self.pframe},{self.clk:.3f},14.0")  # cut-short 3-col row inside scene window -> NOT MEASURED
            self.pgate.append(f"{self.pframe},1400,40,1,{1 if goggles else 0},1")
        else:
            fx, fy, fz = math.cos(cp) * math.cos(cy), math.cos(cp) * math.sin(cy), math.sin(cp)
            gap = 61.0 if (c == "frame_gap" and kind == "pan" and i == 30) else 14.0   # long gap, short Present
            self.proxy.append(f"{self.pframe},{self.clk:.3f},{gap},{pms},1500,1,P:22:0:1.0:2.0:3.0:{fx:.5f}:{fy:.5f}:{fz:.5f}:0.0")
            handoff = 0 if (c == "raster_flash" and kind == "pan" and i == 70) else 1
            sdraws = 0 if c == "noremix_scenedraws0" else 1400
            skdraws = 0 if (c == "proxy_no_char_ok" and kind == "still" and i == 3) else 40
            self.pgate.append(f"{self.pframe},{sdraws},{skdraws},{handoff},{1 if goggles else 0},1")
        return img

    def window(self, name: str, kind: str, n: int, extra=None, goggles=False, motion=None):
        start = (self.tframe, self.pframe)
        imgs = []
        for i in range(n):
            if motion:
                motion(i / max(1, n - 1))
            imgs.append(self.frame(kind, name, i, goggles))
        if self.case == "load_count_mismatch" and name == "to_y45_p0":
            self.tframe += 1
            rec = dict(self.telemetry[-1], frame=self.tframe, frameTimeMs=10.0)
            self.telemetry.append(rec)
            w = {"name": name, "kind": kind, "yaw": self.yaw, "pitch": self.pitch,
                 "telemetry": [start[0], self.tframe], "proxy": [start[1], self.pframe], **(extra or {})}
            self.pframe += 1
        elif self.case == "slow_missing_proxy" and name == "to_y45_p0":
            self.tframe += 1
            rec = dict(self.telemetry[-1], frame=self.tframe, frameTimeMs=120.0)
            self.telemetry.append(rec)
            w = {"name": name, "kind": kind, "yaw": self.yaw, "pitch": self.pitch,
                 "telemetry": [start[0], self.tframe], "proxy": [start[1], self.pframe], **(extra or {})}
            self.pframe += 1
        else:
            w = {"name": name, "kind": kind, "yaw": self.yaw, "pitch": self.pitch,
                 "telemetry": [start[0], self.tframe], "proxy": [start[1], self.pframe], **(extra or {})}
        self.windows.append(w)
        return w, imgs

    def capture(self, rel: str, imgs: list[np.ndarray], every: int = 1) -> None:
        small = [gate.nearest_resize(im, (CAP_H, CAP_W)) for im in imgs[::every]]
        np.array(small, np.uint8).tofile(os.path.join(self.dir, rel))

    def dumps(self, tag: str, outline: bool) -> None:
        c = self.case
        if c == "no_dumps":
            return
        h, w = 36, 64
        yy, xx = np.mgrid[0:h, 0:w]
        pattern = 0.08 * (((yy // 2 + xx // 2) % 2).astype(np.float32) - 0.5)
        color = np.clip(0.4 + pattern[..., None], 0.05, 0.95).repeat(3, axis=2).astype(np.float32)
        if c == "blurry_angles" and abs(self.pitch) > 0:
            color = np.full((h, w, 3), 0.4, np.float32)
        else:
            color[5:8, 5:30] = (1.0, 1.0, 0.98)             # white clipped highlight: allowed
        if c == "too_dark":
            color *= 0.02
        if c == "too_dark_vs_raster":
            color = np.clip(0.14 + pattern[..., None], 0.05, 0.95).repeat(3, axis=2).astype(np.float32)
        if c == "too_bright_vs_raster":
            color = np.clip(0.88 + pattern[..., None], 0.05, 0.95).repeat(3, axis=2).astype(np.float32)
        if c == "too_white_vs_raster":
            color = np.full((h, w, 3), 0.20, np.float32)       # mean ~0.43 (~1.07x), 30% ROI slab at 0.96 -> nearWhite +0.28
            color[6:15, :] = 0.96
        if c == "nan_pixel" and tag == "y45_p0":
            color[10, 10] = np.nan
        if c == "neon" and tag == "y180_p30":
            color[20:25, 0:10] = (1.6, 0.1, 1.4)             # 50 px of 2304 = 2% neon magenta
        gate.write_pfm(os.path.join(self.dir, "gate", f"{tag}_final.pfm"), color)
        if c == "good":
            gate.write_pfm(os.path.join(self.dir, "gate", f"{tag}_raster.pfm"), color)
        if outline:
            depth = np.full((144, 256), 2000.0, np.float32)
            depth[30:110, 100:150] = 250.0                    # Sam, full-res game depth
            depth[:8, :] = np.inf                             # sky
            gate.write_pfm(os.path.join(self.dir, "gate", f"{tag}_gamedepth.pfm"), depth)
            rtx = gate.nearest_resize(depth, (72, 128)).copy()   # Remix at half resolution
            if c == "sam_missing" and tag.endswith("after"):
                rtx[rtx < 300] = 2000.0
            gate.write_pfm(os.path.join(self.dir, "gate", f"{tag}_depth.pfm"), rtx)

    def move(self, name, yaw, pitch, n=6):
        y0, p0 = self.yaw, self.pitch

        def m(t):
            self.yaw, self.pitch = y0 + (yaw - y0) * t, p0 + (pitch - p0) * t
        self.window(name, "move", n, motion=m)

    def stop(self, tag, outline=None):
        self.window(tag + "_settle", "settle", 4)
        rel = f"frames/{tag}.gray"
        w, imgs = self.window(tag, "still", 18, {"frames": rel, "frameSize": [CAP_W, CAP_H], "dump": tag,
                                                 **({"outline": outline} if outline else {})})
        self.capture(rel, imgs)
        self.dumps(tag, bool(outline))

    def build(self) -> None:
        stops = [(y * 45.0, p) for p in (0.0, -30.0, 30.0) for y in range(8)]
        self.window("to_start", "move", 4)
        for i, (y, p) in enumerate(stops):
            if i:
                self.move(f"to_y{y:.0f}_p{p:.0f}", y, p)
            self.stop(f"y{y:.0f}_p{p:.0f}", "before" if i == 0 else None)
        self.move("to_pan", 0.0, 0.0)

        def pan(t):
            self.yaw = 360.0 * t
        rel = "frames/pan.gray"
        w, imgs = self.window("pan360", "pan", 180, {"frames": rel, "frameSize": [CAP_W, CAP_H]}, motion=pan)
        self.capture(rel, imgs, every=2 if self.case == "half_rate_capture" else 1)
        self.yaw = 0.0
        self.stop("y0_p0_after", "after")
        for mode in (1, 2, 3):
            for rt in ("on", "off"):
                rel = f"frames/goggles{mode}_{rt}.gray"
                w, imgs = self.window(f"goggles{mode}_rt{rt}", "goggles", 15, goggles=True,
                                      extra={"goggles": {"mode": mode, "rt": rt, "shot": rel, "frameSize": [CAP_W, CAP_H],
                                                         "toggled": not (self.case == "goggles_untoggled" and mode == 2)}})
                tint = [im * 0.5 + 60 for im in imgs]          # goggles: darker and shifted, same for RT on and off
                if self.case == "goggles_missing" and mode == 3 and rt == "on":
                    tint = [np.full_like(im, 90.0) for im in tint]
                self.capture(rel, tint)
        spot = {"name": self.name, "mission": "fake", "nearField": 300, "error": None, "windows": self.windows,
                "telemetry": None if self.case == "no_telemetry" else "telemetry.jsonl",
                "proxyFrames": "proxy_frames.csv", "proxyGate": None if self.case == "no_proxy_gate" else "proxy_gate.csv",
                "proxyLog": "proxy.log"}
        if self.case != "no_mouse_lag":
            bump = 5.0 if self.case == "lag_within_margin" else (5.5 if self.case == "lag_over_margin" else 0.0)
            # the bump goes on the game share (trials gameMs), which input_latency_ms judges; the screen share
            # (samples_ms) is only printed under input_latency_screen_ms
            trials = [{"screenMs": 22.0, "gameMs": round(14.0 + bump + ((i % 3) - 1) * 0.5, 2)} for i in range(20)]
            spot["mouse_lag"] = {"samples_ms": [22.0] * 20, "game_share_ms": round(14.0 + bump, 2), "after_game_ms": 8.0,
                                 "trials": trials, "method": "fake 20x5deg gdigrab"}
        if self.case == "spot_error":
            spot["error"] = "RuntimeError: loaded map 'menu', checkpoint was recorded in 'S_CON_Global'"
        if self.case == "no_thumbs":
            for r in self.telemetry:
                r.pop("lumaThumb", None)
        if self.case == "half_rate_capture":
            for r in self.telemetry:
                r.pop("lumaThumb", None)
        if self.case == "cut_short_proxy":
            self.proxy.append(f"{self.pframe + 1},1.0,14.0")
        if self.case == "quit_present_stall":
            self.clk += 10.0
            self.proxy.append(f"{self.pframe + 1},{self.clk:.3f},14.0,1500.0,1500,1,P:22:0:1.0:2.0:3.0:1.0:0.0:0.0:0.0")
        if self.case == "clock_frame_shift":
            self.proxy = [f"{int(ln.split(',', 1)[0]) + 1}," + ln.split(',', 1)[1] for ln in self.proxy]
            for w in self.windows:
                w["proxy"] = [w["proxy"][0] + 1, w["proxy"][1] + 1]
        gate.save_text(os.path.join(self.dir, "telemetry.jsonl"), "".join(json.dumps(r) + "\n" for r in self.telemetry))
        if self.case == "launch_rt_delay":
            lrecs = [{"frame": k, "frameTimeMs": 100.0, "gpuTimeMs": 5.0, "rayTraced": False,
                      "remixShaderCompiles": 3} for k in range(1, 21)]
            lrecs.append({"frame": 21, "frameTimeMs": 10.0, "gpuTimeMs": 8.0, "rayTraced": True,
                          "remixShaderCompiles": 0})
            gate.save_text(os.path.join(self.dir, "telemetry_launch.jsonl"),
                           "".join(json.dumps(r) + "\n" for r in lrecs))
        gate.save_text(os.path.join(self.dir, "proxy_frames.csv"), "\n".join(self.proxy) + "\n")
        gate.save_text(os.path.join(self.dir, "proxy_gate.csv"), "\n".join(self.pgate) + "\n")
        if self.case != "no_quit_log":
            lost = "[error] vkQueueSubmit: VK_ERROR_DEVICE_LOST\n" if self.case == "device_lost" else ""
            gate.save_text(os.path.join(self.dir, "remix-dxvk.log"), "[info] Remix shutdown\n" + lost)
            b32 = "[00:01:00.000] info:  Bridge client started\n"
            if self.case != "no_terminate":
                b32 += "[00:02:00.000] info:  Sending Terminate command to server...\n"
            gate.save_text(os.path.join(self.dir, "bridge32.log"), b32)
        s_val = 109 if self.case == "standins_ignored" else 0
        um_val = 15 if self.case == "unplaced_balls" else 0
        mm_val = 3 if self.case == "moving_balls" else 0
        xm_val = 80 if self.case == "lamps_no_flare_jump" else 59
        tm_val = 700 if self.case == "dimmed_linger" else 10
        # Worker B's newer fields (c78eb25, fbe011f): key values in the settings line, totals on the stray line.
        shape, keep, gear_skip = LP_KEYS.get(self.case, (0, 0, 1))
        gear, flips, recreated = LP_COUNTS.get(self.case, (0, 0, 0))
        new_cfg = f"weapon by shape {shape}, keep tracked {keep}, skip sam gear {gear_skip}, "
        new_tail = f" on sam gear {gear} (max {gear}) weapon flips {flips} recreated {recreated}"
        if self.case == "old_lightpub_format":       # proxy before c78eb25: none of the new fields
            new_cfg, new_tail = "", ""
        plog = (
            "lightpub: loaded (scale 1, sun 1, fill 1, max 20, flare stand-ins 0, trace 1, "
            f"{new_cfg}stand-in skip unplaced 1, skip moving 1)\n"
            f"lightpub: stray frame 120 lamps 120 without flare {xm_val} (max {xm_val}) flares 61 "
            f"stand-ins {s_val} on unplaced {um_val} (max {um_val}) on moving {mm_val} (max {mm_val}) "
            f"dimmed 2 (max 2) longest dim {tm_val} frames (max {tm_val}){new_tail}\n"
        )
        stray = ("lightpub: stray frame {} lamps 120 without flare 59 (max 59) flares 61 stand-ins 0 on unplaced 0 "
                 "(max 0) on moving 0 (max 0) dimmed 2 (max 2) longest dim 10 frames (max 10) on sam gear {} (max {}) "
                 "weapon flips {} recreated 0\n")
        if self.case == "lightpub_missed_notice":   # a later reload turns skip sam gear on; the line after the
            plog += ("lightpub: settings reloaded after a missed notice (scale 1, sun 1, fill 1, max 20, flare "
                     "stand-ins 0, trace 1, weapon by shape 0, keep tracked 0, skip sam gear 1, stand-in skip "
                     "unplaced 1, skip moving 1)\n"   # reload's window (frame 720) is excused, frame 1320 breaks it
                     + stray.format(720, 4, 4, 0) + stray.format(1320, 4, 4, 0))
        if self.case == "weapon_flips_reload_window":   # run 20261116-102658: shape 0 -> 2 mid-window, 1 flip on the
            plog += ("lightpub: settings reloaded (scale 1, sun 1, fill 1, max 20, flare stand-ins 0, trace 1, "
                     "weapon by shape 2, keep tracked 0, skip sam gear 1, stand-in skip unplaced 1, skip moving 1)\n"
                     + stray.format(720, 0, 0, 1) + stray.format(1320, 0, 0, 0))   # line spanning it, 0 after
        gate.save_text(os.path.join(self.dir, "proxy.log"), plog)
        trows = ["frame,node,id,change,old,new", "100,0x1,1,started,0.0,100.0"]
        if self.case == "switchoff_lag":
            val = 100.0
            for fr in range(120, 781, 60):
                trows.append(f"{fr},0x1,1,brightness,{val:.4f},{val * 0.5:.4f}")
                val *= 0.5
            trows.append(f"800,0x1,1,stopped,{val:.4f},0")
        else:
            trows.append("130,0x1,1,stopped,100.0,0")
        gate.save_text(os.path.join(self.dir, "lightpub_trace.csv"), "\n".join(trows) + "\n")
        gate.save_json(os.path.join(self.dir, "spot.json"), spot)


# case -> (weapon by shape, keep tracked, skip sam gear) and (on sam gear max, weapon flips, recreated) in proxy.log;
# other cases get (0, 0, 1) and (0, 0, 0)
LP_KEYS = {"sam_gear_noskip": (0, 0, 0), "weapon_flips_shape1": (1, 0, 1), "weapon_flips_shape2": (2, 0, 1),
           "recreated_kept_40": (0, 1, 1), "recreated_kept_1": (0, 1, 1)}
LP_COUNTS = {"sam_gear_skip_ignored": (11, 0, 0), "sam_gear_noskip": (11, 0, 0), "weapon_flips_shape1": (0, 3, 0),
             "weapon_flips_shape2": (0, 3, 0), "weapon_flips_shape0": (0, 3, 0), "recreated_kept_40": (0, 0, 40),
             "recreated_kept_1": (0, 0, 1), "recreated_untracked_150": (0, 0, 150)}


# checks that read telemetry the recorder writes per frame: NOT MEASURED whenever that telemetry is missing
RECORDER = ("step_timing", "skip_occlusion_share", "skip_other_share", "material_share", "textures_demoted")

_ZERO_NORMALS = ("zero_normal_skinned_triangles", "zero_normal_instances",
                 "zero_normal_static_triangles", "zero_normal_static_instances")

# case -> (checks that must FAIL, checks that must be NOT MEASURED); everything else must PASS.
CASES = {
    "good": ((), ()),
    "nan_pixel": (("nan_pixels", "scene_brightness"), ()),
    "nan_frame": (("nonfinite_thumb_pixels",), ()),
    "neon": (("neon_fraction",), ()),
    "no_skinned": (("skinned_min", "remix_dropped_character_frames"), ()),
    "proxy_no_char_ok": ((), ()),
    "goggles_no_char_ok": ((), ()),
    "clock_frame_shift": (("frame_join_clock",), ()),
    "no_qpc_ms": ((), ("frame_join_clock",)),
    "zero_normal_skinned_tris": (("zero_normal_skinned_triangles",), ()),
    "zero_normal_inst": (("zero_normal_instances",), ()),
    "zero_normal_static_tris": (("zero_normal_static_triangles",), ()),
    "zero_normal_static_inst": (("zero_normal_static_instances",), ()),
    "never_ray_traced": (("remix_ray_tracing", "remix_compile_stall", "compile_wait_stuck", "latency_overhead_ms"),
                         ("nan_pixels", "neon_fraction", "scene_brightness", "angled_sharpness",
                          "outline_overlap", "flashes", "still_flicker",
                          "goggles_toggle_rebuilds", "goggles_dark_frame")),
    "never_rt_no_compiles": (("remix_ray_tracing", "remix_compile_stall", "never_ray_traced", "latency_overhead_ms"),
                             ("nan_pixels", "neon_fraction", "scene_brightness", "angled_sharpness",
                              "outline_overlap", "flashes", "still_flicker",
                              "goggles_toggle_rebuilds", "goggles_dark_frame")),
    "goggles_off_only": ((), ()),
    "one_stop_not_ray_traced": (("remix_ray_tracing",),
                                ("nan_pixels", "neon_fraction", "scene_brightness", "angled_sharpness",
                                 "outline_overlap", "flashes", "still_flicker")),
    "pan_not_ray_traced": (("remix_ray_tracing",), ()),
    "launch_rt_delay": ((), ()),
    "stop_missing_telemetry": ((), ("frames_over_50ms", "min_median_fps", "latency_overhead_ms",
                                    "skinned_min", "remix_dropped_character_frames",
                                    "inward_fraction", "nonfinite_thumb_pixels",
                                    "remix_ray_tracing",
                                    "nan_pixels", "neon_fraction", "scene_brightness", "angled_sharpness",
                                    "flashes", "still_flicker", *_ZERO_NORMALS, *RECORDER)),
    "sam_missing": (("outline_overlap",), ()),
    "inward": (("inward_fraction",), ()),
    "raster_flash": (("raster_fallback_frames",), ()),
    "flash_pan": (("flashes",), ()),
    "pop_still": (("flashes",), ()),
    "flicker_still": (("still_flicker",), ()),
    "pulse_still": (("still_flicker",), ()),
    "local_pulse_still": (("still_flicker",), ()),
    "sparse_strobe_still": (("still_flicker",), ()),
    "blas_rebuild": (("static_blas_rebuilds",), ()),
    "slow_frame": (("frames_over_50ms",), ()),
    "loading_in_window": ((), ()),                        # >50 ms frames during a loading screen are excluded: passes
    "missing_cam_col": (("frames_over_50ms",), ()),       # 6-column proxy row (no camera column) is unknown -> fails
    "cut_short_cam_col": (("frames_over_50ms",), ()),     # cut-short camera column text is unknown -> fails
    "short_row_in_window": ((), ("skip_occlusion_share", "skip_other_share", "present_stalls")),  # 3 columns: gap measured
    "cut_short_proxy": ((), ()),                          # last proxy row cut short mid-write: finishes and passes
    "load_count_mismatch": (("frames_over_50ms",), ()),   # 7 tel vs 6 proxy: 1 loading frame excuses only t0, t1 fails
    "slow_missing_proxy": (("frames_over_50ms",), ()),    # slow frame whose frame number is absent from proxy -> fails
    "noremix_scenedraws0": (("frames_over_50ms",), ()),   # noRemix=True: sceneDraws=0 ignored, real camera + 120ms -> fails
    "low_fps": ((), ()),                                # WARN min_median_fps (WARNS); the gate passes
    "near_target_fps": ((), ()),                        # 89.96 FPS: WARN, must print 89.96 FPS not 90.0 FPS
    "near_target_89_996": ((), ()),                     # 89.996 FPS: WARN, must print 89.996 FPS not 90.00 FPS
    "high_latency": (("latency_overhead_ms",), ()),
    "lag_within_margin": ((), ()),                      # +5 ms over the reference: the margin's edge still passes
    "lag_over_margin": (("input_latency_ms",), ()),     # +5.5 ms over the reference
    "no_mouse_lag": ((), ("input_latency_ms",)),        # missing mouse_lag in spot.json: NOT MEASURED, fails the gate
    "fps_regression": (("fps_regression",), ()),        # 15% below the previous run
    "fps_drop_9pct": ((), ()),                          # 9% below the previous run: passes
    "fps_previous_plug_off": ((), ("fps_regression",)),  # same FPS, previous run had the monitors off: NOT COMPARABLE
    "fps_previous_unknown": ((), ("fps_regression",)),   # same FPS, previous run has no machine_before.json
    "no_reference": ((), ("reference_position", "input_latency_ms", "lamps_without_flare", "scene_brightness", "lights_orbs_unmatched", "lights_puffs", "lights_glows_lost")),
    "too_dark": (("scene_brightness",), ()),
    "too_dark_vs_raster": (("scene_brightness",), ()),
    "too_bright_vs_raster": (("scene_brightness",), ()),
    "too_white_vs_raster": (("scene_brightness",), ()),
    "blurry_angles": (("angled_sharpness",), ()),
    "pose_off": (("pose",), ("reference_position",)),   # y135_p0 turned 10 deg: no reference stop there
    "pan_short": (("pose",), ()),
    "goggles_missing": (("goggles",), ()),
    "goggles_untoggled": ((), ("goggles",)),
    "goggles_toggle_rebuilds": (("goggles_toggle_rebuilds",), ()),
    "goggles_dark_frame": (("goggles_dark_frame",), ()),
    "present_stall": (("present_stalls",), ()),           # 120 ms inside Present, 14 ms gap: only present_stalls
    "frame_gap": (("frame_gap_over_50ms",), ()),         # 61 ms gap, 0.5 ms Present, 10 ms telemetry: only the gap
    "orbs_unmatched": (("lights_orbs_unmatched",), ()),  # glow_blobs: 12 unmatched blobs at y0_p0
    "glow_puffs": ((), ()),                              # glow_blobs: 2 puffs at y90_p0: WARN lights_puffs, report only
    "glows_lost": (("lights_glows_lost",), ()),          # glow_blobs: 6 original glows lost at y45_p0
    "orbs_pose_apart_one": ((), ()),                     # one stop with poses apart is listed; the rest pass
    "orbs_all_poses_apart": ((), ("lights_orbs_unmatched", "lights_puffs", "lights_glows_lost")),   # nothing compared: NOT MEASURED
    "orbs_script_error": ((), ("lights_orbs_unmatched", "lights_puffs", "lights_glows_lost")),      # glow_blobs broke on a stop: NOT MEASURED
    "orbs_remix_reference": ((), ("lights_orbs_unmatched", "lights_puffs", "lights_glows_lost")),   # --reference is a Remix run: NOT MEASURED
    "quit_present_stall": (("clean_quit",), ()),
    "no_terminate": ((), ("clean_quit",)),
    "no_telemetry": ((), ("frames_over_50ms", "min_median_fps", "latency_overhead_ms",
                          "skinned_min", "remix_dropped_character_frames", "static_blas_rebuilds",
                          "inward_fraction", "nonfinite_thumb_pixels", "frame_join_clock",
                          "remix_ray_tracing", "remix_compile_stall", "compile_wait_stuck", "never_ray_traced",
                          "nan_pixels", "neon_fraction", "scene_brightness", "angled_sharpness",
                          "outline_overlap", "flashes", "still_flicker",
                          "goggles_toggle_rebuilds", "goggles_dark_frame", *_ZERO_NORMALS, *RECORDER)),
    # Remix writing only every 15th frame: per-frame checks can't pass; picture checks fail closed
    "sparse_telemetry": ((), ("frames_over_50ms", "min_median_fps", "latency_overhead_ms",
                              "skinned_min", "remix_dropped_character_frames", "static_blas_rebuilds",
                              "inward_fraction", "nonfinite_thumb_pixels",
                              "remix_ray_tracing",
                              "nan_pixels", "neon_fraction", "scene_brightness", "angled_sharpness",
                              "outline_overlap", "flashes", "still_flicker",
                              "goggles_toggle_rebuilds", "goggles_dark_frame", *_ZERO_NORMALS, *RECORDER)),
    "renamed_fields": ((), ("frames_over_50ms", "min_median_fps", "latency_overhead_ms",
                            "remix_compile_stall", "compile_wait_stuck", "never_ray_traced")),
    "no_dumps": ((), ("nan_pixels", "neon_fraction", "scene_brightness", "angled_sharpness", "outline_overlap")),
    "no_proxy_gate": ((), ("raster_fallback_frames", "skinned_min", "remix_dropped_character_frames",
                           "remix_ray_tracing")),
    "no_thumbs": ((), ()),                       # still measurable from the screen captures (pan at full rate)
    "half_rate_capture": ((), ("flashes",)),     # screen capture saw half the game frames of the pan
    "spot_error": (("s1: setup",), ("reference_position", "frames_over_50ms", "present_stalls", "frame_gap_over_50ms", "min_median_fps",
                                    "latency_overhead_ms", "lights_orbs_unmatched", "lights_puffs", "lights_glows_lost",
                                    "input_latency_ms", "skinned_min", "remix_dropped_character_frames",
                                    "static_blas_rebuilds", "inward_fraction", "frame_join_clock",
                                    "remix_ray_tracing", "remix_compile_stall", "compile_wait_stuck", "never_ray_traced",
                                    "nonfinite_thumb_pixels", "raster_fallback_frames", "flashes", "still_flicker",
                                    "nan_pixels", "neon_fraction", "scene_brightness", "angled_sharpness",
                                    "outline_overlap", "pose", "goggles", "goggles_toggle_rebuilds",
                                    "goggles_dark_frame", "flare_standins_off", "flare_unplaced_balls",
                                    "flare_moving_balls", "lamps_without_flare", "lights_dimmed_linger",
                                    "light_switchoff_lag", "flare_sam_gear_balls", "lights_weapon_flips",
                                    "lights_recreated", *_ZERO_NORMALS, *RECORDER, "clean_quit")),
    "standins_ignored": (("flare_standins_off",), ()),
    "unplaced_balls": (("flare_unplaced_balls",), ()),
    "moving_balls": (("flare_moving_balls",), ()),
    "lamps_no_flare_jump": (("lamps_without_flare",), ()),
    "dimmed_linger": ((), ()),                    # WARN lights_dimmed_linger: report only, the game dims lights
    "switchoff_lag": ((), ()),                    # WARN light_switchoff_lag: report only, the game fades lights
    "sam_gear_skip_ignored": (("flare_sam_gear_balls",), ()),   # skip sam gear 1, on sam gear max 11
    "sam_gear_noskip": ((), ()),                                # skip sam gear 0, max 11: no limit
    "lightpub_missed_notice": (("flare_sam_gear_balls",), ()),  # reload "after a missed notice" sets skip 1
    "weapon_flips_shape1": (("lights_weapon_flips",), ()),      # weapon by shape 1, 3 flips
    "weapon_flips_shape2": (("lights_weapon_flips",), ()),      # weapon by shape 2 (4d7cb45), 3 flips
    "weapon_flips_shape0": ((), ()),                            # weapon by shape 0, 3 flips: report only
    "weapon_flips_reload_window": ((), ()),                     # 1 flip on the line whose window holds the 0 -> 2 reload
    "recreated_kept_40": (("lights_recreated",), ()),           # keep tracked 1, 40 re-creates
    "recreated_kept_1": ((), ()),                               # keep tracked 1, 1 re-create: under 2
    "recreated_untracked_150": ((), ()),                        # keep tracked 0, 150: report only
    "old_lightpub_format": ((), ("flare_sam_gear_balls", "lights_weapon_flips", "lights_recreated")),
    "untimed_steps": (("step_timing",), ()),          # every 5th still frame: a step with no timing
    "child_over_parent": (("step_timing",), ()),      # a nested step 1.0 ms longer than its parent
    "child_timer_jitter": ((), ()),                   # 0.4 ms over its parent: inside the tolerance
    "occlusion_heavy": (("skip_occlusion_share",), ()),   # 65% of draws skipped by occlusionQuery
    "other_skips_6pct": (("skip_other_share",), ()),      # 6% of a move window's draws, other reason
    "other_skips_4pct": ((), ()),                         # 4%: under the 5% limit
    "no_mod_materials": (("material_share",), ()),        # the mod's materials never drew
    "demoted_textures": (("textures_demoted",), ()),      # half the still frames shrunk textures
    "device_lost": (("clean_quit",), ()),                 # Remix's log shows VK_ERROR_DEVICE_LOST
    "no_quit_log": ((), ("clean_quit",)),   # no remix-dxvk.log in the spot folder
}


# case -> checks that must WARN (shown in the result; the gate still passes)
WARNS = {"low_fps": ("min_median_fps",), "near_target_fps": ("min_median_fps",),
         "near_target_89_996": ("min_median_fps",), "never_ray_traced": ("min_median_fps",),
         "never_rt_no_compiles": ("min_median_fps",),
         "high_latency": ("min_median_fps",), "fps_regression": ("min_median_fps",),
         "dimmed_linger": ("lights_dimmed_linger",), "switchoff_lag": ("light_switchoff_lag",),
         "glow_puffs": ("lights_puffs",),
         "no_reference": ("frame_interval_vs_reference",), "spot_error": ("frame_interval_vs_reference",)}
PREVIOUS = {"fps_regression", "fps_drop_9pct", "fps_previous_plug_off", "fps_previous_unknown"}
# judged against a previous run: the healthy 100 FPS run, in the display state named here (default plug on, as the run)
PREVIOUS_DISPLAY = {"fps_previous_plug_off": display_state.OFF_002048, "fps_previous_unknown": None}
NO_REFERENCE = {"no_reference"}                  # run without --reference


def make_run(folder: str, case: str, no_remix: bool = False, display: dict | None = display_state.ON_212703) -> str:
    """display: the spot's machine_before.json (display_state), None for a run recorded before Nov 16 16:58."""
    run = os.path.join(folder, case)
    shutil.rmtree(run, ignore_errors=True)
    os.makedirs(run)
    Builder(run, "s1", case).build()
    if display is not None:
        gate.save_json(os.path.join(run, "s1", "machine_before.json"), display)
    manifest = {"spots": ["s1"], "fake": case}
    if case == "noremix_scenedraws0" or no_remix:
        manifest["noRemix"] = True
    gate.save_json(os.path.join(run, "manifest.json"), manifest)
    return run


# Free-play session logs for gate.evaluate_session: case -> (checks that must FAIL, checks that must be NOT MEASURED);
# every other check must PASS. 300 frames of 10 ms, goggles on at proxy frames 100..139, so the goggles-off is proxy
# frame 140 and Remix ray traces again from frame 141 (one frame later, as in session 20261115-135449 at frame 41475).
# The two never-ray-traced cases run 700 frames of 30 ms (21 s, past compile_wait_stuck_s) with 2 ms of GPU work.
_NO_RT_NM = ("goggles_toggle_rebuilds", "goggles_dark_frame")
SESSION_CASES = {
    "session_good": ((), ()),
    "session_scene_clear": (("goggles_toggle_rebuilds",), ()),         # 94 static meshes built, as at 135449 frame 41476
    "session_dark_off": (("goggles_dark_frame",), ()),                 # first ray-traced frame at 0.2x the luma 10 later
    "session_dropped_character": (("remix_dropped_character_frames",), ()),  # Remix 0 characters, proxy drew them +/-3
    "session_goggles_no_characters": ((), ()),                         # 0 characters inside goggles does not count
    "session_device_lost": (("clean_quit",), ()),
    "session_slow_frame": (("frames_over_50ms",), ()),                 # one 64.7 ms scene frame
    "session_slow_goggles": (("frames_over_50ms",), ()),               # 64.7 ms inside goggles counts (135449 frame 41472)
    "session_slow_loading": ((), ()),                                  # 80 ms on a proxy loading frame (sceneDraws 0)
    "session_not_rt": (("remix_ray_tracing",), ()),                    # 10 scene frames not ray traced (~4%)
    "session_compile_stuck": (("compile_wait_stuck", "remix_ray_tracing"), _NO_RT_NM),  # never RT, compiles > 0
    "session_never_rt_no_compiles": (("never_ray_traced", "remix_ray_tracing"), _NO_RT_NM),  # never RT, 0 compiles
    "session_freeze_in_play": (("freezes_in_play_over_1s",), ()),      # 1500 ms Present-to-Present gap on frame 200
    "session_quit_gap": ((), ()),                                      # 4931 ms gap on the last frame: quit, report only
    "session_menu_gap": ((), ()),                                      # 1500 ms gap on a frame without a 'G:' publish
}


def make_session(folder: str, case: str) -> str:
    d = os.path.join(folder, case)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    no_rt = case in ("session_compile_stuck", "session_never_rt_no_compiles")
    pg, tel = [], []
    for f in range(700 if no_rt else 300):
        g = 1 if 100 <= f < 140 else 0
        draws = 0 if (case == "session_slow_loading" and f == 220) else 400
        pg.append(f"{f},{draws},5,1,{g},{1 - g}")
        rt = not (no_rt or 100 <= f <= 140 or (case == "session_not_rt" and 200 <= f < 210))
        luma = 20 if (case == "session_dark_off" and f == 141) else 100
        builds = 94 if (case == "session_scene_clear" and f == 141) else (3 if f == 141 else 0)
        skinned = 0 if ((case == "session_dropped_character" and f == 220)
                        or (case == "session_goggles_no_characters" and f == 120)) else 5
        ms = (64.7 if ((case == "session_slow_frame" and f == 220) or (case == "session_slow_goggles" and f == 120))
              else 80.0 if (case == "session_slow_loading" and f == 220) else 30.0 if no_rt else 10.0)
        thumb = base64.b64encode(np.full(gate.THUMB_W * gate.THUMB_H, luma, np.uint8).tobytes()).decode()
        tel.append(json.dumps({"frame": f, "rayTraced": rt, "staticBlasBuilds": builds, "skinnedInstances": skinned,
                               "instances": 500, "lumaThumb": thumb, "frameTimeMs": ms,
                               "gpuTimeMs": 2.0 if no_rt else 8.0,
                               "remixShaderCompiles": 5 if case == "session_compile_stuck" else 0}))
    gate.save_text(os.path.join(d, "d3d9_proxy_gate.csv"), "\n".join(pg) + "\n")
    gate.save_text(os.path.join(d, "rtx_telemetry.jsonl"), "\n".join(tel) + "\n")
    # d3d9_proxy_frames.csv: frame,entryMs,gapMs,presentMs,draws,publishCount,publishes (a gameplay-camera 'G:' publish)
    cam = "G:0:0:-221.528:360.466:193.002:0.9438:-0.3304:0.0000:0.000000"
    n, rows, t = len(pg), [], 0.0
    for f in range(n):
        gap = (1500.0 if (case in ("session_freeze_in_play", "session_menu_gap") and f == 200)
               else 4931.0 if (case == "session_quit_gap" and f == n - 1) else 10.0)
        t += gap
        pub = "" if (case == "session_menu_gap" and f == 200) else cam
        rows.append(f"{f},{t:.3f},{gap:.3f},2.000,400,{1 if pub else 0},{pub}")
    gate.save_text(os.path.join(d, "d3d9_proxy_frames.csv"), "\n".join(rows) + "\n")
    lost = "[error] vkQueueSubmit: VK_ERROR_DEVICE_LOST\n" if case == "session_device_lost" else ""
    gate.save_text(os.path.join(d, "rtx-remix_logs_remix-dxvk.log"), "[info] Remix started\n" + lost)
    return d


def check_session_case(folder: str, case: str) -> tuple[bool, str]:
    result = gate.evaluate_session(make_session(folder, case))
    status = {c["check"]: c["status"] for c in result["checks"]}
    fails, nms = SESSION_CASES[case]
    want = {**{c: gate.NOT_MEASURED for c in nms}, **{c: gate.FAIL for c in fails}}
    wrong = [f"{c}: {st}, expected {want.get(c, gate.PASS)}" for c, st in status.items() if st != want.get(c, gate.PASS)]
    wrong += [f"{c}: not reported" for c in want if c not in status]
    gate_fails = bool(fails) or any(c not in gate.SOFT_NM for c in nms)
    if (result["status"] == gate.PASS) == gate_fails:
        wrong.append(f"session status {result['status']}")
    return not wrong, "; ".join(wrong) or "as expected"


_REAL_GLOW_BLOBS = gate.glow_blobs_stop


def fake_glow_blobs(case: str, still: str) -> tuple[int, str]:
    """Stand-in for glow_blobs.py (fake stills are not 320x180 pictures): the gate's handling of its exit codes and
    output is what the fake cases test; the real verdicts are tested in test_gate.py on runs 142638 and 063147 against
    reference 000454."""
    orbs, puffs, lost = 0, 0, 0
    if case == "orbs_unmatched" and still == "y0_p0":
        orbs = 12
    if case == "glow_puffs" and still == "y90_p0":
        puffs = 2
    if case == "glows_lost" and still == "y45_p0":
        lost = 6
    if case == "orbs_all_poses_apart":
        return 2, "glow_blobs: NOT MEASURED, poses differ by 40 cm, yaw 4.00, pitch 0.00 deg"
    if case == "orbs_script_error" and still == "y45_p0":
        return 1, "SystemExit: no window y45_p0 with frames and proxy frames"
    if case == "orbs_pose_apart_one" and still == "y135_p0":
        return 2, "glow_blobs: NOT MEASURED, poses differ by 20 cm, yaw 4.37, pitch 0.00 deg"
    ok = not (orbs or lost)     # 9ddc95d: the verdict and exit code are orbs and lost glows; puffs are report only
    v = lambda n: "PASS" if n == 0 else "FAIL"
    return (0 if ok else 1), "\n".join([
        "glow_blobs: hud mask none",
        f"glow_blobs: unmatched {orbs} of 50 blobs (fake): {v(orbs)}",
        f"glow_blobs: puffs {puffs} (fake): report only",
        f"glow_blobs: lost {lost} of 20 original glows (fake): {v(lost)}",
        f"glow_blobs: {'PASS' if ok else 'FAIL'} (orbs unmatched {orbs}, lost glows {lost}; puffs {puffs} report only)"])

def check_case(folder: str, case: str) -> tuple[bool, dict, str]:
    """(expectation met, result, explanation). Checks the case does not name must PASS, except fps_regression, which
    only WARNs when no previous run is given (the PREVIOUS cases get one)."""
    must_fail, must_nm = CASES[case]
    must_warn = WARNS.get(case, ())
    previous = make_run(os.path.join(folder, "previous"), "good",
                        display=PREVIOUS_DISPLAY.get(case, display_state.ON_212703)) if case in PREVIOUS else None
    reference = None if case in NO_REFERENCE else make_run(os.path.join(folder, "reference"), "good",
                                                            no_remix=case != "orbs_remix_reference")
    gate.glow_blobs_stop = lambda r, o, still, orig: fake_glow_blobs(case, still)
    try:
        result = gate.evaluate(make_run(folder, case), previous=previous, reference=reference)
    finally:
        gate.glow_blobs_stop = _REAL_GLOW_BLOBS
    status = {c["check"]: c["status"] for c in result["checks"]}
    expect = {"fps_regression": gate.PASS if case in PREVIOUS else gate.WARN,
              "input_latency_screen_ms": gate.NOT_MEASURED,   # always, until the screen grab works (in SOFT_NM)
              "lights_lamps_dimmed": gate.NOT_MEASURED}       # fake runs have no switch steps (in SOFT_NM; its
                                                              # verdicts are tested in test_gate.py on 142638)
    expect.update({c: gate.WARN for c in must_warn})
    expect.update({c: gate.NOT_MEASURED for c in must_nm})
    expect.update({c: gate.FAIL for c in must_fail})
    wrong = []
    for check, st in status.items():
        if st != expect.get(check, gate.PASS):
            wrong.append(f"{check}: {st}, expected {expect.get(check, gate.PASS)}")
    for check in must_fail + must_nm + must_warn:
        if check not in status:
            wrong.append(f"{check}: not reported")
    if (result["status"] == gate.PASS) != (not must_fail and not [c for c in must_nm if c not in gate.SOFT_NM]):
        wrong.append(f"overall {result['status']}")
    return not wrong, result, "; ".join(wrong) or "as expected"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    ok_all = True
    for case in CASES:
        ok, result, why = check_case(sys.argv[1], case)
        ok_all &= ok
        print(f"[{'OK ' if ok else 'BAD'}] {case:18s} gate {result['status']:4s}  {why}")
    print("DRY RUN", "PASS" if ok_all else "FAIL", "-", os.path.abspath(sys.argv[1]))
    sys.exit(0 if ok_all else 1)
