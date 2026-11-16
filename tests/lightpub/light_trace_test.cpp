// Game-free test of lightcal::LightChangeTrace (light_calibration.h), the logic behind lightpub_trace.csv.
// Each case feeds the per-frame values lightpub would publish and checks the events the CSV would get.
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <string>
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

// The publish steps of lightpub.cpp SubmitLights for one frame, with the keys KeepTrackedLights (keep) and
// WeaponSpotByShape (tuning.weaponSpotByShape), counting what the stray line and Remix would see.
struct Node {
    uintptr_t id;
    lightcal::NodeData data;
};
struct Lifecycle {
    bool keep = false;
    lightcal::Tuning tuning;
    lightcal::LightLifecycleTracker<16> tracker;
    lightcal::DroppedWhilePresent<16> dropped;
    uint32_t frame = 0, created = 0, destroyed = 0, recreated = 0, weaponFlips = 0;
    uintptr_t nextHandle = 0x100;
    void Step(const std::vector<Node>& database) {
        ++frame;
        for (const Node& n : database) {
            const lightcal::TrackedEntry* t = tracker.Find(n.id);
            const bool wasPublished = t && t->handle;
            lightcal::PublishedLight pub;
            if (!lightcal::ConvertGameLight(n.data, tuning, &pub, keep && wasPublished)) continue;
            void* handle = nullptr;
            uint32_t made = 0;
            tracker.Upsert(n.id, 0, pub, frame,
                           [&](uint64_t, const lightcal::PublishedLight&) { return reinterpret_cast<void*>(nextHandle++); },
                           &made, nullptr, &handle);
            created += made;
            if (!wasPublished && dropped.Take(n.id)) ++recreated;
            if (n.data.type == 2 &&
                lightcal::NoteWeaponClass(tracker.Find(n.id), lightcal::IsWeaponSpotlight(n.data, tuning.weaponSpotByShape)))
                ++weaponFlips;
        }
        auto present = [&](uintptr_t id) {
            for (const Node& n : database)
                if (n.id == id) return true;
            return false;
        };
        for (size_t i = 0; i < tracker.Count(); ++i) {
            const auto& e = tracker.Entries()[i];
            if (e.lastSeen != frame && e.handle && present(e.node)) dropped.Note(e.node);
        }
        dropped.Prune(present);
        tracker.SweepUnseen(frame, [&](void*) { ++destroyed; });
    }
    void* Handle(uintptr_t id) {
        const lightcal::TrackedEntry* t = tracker.Find(id);
        return t ? t->handle : nullptr;
    }
};

static lightcal::NodeData Light(uint32_t type, float brightness, float range = 150.0f) {
    lightcal::NodeData n;
    n.type = type;
    n.orientation[0][0] = n.orientation[1][1] = n.orientation[2][2] = 1.0f;
    n.range = range;
    n.falloffStart = range * 0.5f;
    n.color[0] = brightness; n.color[1] = brightness * 0.5f; n.color[2] = brightness * 0.25f;
    n.intensity = 1.0f;
    return n;
}
// The guard flashlight as the dumps show it: type 2, near plane 50 x 50 at 150, with a range that wanders.
static lightcal::NodeData GuardFlashlight(float range) {
    lightcal::NodeData n = Light(2, 1.0f, range);
    n.nearSize[0] = n.nearSize[1] = 50.0f;
    n.nearDistance = 150.0f;
    return n;
}

