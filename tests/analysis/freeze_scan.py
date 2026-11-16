"""Freezes in play: gaps over a limit (default 1000 ms) between Present entries, from the proxy's frame trace.

Usage:
  freeze_scan.py --self-test
  freeze_scan.py [--limit MS] FOLDER [FOLDER ...]

FOLDER holds d3d9_proxy_frames.csv (play-setup-logs sessions) or proxy_frames_launch.csv / proxy_frames.csv (gate
spots). Rows: frame,entryMs,gapMs,presentMs,draws,publishCount,publishes; a file can hold several launches (the
frame number restarts). When FOLDER has spot.json, each gap is matched against the scored windows' proxy ranges.

Each gap over the limit gets one class; only "play" counts as a freeze:
  startup  the frame is among the first STARTUP_FRAMES of the launch (device creation, first loads)
  end      the last frame of the launch: the gap is the game quitting or the quit hang, not a freeze in play
           (Nov 16: all three "freezes" first reported in Corey's sessions were this frame)
  menu     the frame or the one before has no gameplay-camera publish ('G'), so it is a menu or loading screen
  play     everything else
"""
import json
import os
import sys

STARTUP_FRAMES = 10


def classify(frames, limit, edge_rule=True):
    """frames: list of (frame, gapMs, presentMs, inPlay) for one launch. Returns [(frame, gap, present, class)]."""
    out = []
    last = len(frames) - 1
    for i, (frame, gap, present, in_play) in enumerate(frames):
        if gap <= limit:
            continue
        if edge_rule and frame < STARTUP_FRAMES:
            kind = 'startup'
        elif edge_rule and i == last:
            kind = 'end'
        elif not (in_play and i > 0 and frames[i - 1][3]):
            kind = 'menu'
        else:
            kind = 'play'
        out.append((frame, gap, present, kind))
    return out


def read_launches(path):
    launches, cur, prev = [], None, None
    with open(path, 'r', errors='replace') as f:
        for line in f:
            p = line.rstrip('\r\n').split(',', 6)
            if len(p) < 6:
                continue
            try:
                frame, gap, present, draws = int(p[0]), float(p[2]), float(p[3]), int(p[4])
            except ValueError:
                continue
            if cur is None or prev is None or frame <= prev:
                cur = []
                launches.append(cur)
            in_play = draws > 0 and 'G:' in (p[6] if len(p) > 6 else '')
            cur.append((frame, gap, present, in_play))
            prev = frame
    return launches


def frames_file(folder):
    for name in ('proxy_frames_launch.csv', 'd3d9_proxy_frames.csv', 'proxy_frames.csv'):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            return path
    return None


def windows(folder):
    path = os.path.join(folder, 'spot.json')
    if not os.path.exists(path):
        return None
    spot = json.load(open(path))
    return [(w['name'], w['proxy'][0], w['proxy'][1]) for w in spot.get('windows', []) if w.get('proxy')]


def excluded_lines(found, frames, gate_spot):
    """The gaps the rule leaves out, so it hides nothing: per class a count and the sizes, then the quit gap
    (the last frame's gap, printed whatever its size). In a gate spot's trace the last frame is where the spot's
    copy ends, not necessarily the game's quit, so it is labelled 'trace end' there."""
    lines = []
    for kind in ('startup', 'menu', 'end'):
        sizes = [gap for _, gap, _, k in found if k == kind]
        if sizes:
            lines.append(f'excluded {kind}: {len(sizes)} gap(s), ' + ', '.join(f'{s:.0f}' for s in sizes) + ' ms')
    last_frame, last_gap = frames[-1][0], frames[-1][1]
    label = 'trace end (gate spot copy, not a quit)' if gate_spot else 'quit gap'
    lines.append(f'{label}: {last_gap:.0f} ms on frame {last_frame}')
    return lines


def renderer(folder):
    """Which d3d9 the proxy chained in the folder's last launch: Remix, or Windows' own (the original game)."""
    for name in ('d3d9_proxy.log', 'proxy_launch.log', 'proxy.log'):
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            continue
        last = None
        with open(path, 'r', errors='replace') as f:
            for line in f:
                if 'chained ' in line and 'DLL:' in line:
                    last = line
        if last:
            return 'original game (no Remix)' if 'SysWOW64' in last or 'System32' in last else 'Remix'
    return 'renderer unknown'


