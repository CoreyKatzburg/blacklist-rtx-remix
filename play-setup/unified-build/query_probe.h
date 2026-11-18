#pragma once
// Query probe (worker B, flare orbs, X1 and X2). Both parts are off by default and change nothing while off.
//
// X1, [Proxy] QueryStatsFrames=N (read at load; 0 = off, 1..600 frames per window). Counts the game's own occlusion
// queries and, every N frames, logs one line a scorer can match to a switch step by its frame range:
//   "QueryProbe: frames A-B issued I answered R (pixels>0 P) polled Q (pixels>0 K) skipped scene-hdr S other O"
//   issued:   Issue(D3DISSUE_END) on occlusion queries the game has read at least once (the proxy's own bracket query
//             is issued but never read, so it is left out)
//   answered: GetData calls that handed the game a pixel count (S_OK); with QueryMode=1 this is the cached count the
//             game actually sees. pixels>0: of those, how many were above 0.
//   polled:   real GetData calls the proxy made to the bridge, and how many of them read above 0.
//   skipped:  draws X2 skipped in the window (0 while X2 is off).
// Installing the Issue hook needs the key at load, so a launch sets it in the ini before the game starts.
//
// X2, [Proxy] SkipBracketDraws=mask (reloads live; 0 = off). Under PassFilter, skips the non-scene draws the game
// makes after the proxy has handed Remix's picture to it (ModernFilter hand-off) and before the composite marker:
//   1 = draws onto the scene HDR target (sprites, particles, glows drawn over Remix's picture)
//   2 = draws onto any other target (the game's post passes before the composite)
// Scene draws, the hand-off draw, the composite and everything after the marker are never skipped.
// tests\hitch\run_query_probe_test.ps1 checks this header (normal build PASS, each mutant FAIL) and the hook sites.
#include <windows.h>

#include <cstdint>

namespace proxy {

constexpr uint32_t kQueryProbeMaxFrames = 600;

inline uint32_t ParseQueryStatsFrames(int raw) {
    if (raw <= 0) return 0;
    return static_cast<uint32_t>(raw) > kQueryProbeMaxFrames ? kQueryProbeMaxFrames : static_cast<uint32_t>(raw);
}

inline uint32_t ParseSkipBracketDraws(int raw) { return raw > 0 ? static_cast<uint32_t>(raw) & 3u : 0u; }

// 0 = draw, 1 = skip (onto the scene HDR target), 2 = skip (other target).
inline int SkipBracketDrawClass(uint32_t mask, bool passFilter, bool afterHandoff, bool markerDone, bool isScene,
                                bool rt0IsSceneHdr) {
#ifdef QUERY_PROBE_MUTANT_IGNORE_MARKER
    markerDone = false;
#endif
    if (!mask || !passFilter || !afterHandoff || markerDone || isScene) return 0;
    const int cls = rt0IsSceneHdr ? 1 : 2;
#ifdef QUERY_PROBE_MUTANT_IGNORE_MASK
    return cls;
#else
    return (mask & static_cast<uint32_t>(cls)) ? cls : 0;
#endif
}

struct QueryProbeCounts {
    uint32_t issued = 0, answered = 0, answeredNonZero = 0, polled = 0, polledNonZero = 0;
    uint32_t skippedSceneHdr = 0, skippedOther = 0;
};

// Window accumulator. Not thread safe by itself; the proxy calls it under its occlusion lock or on the render thread.
class QueryProbeWindow {
public:
    void SetFrames(uint32_t frames) { frames_ = frames; }
    uint32_t Frames() const { return frames_; }
    bool Enabled() const { return frames_ != 0; }
    void OnIssueEnd() { if (frames_) ++now_.issued; }
    void OnAnswer(uint32_t pixels) {
        if (!frames_) return;
        ++now_.answered;
        if (pixels > 0) ++now_.answeredNonZero;
    }
    void OnPoll(uint32_t pixels) {
        if (!frames_) return;
        ++now_.polled;
#ifdef QUERY_PROBE_MUTANT_POLL_COUNTS_ZERO
        (void)pixels;
        ++now_.polledNonZero;
#else
        if (pixels > 0) ++now_.polledNonZero;
#endif
    }
    void OnSkip(int cls) {
        if (cls == 1) ++now_.skippedSceneHdr;
        else if (cls == 2) ++now_.skippedOther;
    }
    // Call once per presented frame with that frame's number. Returns true when a window of Frames() frames ends at
    // `frame`; then *out holds its counts and *first its first frame, and the next window starts empty.
    bool EndFrame(uint32_t frame, QueryProbeCounts* out, uint32_t* first) {
        if (!frames_) { now_ = {}; seen_ = 0; return false; }
        if (seen_ == 0) first_ = frame;
        ++seen_;
#ifdef QUERY_PROBE_MUTANT_WINDOW_OFF_BY_ONE
        if (seen_ <= frames_) return false;
#else
        if (seen_ < frames_) return false;
#endif
        *out = now_;
        *first = first_;
        now_ = {};
        seen_ = 0;
        return true;
    }

private:
    uint32_t frames_ = 0, seen_ = 0, first_ = 0;
    QueryProbeCounts now_;
};

inline QueryProbeWindow g_queryProbe;
inline uint32_t g_skipBracketDraws = 0;
inline SRWLOCK g_queryProbeLock = SRWLOCK_INIT;

// Locked entry points for the proxy's hooks (query calls and draws may come from more than one thread).
inline void QueryProbeIssueEnd() {
    if (!g_queryProbe.Enabled()) return;
    AcquireSRWLockExclusive(&g_queryProbeLock);
    g_queryProbe.OnIssueEnd();
    ReleaseSRWLockExclusive(&g_queryProbeLock);
}
inline void QueryProbeAnswer(uint32_t pixels) {
    if (!g_queryProbe.Enabled()) return;
    AcquireSRWLockExclusive(&g_queryProbeLock);
    g_queryProbe.OnAnswer(pixels);
    ReleaseSRWLockExclusive(&g_queryProbeLock);
}
inline void QueryProbePoll(uint32_t pixels) {
    if (!g_queryProbe.Enabled()) return;
    AcquireSRWLockExclusive(&g_queryProbeLock);
    g_queryProbe.OnPoll(pixels);
    ReleaseSRWLockExclusive(&g_queryProbeLock);
}
inline void QueryProbeSkip(int cls) {
    AcquireSRWLockExclusive(&g_queryProbeLock);
    g_queryProbe.OnSkip(cls);
    ReleaseSRWLockExclusive(&g_queryProbeLock);
}
inline bool QueryProbeEndFrame(uint32_t frame, QueryProbeCounts* out, uint32_t* first) {
    if (!g_queryProbe.Enabled()) return false;
    AcquireSRWLockExclusive(&g_queryProbeLock);
    const bool closed = g_queryProbe.EndFrame(frame, out, first);
    ReleaseSRWLockExclusive(&g_queryProbeLock);
    return closed;
}

}  // namespace proxy
