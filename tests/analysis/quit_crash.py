"""Quit crash timeline: for each saved session, line up window focus changes (bridge32.log),
the Present semaphore waits, VK_ERROR_DEVICE_LOST (remix-dxvk.log), and the Terminate command.

Question it answers: does the device loss follow the game window losing focus, and does every
focus loss end in a device loss, or only the one at quit?

Usage: python -I quit_crash.py [session_dir ...]   (default: every saved session under experiments)
"""
import argparse
import re
from pathlib import Path

EXPERIMENTS = Path(r"D:\BlacklistOverhaul\experiments")
DEFAULT_GLOBS = ("playtest-evidence/play-setup-logs/before-*", "playtest-evidence/steam-session-*",
                 "remix-evidence/session-*")
TS = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\.(\d{3})\]")


def secs(line):
    m = TS.match(line)
    if not m:
        return None
    h, mi, s, ms = map(int, m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def fmt(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def events(path, patterns):
    out = []
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        t = secs(line)
        if t is None:
            continue
        for name, pat in patterns:
            if pat in line:
                out.append((t, name))
    return out


def find(d, stem):
    for cand in (d / f"rtx-remix_logs_{stem}.log", d / "rtx-remix" / "logs" / f"{stem}.log", d / f"{stem}.log"):
        if cand.exists():
            return cand
    return d / f"rtx-remix_logs_{stem}.log"


def analyze(d):
    ev = events(find(d, "bridge32"), [
        ("focus-lost", "Client window became inactive"),
        ("focus-back", "Client window became active"),
        ("present-wait", "Still waiting on the Present semaphore"),
        ("present-max-retries", "Max retries reached waiting on the Present"),
        ("terminate", "Sending Terminate command"),
    ])
    ev += events(find(d, "remix-dxvk"), [("DEVICE_LOST", "VK_ERROR_DEVICE_LOST"),
                                         ("not-disposed", "were not disposed of")])
    ev += events(find(d, "bridge64"), [("server-leaks", "objects discovered in")])
    ev.sort()
    lost = [t for t, n in ev if n == "DEVICE_LOST"]
    focus = [t for t, n in ev if n == "focus-lost"]
    term = [t for t, n in ev if n == "terminate"]
    # Collapse repeated lines into first time + count.
    rows, last = [], None
    for t, n in ev:
        if last and last[1] == n and t - last[2] < 5.0:
            last[3] += 1
            last[2] = t
        else:
            last = [t, n, t, 1]
            rows.append(last)
    print(f"\n== {d.relative_to(EXPERIMENTS) if d.is_relative_to(EXPERIMENTS) else d}")
    if not ev:
        print("   no bridge or remix logs")
        return None
    for t0, n, t1, c in rows:
        print(f"   {fmt(t0)}  {n}" + (f" x{c} (to {fmt(t1)})" if c > 1 else ""))
    verdict = {
        "device_lost": bool(lost),
        "focus_losses": len(focus),
        "lost_after_focus_s": round(lost[0] - max([f for f in focus if f <= lost[0]], default=float("nan")), 3)
        if lost and any(f <= lost[0] for f in focus) else None,
        "lost_before_terminate": bool(lost and term and lost[0] < term[0]),
        "focus_losses_without_loss": sum(1 for f in focus if not any(0 <= l - f <= 15 for l in lost)),
    }
    print("   ->", verdict)
    return verdict


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="*", type=Path)
    a = ap.parse_args()
    dirs = a.dirs or sorted(p for g in DEFAULT_GLOBS for p in EXPERIMENTS.glob(g) if p.is_dir())
    results = {d: analyze(d) for d in dirs}
    v = [r for r in results.values() if r]
    print(f"\nsessions with logs: {len(v)}; with DEVICE_LOST: {sum(r['device_lost'] for r in v)}; "
          f"DEVICE_LOST preceded by focus loss: {sum(r['lost_after_focus_s'] is not None for r in v)}; "
          f"DEVICE_LOST before Terminate: {sum(r['lost_before_terminate'] for r in v)}; "
          f"focus losses with no loss within 15 s: {sum(r['focus_losses_without_loss'] for r in v)}")


if __name__ == "__main__":
    main()
