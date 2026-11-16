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
        self.telemetry, self.proxy, self.pgate, self.windows = [], [], [], []
        self.yaw = self.pitch = 0.0
        self.base_cam = 40.0   # the proxy's yaw for the reference pose (any value)

    def frame(self, kind: str, wname: str, i: int, goggles: bool = False) -> None:
        self.tframe += 1; self.pframe += 1
        img = view(self.yaw, self.pitch, self.rng)
        rec = {"frame": self.tframe, "frameTimeMs": 10.0, "gpuTimeMs": 8.0, "instances": 900, "skinnedInstances": 3,
               "staticBlasRebuilds": 0, "staticBlasBuilds": 0,
               "rayTraced": not (goggles and wname.endswith("_rtoff") and i < 3),
               "cameraCut": False, "textureMemoryMB": 1800, "vramUsedMB": 6100,
               "skinnedTriangles": 60000, "inwardSkinnedTriangles": 12, "thumbNonFinite": 0}
        # recorder fields (gate.recorder_checks): healthy values, with one fault per case below
        rec.update({"replacedMaterialDraws": 354, "legacyMaterialDraws": 25, "texturesDemoted": 0,
                    "skips": {"occlusionQuery": 300},
                    "passMs": {"InjectRTX": [10.0, 4.0, 1], "InjectRTX/Ray Reconstruction": [5.0, 0.2, 1],
                               "InjectRTX/Ray Reconstruction/Prepare DLSS": [0.3, 0.02, 1]}})
        c = self.case
        if c == "nan_frame" and kind == "still" and i == 3: rec["thumbNonFinite"] = 40
        if c == "goggles_toggle_rebuilds" and wname == "goggles1_rtoff" and i == 3: rec["staticBlasBuilds"] = 300
        if c == "goggles_dark_frame" and wname == "goggles1_rtoff" and i == 3: img = img * 0.2
        if c in ("slow_frame", "noremix_scenedraws0") and kind == "pan" and i == 30: rec["frameTimeMs"] = 120.0 if c == "noremix_scenedraws0" else 61.0
        if c in ("missing_cam_col", "cut_short_cam_col") and wname == "to_y45_p0" and i == 2: rec["frameTimeMs"] = 120.0
        if c == "load_count_mismatch" and wname == "to_y45_p0" and i in (0, 1): rec["frameTimeMs"] = 120.0
        if c == "slow_beside_nonload" and wname == "to_y45_p0" and i == 0: rec["frameTimeMs"] = 120.0
        loading = ((c == "loading_in_window" and wname == "to_y45_p0" and i in (2, 3))
                   or (c in ("load_count_mismatch", "slow_beside_nonload") and wname == "to_y45_p0" and i == 0))
        if c == "loading_in_window" and loading: rec["frameTimeMs"] = 120.0
        if c in FRAME_TIMES: rec["frameTimeMs"], rec["gpuTimeMs"] = FRAME_TIMES[c]
        if c == "no_skinned" and kind == "still" and i == 3: rec["skinnedInstances"] = 0
        if c == "blas_rebuild" and kind == "pan" and i == 50: rec["staticBlasRebuilds"] = 1
        if c == "inward" and kind == "move" and i == 2: rec["inwardSkinnedTriangles"] = 900
        if c == "flash_pan" and kind == "pan" and i == 40: img = np.clip(img + 70, 0, 255)
        if c == "pop_still" and wname == "y90_p0" and i == 7: img[20:40, 40:64] = 255 - img[20:40, 40:64]
        if c == "flicker_still" and wname == "y90_p0" and i % 2 == 1:
            img[20:36, 40:56] = np.clip(img[20:36, 40:56] + 10, 0, 255)
        if c == "pulse_still" and wname == "y90_p0":
            img = np.clip(img + 12.0 * math.sin(i * 2.0 * math.pi / 12.0), 0, 255)
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
        if not (c == "sparse_telemetry" and self.tframe % 15):
            self.telemetry.append(rec)
        # proxy camera trace: sign of yaw flipped against the request, pitch kept
        cy = math.radians(self.base_cam - self.yaw + (10.0 if c == "pose_off" and wname == "y135_p0" else 0.0))
        cp = math.radians(self.pitch)
        if c == "pan_short" and kind == "pan":
            cy = math.radians(self.base_cam - self.yaw * 0.5)
        if loading:
            self.proxy.append(f"{self.pframe},1.0,1.6,0.5,29,0,")
            self.pgate.append(f"{self.pframe},0,0,0,0,1")
        elif c == "missing_cam_col" and wname == "to_y45_p0" and i == 2:
            self.proxy.append(f"{self.pframe},1.0,14.0,0.5,1500,1")  # 6 columns: no camera column -> unknown
            self.pgate.append(f"{self.pframe},1400,40,1,{1 if goggles else 0},1")
        elif c == "cut_short_cam_col" and wname == "to_y45_p0" and i == 2:
            self.proxy.append(f"{self.pframe},1.0,14.0,0.5,1500,1,G:0:0:1.")  # cut short in camera column -> unknown
            self.pgate.append(f"{self.pframe},1400,40,1,{1 if goggles else 0},1")
        elif c == "short_row_in_window" and wname == "to_y45_p0" and i == 2:
            self.proxy.append(f"{self.pframe},1.0,14.0")  # cut-short 3-col row inside scene window -> NOT MEASURED
            self.pgate.append(f"{self.pframe},1400,40,1,{1 if goggles else 0},1")
        else:
            fx, fy, fz = math.cos(cp) * math.cos(cy), math.cos(cp) * math.sin(cy), math.sin(cp)
            pms = 120.0 if (c == "present_stall" and kind == "pan" and i == 30) else 0.5
            clk = 1000.0 + self.pframe * 10.0
            self.proxy.append(f"{self.pframe},{clk:.3f},14.0,{pms},1500,1,P:22:0:1.0:2.0:3.0:{fx:.5f}:{fy:.5f}:{fz:.5f}:0.0")
            handoff = 0 if (c == "raster_flash" and kind == "pan" and i == 70) else 1
            sdraws = 0 if c == "noremix_scenedraws0" else 1400
            self.pgate.append(f"{self.pframe},{sdraws},40,{handoff},{1 if goggles else 0},1")
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
        if self.case == "slow_beside_nonload" and name == "to_y45_p0":
            self.pframe += 1
            self.proxy.append(f"{self.pframe}," + self.proxy[-1].split(",", 1)[1])
            self.pgate.append(f"{self.pframe}," + self.pgate[-1].split(",", 1)[1])
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
            samples = [round(22.0 + bump + ((i % 3) - 1) * 0.5, 2) for i in range(20)]
            spot["mouse_lag"] = {"samples_ms": samples, "game_share_ms": 14.0, "after_game_ms": round(8.0 + bump, 2),
                                 "method": "fake 20x5deg gdigrab"}
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
            clk = 1000.0 + (self.pframe + 1) * 10.0
            self.proxy.append(f"{self.pframe + 1},{clk:.3f},14.0,1500.0,1500,1,P:22:0:1.0:2.0:3.0:1.0:0.0:0.0:0.0")
        gate.save_text(os.path.join(self.dir, "telemetry.jsonl"), "".join(json.dumps(r) + "\n" for r in self.telemetry))
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
        plog = (
            "lightpub: loaded (scale 1, sun 1, fill 1, max 20, flare stand-ins 0, trace 1, "
            "stand-in skip unplaced 1, skip moving 1)\n"
            f"lightpub: stray frame 120 lamps 120 without flare {xm_val} (max {xm_val}) flares 61 "
            f"stand-ins {s_val} on unplaced {um_val} (max {um_val}) on moving {mm_val} (max {mm_val}) "
            f"dimmed 2 (max 2) longest dim {tm_val} frames (max {tm_val})\n"
        )
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