def scan(folder, limit):
    path = frames_file(folder)
    if not path:
        print(f'{folder}: no frame trace')
        return 0
    wins = windows(folder)
    freezes = 0
    for n, frames in enumerate(read_launches(path)):
        found = classify(frames, limit)
        play_frames = sum(1 for f in frames if f[3])
        print(f'{folder} [{os.path.basename(path)}] launch {n} ({renderer(folder)}): '
              f'frames {frames[0][0]}..{frames[-1][0]} ({len(frames)}), in play {play_frames}, '
              f'gaps over {limit:.0f} ms {len(found)}, freezes in play {sum(1 for f in found if f[3] == "play")}')
        # Only proxy_frames.csv is a copy starting and ending with the spot; the other two traces span the launch.
        for line in excluded_lines(found, frames, os.path.basename(path) == 'proxy_frames.csv'):
            print('    ' + line)
        for frame, gap, present, kind in found:
            if kind != 'play':
                continue
            if wins is None:
                where = 'no scored windows (not a gate spot)'
            else:
                # Frames a+1..b, as in gate.py: the gap ending on frame a began before the window opened.
                hit = [name for name, a, b in wins if a < frame <= b]
                where = 'in window ' + ', '.join(hit) if hit else 'outside every scored window'
            print(f'    frame {frame} of {frames[-1][0]}: gap {gap:.0f} ms, inside Present {present:.0f} ms, '
                  f'{kind}, {where}')
            freezes += kind == 'play'
    return freezes


def self_test():
    # One launch, 200 frames in play at 16 ms, with a 1.5 s gap on frame 1 (startup), frame 100 (play) and on the
    # last frame (quit); and frames 150-151 on a menu (no gameplay camera) with a 1.5 s gap on 151.
    frames = [(i, 16.0, 1.0, not (150 <= i <= 151)) for i in range(200)]
    for i in (1, 100, 151, 199):
        frames[i] = (i, 1500.0, 2.0, frames[i][3])
    got = classify(frames, 1000)
    kinds = {f: k for f, _, _, k in got}
    ok = True

    def check(name, cond):
        nonlocal ok
        ok &= cond
        print(('ok    ' if cond else 'FAIL  ') + name)

    check('startup gap on frame 1 is not a freeze', kinds.get(1) == 'startup')
    check('gap on frame 100 in play is a freeze', kinds.get(100) == 'play')
    check('gap on a menu frame is not a freeze', kinds.get(151) == 'menu')
    check('gap on the last frame (quit) is not a freeze', kinds.get(199) == 'end')
    check('exactly one freeze in play', sum(k == 'play' for k in kinds.values()) == 1)
    # Fake case: a session whose only long gap is its last frame must have no freeze, and without the edge rule
    # (the Nov 16 mistake) the same session reports one, so the rule is what keeps it out.
    quit_only = [(i, 16.0, 1.0, True) for i in range(50)] + [(50, 2490.0, 8.0, True)]
    check('quit-only session: 0 freezes with the rule',
          sum(k == 'play' for *_, k in classify(quit_only, 1000)) == 0)
    check('quit-only session: 1 freeze without the rule (the rule is load-bearing)',
          sum(k == 'play' for *_, k in classify(quit_only, 1000, edge_rule=False)) == 1)
    # Fake case: the rule must not hide the long last gap; it has to appear in the quit line and the 'end' line.
    lines = excluded_lines(classify(quit_only, 1000), quit_only, gate_spot=False)
    check('quit-only session: the 2490 ms gap shows as the quit gap',
          any(l.startswith('quit gap: 2490 ms') for l in lines))
    check('quit-only session: the 2490 ms gap shows as an excluded end gap',
          any(l.startswith('excluded end: 1') and '2490' in l for l in lines))
    lines = excluded_lines(got, frames, gate_spot=False)
    check('startup and menu gaps are listed, not dropped',
          any(l.startswith('excluded startup: 1') for l in lines) and any(l.startswith('excluded menu: 1') for l in lines))
    print('self-test PASS' if ok else 'self-test FAIL')
    return 0 if ok else 1


def main(argv):
    if argv[:1] == ['--self-test']:
        return self_test()
    limit = 1000.0
    if argv[:1] == ['--limit']:
        limit, argv = float(argv[1]), argv[2:]
    if not argv:
        print(__doc__)
        return 2
    total = sum(scan(folder, limit) for folder in argv)
    print(f'TOTAL freezes in play over {limit:.0f} ms: {total}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
