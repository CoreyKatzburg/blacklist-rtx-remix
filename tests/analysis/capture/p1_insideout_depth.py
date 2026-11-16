"""Inside-out character symptom check (draft spec, offline).

Number: insideOutShare = share of character pixels where Remix depth lies behind the game's own depth by a body's
thickness, (max(3, 1% of game depth), 80] units. An inside-out character shows its far, inner surface, so Remix
hits it later than the game's front surface. Pixels behind by more than 80 are reported as missingShare (the
character is gone, problem 3), not as inside-out.

Character mask: the character's captured mesh projected at 320x180 (--mask, a .npy of its depth, inf outside),
taken from a USD capture of the same frame as both depth pictures. "Game depth below 80 units" is not a character
mask: at con_boiler y0_p0 it selects the near wall (draw 143), not Sam.

NOT MEASURED, in this order:
- the capture frame or the depth frame is not given, or they differ (guards move between frames);
- the character's capture copy shares its points with other draws (--piled > 1): the capture writes draws that
  share a mesh at one place, so the copy does not say where this character stands;
- the mask has fewer than 150 px;
- the mesh is misplaced: its depth agrees with the game depth (3% + 3 units) on under PLACE_MIN of its pixels;
- Remix and game depth agree on under 95% of the non-character pixels (the two pictures show different views).

Pass: insideOutShare <= 10%. GUESS: no frame with an inside-out character exists yet, so this limit is not taken
from data; set it from the first one.

usage: p1_insideout_depth.py <remix_depth.pfm> <game_depth.pfm> --mask <mesh_depth.npy>
           --capture-frame N --depth-frame N [--piled K]
       p1_insideout_depth.py --self-test"""
import sys, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cap_replace import pfm  # noqa: E402

PASS_MAX = 0.10   # GUESS, see above
# Share of the mesh's pixels that must sit on the game's depth. At con_custodial f7319 the two largest skinned
# copies (single-draw groups) agree on 84% and 91% against a still from the same pose; small ones lower (edges).
PLACE_MIN = 0.75


def score(R, G, mesh, capture_frame=None, depth_frame=None, piled=1):
    if capture_frame is None or depth_frame is None or capture_frame != depth_frame:
        return "NOT MEASURED", f"capture frame {capture_frame} and depth frame {depth_frame} are not one frame", None
    if piled > 1:
        return "NOT MEASURED", f"the copy's points are shared by {piled} draws, so it does not place this character", None
    mask = np.isfinite(mesh)
    if mask.sum() < 150:
        return "NOT MEASURED", f"character mask {int(mask.sum())} px < 150", None
    placed = (np.abs(mesh - G) <= 0.03 * G + 3)[mask].mean()
    if placed < PLACE_MIN:
        return "NOT MEASURED", f"mesh misplaced: on the game depth for {placed * 100:.0f}% of its px < {PLACE_MIN:.0%}", None
    d = R - G
    rest = ~mask
    agree = (np.abs(d[rest]) <= 0.02 * G[rest] + 2).mean()
    if agree < 0.95:
        return "NOT MEASURED", f"views differ: depth agrees on {agree * 100:.1f}% of non-character px", None
    tol = np.maximum(3.0, 0.01 * G)
    inside = ((d > tol) & (d <= 80))[mask].mean()
    missing = (d > 80)[mask].mean()
    verdict = "FAIL" if inside > PASS_MAX else "PASS"
    return verdict, (f"insideOutShare {inside * 100:.1f}%  missingShare {missing * 100:.1f}%  mask {int(mask.sum())} px  "
                     f"placed {placed * 100:.0f}%"), inside


def self_test():
    H, W = 180, 320
    G = np.full((H, W), 1000.0)
    body = np.zeros((H, W), bool); body[60:160, 140:180] = True   # a 40x100 px character
    G[body] = 40.0                                                 # its front surface
    mesh = np.where(body, 40.0, np.inf)                            # its captured mesh, nearest surface
    noise = np.random.default_rng(1).normal(0, 0.5, (H, W))
    F = dict(capture_frame=7319, depth_frame=7319)
    cases = []
    R = G + noise; cases.append(("correct character", R, mesh, F, "PASS"))
    R = G + noise; R[body] += 30; cases.append(("whole body inside-out (far side +30)", R, mesh, F, "FAIL"))
    R = G + noise; arms = body.copy(); arms[:, 150:170] = False; R[arms] += 25
    cases.append(("arms only inside-out (50% of px)", R, mesh, F, "FAIL"))
    R = G + noise; R[body] = 1000; cases.append(("character missing (wall behind)", R, mesh, F, "PASS"))
    R = np.roll(G, 60, axis=1) + noise; cases.append(("views do not match", R, mesh, F, "NOT MEASURED"))
    small = np.full((H, W), np.inf); small[0:5, 0:5] = 40.0
    Gs = np.where(np.isfinite(small), 40.0, 1000.0)
    inside_out = G + noise; inside_out[body] += 30
    cases += [
        ("mask under 150 px", Gs + noise, small, F, "NOT MEASURED"),
        ("inside-out, but depth from another frame", inside_out, mesh, dict(capture_frame=7319, depth_frame=6500), "NOT MEASURED"),
        ("inside-out, frames not given", inside_out, mesh, {}, "NOT MEASURED"),
        ("inside-out, copy piled with 9 draws", inside_out, mesh, dict(F, piled=9), "NOT MEASURED"),
        ("inside-out, mesh misplaced 60 px sideways", inside_out, np.roll(mesh, 60, axis=1), F, "NOT MEASURED"),
    ]
    for name, Rc, M, kw, want in cases:
        Gc = Gs if M is small else G
        got, why, _ = score(Rc, Gc, M, **kw)
        assert got == want, (name, got, why)
    R = G + noise; R[body] = 1000
    assert "missingShare 100.0%" in score(R, G, mesh, **F)[1]
    print(f"p1_insideout_depth self-test: PASS ({len(cases) + 1} cases)")


def main():
    if "--self-test" in sys.argv:
        return self_test()
    a = sys.argv[1:]
    opt = lambda k, f=int: f(a[a.index(k) + 1]) if k in a else None
    R, G = pfm(a[0]), pfm(a[1])
    v, why, _ = score(R, G, np.load(a[a.index("--mask") + 1]), opt("--capture-frame"), opt("--depth-frame"),
                      opt("--piled") or 1)
    print(v, why)


if __name__ == "__main__":
    main()