# checks that read telemetry the recorder writes per frame: NOT MEASURED whenever that telemetry is missing
RECORDER = ("step_timing", "skip_occlusion_share", "skip_other_share", "material_share", "textures_demoted")

# case -> (checks that must FAIL, checks that must be NOT MEASURED); everything else must PASS.
CASES = {
    "good": ((), ()),
    "nan_pixel": (("nan_pixels", "scene_brightness"), ()),
    "nan_frame": (("nonfinite_thumb_pixels",), ()),
    "neon": (("neon_fraction",), ()),
    "no_skinned": (("skinned_min",), ()),
    "sam_missing": (("outline_overlap",), ()),
    "inward": (("inward_fraction",), ()),
    "raster_flash": (("raster_fallback_frames",), ()),
    "flash_pan": (("flashes",), ()),
    "pop_still": (("flashes",), ()),
    "flicker_still": (("still_flicker",), ()),
    "pulse_still": (("still_flicker",), ()),
    "blas_rebuild": (("static_blas_rebuilds",), ()),
    "slow_frame": (("frames_over_50ms",), ()),
    "loading_in_window": ((), ()),                        # >50 ms frames during a loading screen are excluded: passes
    "missing_cam_col": (("frames_over_50ms",), ()),       # 6-column proxy row (no camera column) is unknown -> fails
    "cut_short_cam_col": (("frames_over_50ms",), ()),     # cut-short camera column text is unknown -> fails
    "short_row_in_window": ((), ("skip_occlusion_share", "skip_other_share", "present_stalls")),
    "cut_short_proxy": ((), ()),                          # last proxy row cut short mid-write: finishes and passes
    "load_count_mismatch": (("frames_over_50ms",), ()),   # 7 tel vs 6 proxy: 1 loading frame excuses only t0, t1 fails
    "slow_beside_nonload": (("frames_over_50ms",), ()),   # 6 tel vs 7 proxy: t0 maps to [load, nonload] -> fails
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
    "no_reference": ((), ("input_latency_ms", "lamps_without_flare", "scene_brightness")),
    "too_dark": (("scene_brightness",), ()),
    "too_dark_vs_raster": (("scene_brightness",), ()),
    "too_bright_vs_raster": (("scene_brightness",), ()),
    "too_white_vs_raster": (("scene_brightness",), ()),
    "blurry_angles": (("angled_sharpness",), ()),
    "pose_off": (("pose",), ()),
    "pan_short": (("pose",), ()),
    "goggles_missing": (("goggles",), ()),
    "goggles_untoggled": ((), ("goggles",)),
    "goggles_toggle_rebuilds": (("goggles_toggle_rebuilds",), ()),
    "goggles_dark_frame": (("goggles_dark_frame",), ()),
    "present_stall": (("present_stalls",), ()),
    "quit_present_stall": (("clean_quit",), ()),
    "no_terminate": ((), ("clean_quit",)),
    "no_telemetry": ((), ("frames_over_50ms", "min_median_fps", "latency_overhead_ms",
                          "skinned_min", "static_blas_rebuilds", "inward_fraction", "nonfinite_thumb_pixels",
                          "goggles_toggle_rebuilds", "goggles_dark_frame", *RECORDER)),
    # Remix writing only every 15th frame: per-frame checks can't pass; flashes fall back to the screen captures
    "sparse_telemetry": ((), ("frames_over_50ms", "min_median_fps", "latency_overhead_ms",
                              "skinned_min", "static_blas_rebuilds", "inward_fraction", "nonfinite_thumb_pixels",
                              "goggles_toggle_rebuilds", "goggles_dark_frame", *RECORDER)),
    "renamed_fields": ((), ("frames_over_50ms", "min_median_fps", "latency_overhead_ms")),
    "no_dumps": ((), ("nan_pixels", "neon_fraction", "scene_brightness", "angled_sharpness", "outline_overlap")),
    "no_proxy_gate": ((), ("raster_fallback_frames",)),
    "no_thumbs": ((), ()),                       # still measurable from the screen captures (pan at full rate)
    "half_rate_capture": ((), ("flashes",)),     # screen capture saw half the game frames of the pan
    "spot_error": (("s1: setup",), ("frames_over_50ms", "present_stalls", "min_median_fps", "latency_overhead_ms",
                                    "input_latency_ms", "skinned_min", "static_blas_rebuilds", "inward_fraction",
                                    "nonfinite_thumb_pixels", "raster_fallback_frames", "flashes", "still_flicker",
                                    "nan_pixels", "neon_fraction", "scene_brightness", "angled_sharpness",
                                    "outline_overlap", "pose", "goggles", "goggles_toggle_rebuilds",
                                    "goggles_dark_frame", "flare_standins_off", "flare_unplaced_balls",
                                    "flare_moving_balls", "lamps_without_flare", "lights_dimmed_linger",
                                    "light_switchoff_lag", *RECORDER, "clean_quit")),
    "standins_ignored": (("flare_standins_off",), ()),
    "unplaced_balls": (("flare_unplaced_balls",), ()),
    "moving_balls": (("flare_moving_balls",), ()),
    "lamps_no_flare_jump": (("lamps_without_flare",), ()),
    "dimmed_linger": (("lights_dimmed_linger",), ()),
    "switchoff_lag": (("light_switchoff_lag",), ()),
    "untimed_steps": (("step_timing",), ()),          # every 5th still frame: a step with no timing
    "child_over_parent": (("step_timing",), ()),      # a nested step 1.0 ms longer than its parent
    "child_timer_jitter": ((), ()),                   # 0.4 ms over its parent: inside the tolerance
    "occlusion_heavy": (("skip_occlusion_share",), ()),   # 65% of draws skipped by occlusionQuery
    "other_skips_6pct": (("skip_other_share",), ()),      # 6% of a move window's draws, other reason
    "other_skips_4pct": ((), ()),                         # 4%: under the 5% limit
    "no_mod_materials": (("material_share",), ()),        # the mod's materials never drew
    "demoted_textures": (("textures_demoted",), ()),      # half the still frames shrunk textures
    "device_lost": (("clean_quit",), ()),                 # Remix's log shows VK_ERROR_DEVICE_LOST
    "no_quit_log": ((), ("clean_quit",)),                 # no remix-dxvk.log in the spot folder
}