static void KeyCases() {
    // KeepTrackedLights: Consumption's pulsing group dips to about 1e-4 once per cycle. With the key on, the light keeps
    // its Remix handle through the dip and nothing is destroyed or re-created. With it off (today), the dip costs a
    // destroy and a re-create, which is the old behaviour the key exists to remove.
    for (const bool keep : {true, false}) {
        Lifecycle s;
        s.keep = keep;
        s.Step({{1, Light(1, 0.8f)}});
        void* const first = s.Handle(1);
        s.Step({{1, Light(1, 5e-5f)}});     // the dip, below Qualifies' 1e-4 start threshold
        s.Step({{1, Light(1, 0.8f)}});
        if (keep)
            CHECK(s.destroyed == 0 && s.recreated == 0 && s.created == 1 && s.Handle(1) == first,
                  "keep tracked: dip destroyed %u recreated %u created %u, want 0 0 1 and the same handle", s.destroyed,
                  s.recreated, s.created);
        else
            CHECK(s.destroyed == 1 && s.recreated == 1 && s.created == 2,
                  "key off: dip destroyed %u recreated %u created %u, want 1 1 2 (today's re-create)", s.destroyed,
                  s.recreated, s.created);
    }
    // KeepTrackedLights must not keep a light whose node the game removed, and its return is not a re-create.
    {
        Lifecycle s;
        s.keep = true;
        s.Step({{1, Light(1, 0.8f)}, {2, Light(1, 0.8f)}});
        s.Step({{2, Light(1, 0.8f)}});
        CHECK(s.destroyed == 1 && !s.Handle(1), "keep tracked: removed node destroyed %u, want 1", s.destroyed);
        s.Step({{1, Light(1, 0.8f)}, {2, Light(1, 0.8f)}});
        CHECK(s.recreated == 0, "keep tracked: a node the game removed and added back counted %u re-creates, want 0",
              s.recreated);
    }
    // A light dropped while its node stayed, whose node the game then removes, is forgotten: a new light at the same
    // address later is not a re-create.
    {
        Lifecycle s;
        s.Step({{1, Light(1, 0.8f)}});
        s.Step({{1, Light(1, 5e-5f)}});   // dropped while present (key off)
        s.Step({});                       // the game removes the node
        s.Step({{1, Light(1, 0.8f)}});
        CHECK(s.recreated == 0, "removed after a drop: counted %u re-creates, want 0", s.recreated);
    }
    // KeepTrackedLights must not grow the light count: a light never published is not created at 1e-4 or at 0.
    {
        Lifecycle s;
        s.keep = true;
        s.Step({{1, Light(1, 1e-4f)}, {2, Light(2, 0.0f)}, {3, Light(1, 0.8f)}});
        s.Step({{1, Light(1, 1e-4f)}, {2, Light(2, 0.0f)}, {3, Light(1, 0.8f)}});
        CHECK(s.created == 1 && !s.Handle(1) && !s.Handle(2), "keep tracked: created %u lights, want only the lit one",
              s.created);
    }
    // WeaponSpotByShape: a guard flashlight at range 545 is weapon class by shape; the range rule (<= 100) says no.
    {
        lightcal::Tuning shape;
        shape.weaponSpotByShape = true;
        lightcal::PublishedLight byShape, byRange;
        CHECK(lightcal::ConvertGameLight(GuardFlashlight(545.0f), shape, &byShape) &&
                  lightcal::ConvertGameLight(GuardFlashlight(545.0f), lightcal::Tuning{}, &byRange),
              "guard flashlight must convert");
        CHECK(lightcal::IsWeaponSpotlight(GuardFlashlight(545.0f), true) && byShape.radius == lightcal::kWeaponSpotRadiusCm,
              "by shape: guard flashlight at range 545 is not weapon class (radius %g)", byShape.radius);
        CHECK(!lightcal::IsWeaponSpotlight(GuardFlashlight(545.0f)) && byRange.radius != lightcal::kWeaponSpotRadiusCm,
              "range rule: guard flashlight at range 545 should be a ball, as today");
    }
    // WeaponSpotByShape must never take an architectural spot: range 550, near plane 100 x 100 at 80.
    {
        lightcal::NodeData n = Light(2, 1.0f, 550.0f);
        n.nearSize[0] = n.nearSize[1] = 100.0f;
        n.nearDistance = 80.0f;
        CHECK(!lightcal::IsWeaponSpotlight(n, true) && !lightcal::IsWeaponSpotlight(n),
              "architectural spot 550 / 100x100 at 80 classified as a weapon light");
    }
    // The "weapon flips" count: a guard flashlight whose range crosses 100 between frames flips under today's rule
    // and never by shape.
    for (const bool byShape : {false, true}) {
        Lifecycle s;
        s.tuning.weaponSpotByShape = byShape;
        for (int f = 0; f < 6; ++f) s.Step({{7, GuardFlashlight(f % 2 ? 545.0f : 80.0f)}});
        CHECK(s.weaponFlips == (byShape ? 0u : 5u), "weapon flips by %s: %u, want %u", byShape ? "shape" : "range",
              s.weaponFlips, byShape ? 0u : 5u);
    }
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
    {   // sharing: another process (the gate harness) must be able to read the trace while the game writes it
        const std::wstring path = (std::filesystem::temp_directory_path() / L"lightpub_trace_share_test.csv").wstring();
        auto readable = [&](FILE* writer) {
            if (!writer) return false;
            std::fputs("frame,node,id,change,old,new\n", writer);
            std::fflush(writer);
            FILE* reader = _wfsopen(path.c_str(), L"r", _SH_DENYNO);
            char line[64] = {};
            const bool ok = reader && std::fgets(line, sizeof line, reader) && std::strncmp(line, "frame,", 6) == 0;
            if (reader) std::fclose(reader);
            std::fclose(writer);
            return ok;
        };
        CHECK(readable(lightcal::OpenTraceFile(path.c_str(), false)), "share: trace file not readable while open");
        // Fake case: the old open (_wfopen_s, no sharing) must fail this check, or the check proves nothing.
        FILE* old = nullptr;
        _wfopen_s(&old, path.c_str(), L"w");
        CHECK(!readable(old), "share: the check passed on the old unshared open");
        std::filesystem::remove(path);
    }
    KeyCases();
    std::printf(g_failures ? "light_trace_test: %d FAILED\n" : "light_trace_test: all passed\n", g_failures);
    return g_failures ? 1 : 0;
}
