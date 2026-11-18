// Checks play-setup\unified-build\query_probe.h (worker B, X1 QueryStatsFrames and the rebuilt X2 SkipBracketDraws).
// Built by run_query_probe_test.ps1 with the real pass_graph.cpp: the normal build must PASS; each mutant must FAIL.
// Exit code 0 = all cases pass, 1 = a case failed.
#include "pass_graph.h"
#include "query_probe.h"

#include <cstdio>

namespace {
int g_failed = 0;

void Expect(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) ++g_failed;
}

// One draw through the order of draw_filter.cpp PassFilterBeforeDraw's MarkerDoneThisFrame() branch: the composite
// takes Remix's picture, then ModernFilter skips draws onto other targets or reading the scene HDR, then X2.
// Returns 1 when X2 skips the draw (and counts it in the window), 0 otherwise.
struct Frame {
    proxy::PassGraph* pg;
    bool awaiting = false;
};
int Draw(Frame& f, uint32_t key, bool isComposite, bool ontoSceneHdr, bool readsSceneHdr) {
    if (!f.pg->MarkerDoneThisFrame()) return 0;
    if (!f.awaiting) return 0;
    if (isComposite) { f.awaiting = false; return 0; }
    if (!ontoSceneHdr || readsSceneHdr) return 0;
    const int cls = proxy::SkipAfterHandoffClass(key, true, f.pg->MarkerDoneThisFrame(), f.awaiting, isComposite,
                                                 ontoSceneHdr, readsSceneHdr);
    if (cls) proxy::g_queryProbe.OnSkip(cls);
    return cls;
}
}  // namespace

int main() {
    using namespace proxy;

    // Keys: off unless set.
    Expect(ParseQueryStatsFrames(0) == 0 && ParseQueryStatsFrames(-5) == 0, "QueryStatsFrames 0 or negative is off");
    Expect(ParseQueryStatsFrames(30) == 30, "QueryStatsFrames 30 kept");
    Expect(ParseQueryStatsFrames(5000) == kQueryProbeMaxFrames, "QueryStatsFrames clamps to 600");
    Expect(ParseSkipBracketDraws(0) == 0 && ParseSkipBracketDraws(-1) == 0, "SkipBracketDraws 0 or negative is off");
    Expect(ParseSkipBracketDraws(1) == 1 && ParseSkipBracketDraws(2) == 1 && ParseSkipBracketDraws(7) == 1,
           "SkipBracketDraws 1, 2, 7 read as on");

    // Off path: key 0 never skips, over every flag combination.
    bool anySkipWhenOff = false;
    for (int bits = 0; bits < 64; ++bits)
        anySkipWhenOff |= SkipAfterHandoffClass(0, bits & 1, bits & 2, bits & 4, bits & 8, bits & 16, bits & 32) != 0;
    Expect(!anySkipWhenOff, "key 0 skips nothing (all 64 flag states)");
    Expect(SkipAfterHandoffClass(1, false, true, true, false, true, false) == 0, "PassFilter off: kept");
    Expect(SkipAfterHandoffClass(1, true, true, true, false, true, true) == 0, "a draw reading the scene HDR: kept");
    Expect(SkipAfterHandoffClass(1, true, true, true, false, false, false) == 0, "a draw onto another target: kept");

    // Real call order with the real PassGraph.
    PassGraphConfig cfg;
    cfg.enabled = true;
    cfg.modernFilter = true;
    PassGraph pg;
    pg.SetConfig(cfg);
    Frame f{&pg};
    g_queryProbe.SetFrames(1);
    QueryProbeCounts c;
    uint32_t first = 0;

    Expect(!pg.MarkerDoneThisFrame(), "frame start: marker not done");
    Expect(Draw(f, 1, false, true, false) == 0, "before the hand-off: a scene HDR draw is kept");
    pg.NoteHandoffCompleted();      // handoff_renderer.cpp HandoffBeforePost -> pass_graph.cpp NoteHandoffCompleted
    f.awaiting = pg.MarkerDoneThisFrame() && cfg.modernFilter;   // draw_filter.cpp: g_modernAwaitingComposite = true
    Expect(f.awaiting, "after the hand-off: marker done and awaiting the composite");
    Expect(Draw(f, 0, false, true, false) == 0, "after the hand-off, key 0: the scene HDR draw is kept");
    Expect(Draw(f, 1, false, true, false) == 1, "after the hand-off, key 1: a scene HDR draw is skipped");
    Expect(Draw(f, 1, false, true, true) == 0, "a draw reading the scene HDR is left to ModernFilter (not X2)");
    Expect(Draw(f, 1, false, false, false) == 0, "a draw onto another target is left to ModernFilter (not X2)");
    Expect(Draw(f, 1, false, true, false) == 1, "a second scene HDR draw is skipped");
    Expect(Draw(f, 1, true, true, false) == 0, "the composite draw is kept");
    Expect(Draw(f, 1, false, true, false) == 0, "after the composite: a scene HDR draw is kept");
    Expect(g_queryProbe.EndFrame(100, &c, &first), "window of 1 frame closes");
    Expect(c.skippedSceneHdr == 2 && c.skippedOther == 0, "skipped scene-hdr 2, other 0 (fails if it stays 0)");

    pg.EndFrame(nullptr, true);
    f.awaiting = false;
    Expect(!pg.MarkerDoneThisFrame(), "next frame: marker reset");
    Expect(Draw(f, 1, false, true, false) == 0, "next frame before its hand-off: kept");
    g_queryProbe.SetFrames(0);

    // Window: disabled counts nothing.
    QueryProbeWindow off;
    off.OnIssueEnd();
    off.OnAnswer(5);
    off.OnPoll(5);
    bool closed = false;
    for (uint32_t fr = 1; fr <= 700; ++fr) closed |= off.EndFrame(fr, &c, &first);
    Expect(!closed, "window off: no line in 700 frames");

    // Window of 3 frames: counts, first frame, and the next window starting empty.
    QueryProbeWindow w;
    w.SetFrames(3);
    w.OnIssueEnd();
    w.OnAnswer(0);
    w.OnPoll(0);
    Expect(!w.EndFrame(10, &c, &first), "frame 10 of 3: window open");
    w.OnIssueEnd();
    w.OnAnswer(5);
    w.OnSkip(1);
    w.OnSkip(2);
    Expect(!w.EndFrame(11, &c, &first), "frame 11 of 3: window open");
    w.OnAnswer(7);
    w.OnPoll(9);
    w.OnSkip(2);
    w.OnSkip(0);
    closed = w.EndFrame(12, &c, &first);
    Expect(closed && first == 10, "window closes at frame 12 and starts at 10");
    Expect(c.issued == 2, "issued 2");
    Expect(c.answered == 3 && c.answeredNonZero == 2, "answered 3, of them 2 above 0");
    Expect(c.polled == 2 && c.polledNonZero == 1, "polled 2, of them 1 above 0");
    Expect(c.skippedSceneHdr == 1 && c.skippedOther == 2, "skipped scene-hdr 1, other 2");
    Expect(!w.EndFrame(13, &c, &first) && !w.EndFrame(14, &c, &first), "next window open at 13 and 14");
    closed = w.EndFrame(15, &c, &first);
    Expect(closed && first == 13 && c.issued == 0 && c.answered == 0 && c.polled == 0 && c.skippedOther == 0,
           "next window 13-15 starts empty");

    std::printf("%s: %d failed\n", g_failed ? "FAIL" : "PASS", g_failed);
    return g_failed ? 1 : 0;
}
