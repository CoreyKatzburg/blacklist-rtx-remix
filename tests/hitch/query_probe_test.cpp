// Checks play-setup\unified-build\query_probe.h (worker B, X1 QueryStatsFrames and X2 SkipBracketDraws).
// Built by run_query_probe_test.ps1: the normal build must PASS; each mutant define must FAIL.
// Exit code 0 = all cases pass, 1 = a case failed.
#include "query_probe.h"

#include <cstdio>

namespace {
int g_failed = 0;

void Expect(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) ++g_failed;
}
}  // namespace

int main() {
    using namespace proxy;

    // Keys: off unless set; out-of-range values clamp.
    Expect(ParseQueryStatsFrames(0) == 0 && ParseQueryStatsFrames(-5) == 0, "QueryStatsFrames 0 or negative is off");
    Expect(ParseQueryStatsFrames(30) == 30, "QueryStatsFrames 30 kept");
    Expect(ParseQueryStatsFrames(5000) == kQueryProbeMaxFrames, "QueryStatsFrames clamps to 600");
    Expect(ParseSkipBracketDraws(0) == 0 && ParseSkipBracketDraws(-1) == 0, "SkipBracketDraws 0 or negative is off");
    Expect(ParseSkipBracketDraws(1) == 1 && ParseSkipBracketDraws(2) == 2 && ParseSkipBracketDraws(3) == 3,
           "SkipBracketDraws 1, 2, 3 kept");
    Expect(ParseSkipBracketDraws(7) == 3, "SkipBracketDraws keeps only bits 1 and 2");

    // Off path: mask 0 never skips, whatever the draw.
    bool anySkipWhenOff = false;
    for (int bits = 0; bits < 32; ++bits)
        anySkipWhenOff |= SkipBracketDrawClass(0, bits & 1, bits & 2, bits & 4, bits & 8, bits & 16) != 0;
    Expect(!anySkipWhenOff, "mask 0 skips nothing (all 32 draw states)");

    // In the span (pass filter on, after the hand-off, before the marker, non-scene draw).
    Expect(SkipBracketDrawClass(1, true, true, false, false, true) == 1, "mask 1 skips a draw onto scene HDR");
    Expect(SkipBracketDrawClass(1, true, true, false, false, false) == 0, "mask 1 keeps a draw onto another target");
    Expect(SkipBracketDrawClass(2, true, true, false, false, false) == 2, "mask 2 skips a draw onto another target");
    Expect(SkipBracketDrawClass(2, true, true, false, false, true) == 0, "mask 2 keeps a draw onto scene HDR");
    Expect(SkipBracketDrawClass(3, true, true, false, false, true) == 1 &&
               SkipBracketDrawClass(3, true, true, false, false, false) == 2,
           "mask 3 skips both");

    // Outside the span nothing is skipped, even with mask 3.
    Expect(SkipBracketDrawClass(3, true, true, true, false, true) == 0, "after the composite marker: kept");
    Expect(SkipBracketDrawClass(3, true, false, false, false, true) == 0, "before the hand-off: kept");
    Expect(SkipBracketDrawClass(3, true, true, false, true, true) == 0, "scene draw: kept");
    Expect(SkipBracketDrawClass(3, false, true, false, false, true) == 0, "PassFilter off: kept");

    // Window: disabled counts nothing.
    QueryProbeWindow off;
    off.OnIssueEnd();
    off.OnAnswer(5);
    off.OnPoll(5);
    QueryProbeCounts c;
    uint32_t first = 0;
    bool closed = false;
    for (uint32_t f = 1; f <= 700; ++f) closed |= off.EndFrame(f, &c, &first);
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