# case -> checks that must WARN (shown in the result; the gate still passes)
WARNS = {"low_fps": ("min_median_fps",), "near_target_fps": ("min_median_fps",),
         "near_target_89_996": ("min_median_fps",),
         "high_latency": ("min_median_fps",), "fps_regression": ("min_median_fps",)}
PREVIOUS = {"fps_regression", "fps_drop_9pct"}    # judged against a previous run: the healthy 100 FPS run
NO_REFERENCE = {"no_reference"}                  # run without --reference


def make_run(folder: str, case: str) -> str:
    run = os.path.join(folder, case)
    shutil.rmtree(run, ignore_errors=True)
    os.makedirs(run)
    Builder(run, "s1", case).build()
    manifest = {"spots": ["s1"], "fake": case}
    if case == "noremix_scenedraws0":
        manifest["noRemix"] = True
    gate.save_json(os.path.join(run, "manifest.json"), manifest)
    return run


def check_case(folder: str, case: str) -> tuple[bool, dict, str]:
    """(expectation met, result, explanation). Checks the case does not name must PASS, except fps_regression, which
    only WARNs when no previous run is given (the PREVIOUS cases get one)."""
    must_fail, must_nm = CASES[case]
    must_warn = WARNS.get(case, ())
    previous = make_run(os.path.join(folder, "previous"), "good") if case in PREVIOUS else None
    reference = None if case in NO_REFERENCE else make_run(os.path.join(folder, "reference"), "good")
    result = gate.evaluate(make_run(folder, case), previous=previous, reference=reference)
    status = {c["check"]: c["status"] for c in result["checks"]}
    expect = {"fps_regression": gate.PASS if case in PREVIOUS else gate.WARN}
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
    if (result["status"] == gate.PASS) != (not must_fail and not must_nm):
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
