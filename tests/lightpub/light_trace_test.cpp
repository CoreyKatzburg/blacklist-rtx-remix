// Game-free test of lightcal::LightChangeTrace (light_calibration.h), the logic behind lightpub_trace.csv.
// Each case feeds the per-frame values lightpub would publish and checks the events the CSV would get.
#include <cstdio>
#include <vector>

#include "../../play-setup/unified-build/light_calibration.h"

using lightcal::TraceChange;
using lightcal::TraceEvent;

static int g_failures = 0;
#define CHECK(cond, ...)                                       \
    do {                                                       \
        if (!(cond)) {                                         \
            ++g_failures;                                      \
            std::printf("FAIL %s:%d: ", __FILE__, __LINE__);   \
            std::printf(__VA_ARGS__);                          \
            std::printf("\n");                                 \
        }                                                      \
    } while (0)

struct Recorded {
    uint32_t frame;
    TraceEvent e;
};

// brightnessAt(frame) < 0 means the light is not published that frame.
template <typename Fn>
std::vector<Recorded> Run(uint32_t frames, Fn brightnessAt, uint32_t (*flagsAt)(uint32_t) = nullptr) {
    lightcal::LightChangeTrace<8> trace;
    std::vector<Recorded> out;
    for (uint32_t f = 1; f <= frames; ++f) {
        auto emit = [&](const TraceEvent& e) { out.push_back({f, e}); };
        const float b = brightnessAt(f);
        if (b >= 0.0f) trace.See(0x1000, b, flagsAt ? flagsAt(f) : 0x21u, f, emit);
        trace.Sweep(f, emit);
    }
    return out;
}

static size_t Count(const std::vector<Recorded>& r, TraceChange c) {
    size_t n = 0;
    for (const auto& x : r) n += x.e.change == c;
    return n;
}

int main() {
    // A light the game cuts at once on frame 50: started, then stopped on frame 50 with its full brightness.
    {
        const auto r = Run(80, [](uint32_t f) { return f < 50 ? 2.25f : -1.0f; });
        CHECK(r.size() == 2, "step: %zu events, want 2", r.size());
        CHECK(r.size() == 2 && r[1].e.change == TraceChange::Stopped && r[1].frame == 50 && r[1].e.oldBrightness == 2.25f,
              "step: second event should be stopped at frame 50 from 2.25");
    }
    // A light the game ramps down like the custodial fade (half-life about 90 frames), then drops at frame 400.
    {
        const auto r = Run(420, [](uint32_t f) {
            if (f >= 400) return -1.0f;
            return f < 20 ? 1.0f : std::pow(0.5f, (f - 20) / 90.0f);
        });
        const size_t ramp = Count(r, TraceChange::Brightness);
        CHECK(ramp >= 20, "ramp: %zu brightness events, want a series of at least 20", ramp);
        bool falling = true;
        for (const auto& x : r)
            if (x.e.change == TraceChange::Brightness) falling = falling && x.e.newBrightness < x.e.oldBrightness;
        CHECK(falling, "ramp: every brightness event should go down");
        CHECK(!r.empty() && r.back().e.change == TraceChange::Stopped && r.back().frame == 400,
              "ramp: last event should be stopped at frame 400");
        // The first brightness event comes within a few frames of the ramp start.
        for (const auto& x : r)
            if (x.e.change == TraceChange::Brightness) {
                CHECK(x.frame > 20 && x.frame < 30, "ramp: first brightness event at frame %u, want 21..29", x.frame);
                break;
            }
    }
    // Flicker of +-2% (4% frame to frame, under the 5% step) writes nothing after started.
    {
        const auto r = Run(200, [](uint32_t f) { return 1.0f + 0.02f * ((f % 2) ? 1.0f : -1.0f); });
        CHECK(r.size() == 1 && r[0].e.change == TraceChange::Started, "flicker: %zu events, want only started", r.size());
    }
    // A flags change while brightness stays: one flags event with old and new words.
    {
        const auto r = Run(30, [](uint32_t) { return 1.0f; },
                           [](uint32_t f) -> uint32_t { return f < 10 ? 0x21u : 0x10021u; });
        CHECK(Count(r, TraceChange::Flags) == 1, "flags: want one flags event");
        for (const auto& x : r)
            if (x.e.change == TraceChange::Flags)
                CHECK(x.frame == 10 && x.e.oldFlags == 0x21u && x.e.newFlags == 0x10021u, "flags: wrong frame or words");
    }
    // Fake broken case: a light that the game switched off at frame 50 but that stays published at full brightness.
    // The trace must show no stopped event and no brightness change, so a check on the CSV can flag it.
    {
        const auto r = Run(200, [](uint32_t) { return 2.25f; });
        CHECK(Count(r, TraceChange::Stopped) == 0 && Count(r, TraceChange::Brightness) == 0,
              "stuck: a light that never changes must leave only its started event");
    }
    std::printf(g_failures ? "light_trace_test: %d FAILED\n" : "light_trace_test: all passed\n", g_failures);
    return g_failures ? 1 : 0;
}
