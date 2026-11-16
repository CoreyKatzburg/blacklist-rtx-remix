// Game-free test of the flare stand-in choice and the stray-light counts in light_calibration.h (WantsStandIn,
// FlareMotion, CountLampsWithoutFlare, LightChangeTrace::DimStats). Part of it replays the saved run20 dumps
// (fixtures/run20/*_lights.tsv, Consumption, 185 flares): the numbers it expects were counted independently by
// .amp/in/workerB/analyze_lights.py and flare_groups.py from the same files.
// usage: flare_standin_test.exe <fixtures/run20 folder>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include "../../play-setup/unified-build/light_calibration.h"

using lightcal::FlareData;
using lightcal::StandInOptions;

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

struct Dump {
    std::vector<std::pair<uintptr_t, FlareData>> flares;
    std::vector<std::vector<float>> lamps;  // published point/spot light positions
};

static std::vector<std::string> Split(const std::string& s, char sep) {
    std::vector<std::string> out;
    std::stringstream ss(s);
    for (std::string part; std::getline(ss, part, sep);) out.push_back(part);
    return out;
}

static std::vector<float> Floats(const std::string& s) {
    std::vector<float> out;
    for (const auto& p : Split(s, ',')) out.push_back(std::strtof(p.c_str(), nullptr));
    return out;
}

// Reads the dump format lightpub's DumpLights writes (key=value fields after the line kind). Flare node flags and
// +0x34 come from the raw words (raw= starts at +0x20), which both the old and the relabelled dumps carry.
static Dump Load(const std::filesystem::path& path) {
    Dump d;
    std::ifstream in(path);
    for (std::string line; std::getline(in, line);) {
        const auto parts = Split(line, '\t');
        if (parts.empty()) continue;
        std::map<std::string, std::string> kv;
        for (size_t i = 1; i < parts.size(); ++i) {
            const size_t eq = parts[i].find('=');
            if (eq != std::string::npos) kv[parts[i].substr(0, eq)] = parts[i].substr(eq + 1);
        }
        if (parts[0] == "light") {
            const int type = std::atoi(kv["type"].c_str());
            if (kv["tracked"] == "1" && (type == 1 || type == 2)) d.lamps.push_back(Floats(kv["pos"]));
        } else if (parts[0] == "flare") {
            FlareData f;
            const auto pos = Floats(kv["pos"]);
            for (int k = 0; k < 3; ++k) f.pos[k] = pos[k];
            std::vector<uint32_t> raw;
            std::stringstream words(kv["raw"]);
            for (std::string w; words >> w;) raw.push_back(static_cast<uint32_t>(std::strtoul(w.c_str(), nullptr, 16)));
            f.nodeFlags = raw.size() > 3 ? raw[(0x2C - 0x20) / 4] : 0;
            f.word34 = raw.size() > 5 ? raw[(0x34 - 0x20) / 4] : 0;
            // Brightest element, as CollectFlares picks it (first 8 elements).
            for (int i = 0; i < 8; ++i) {
                const auto it = kv.find("e" + std::to_string(i));
                if (it == kv.end()) break;
                const auto v = Split(it->second, ',');  // i<intensity>, s<sx>, <sy>, c<r>, g, b, a
                const float intensity = std::strtof(v[0].c_str() + 1, nullptr);
                const float sx = std::strtof(v[1].c_str() + 1, nullptr), sy = std::strtof(v[2].c_str(), nullptr);
                if (intensity > f.peakIntensity) {
                    f.peakIntensity = intensity;
                    if (sx > 0.0f && sy > 0.0f) f.peakSize = 0.5f * (sx + sy);
                }
            }
            d.flares.push_back({static_cast<uintptr_t>(std::strtoull(kv["node"].c_str(), nullptr, 16)), f});
        }
    }
    return d;
}

static size_t Picked(const Dump& d, const StandInOptions& o, size_t* onUnplaced = nullptr) {
    size_t n = 0, unplaced = 0;
    for (const auto& [node, f] : d.flares)
        if (lightcal::WantsStandIn(f, o)) {
            ++n;
            unplaced += (f.nodeFlags & lightcal::kFlareNodeUnplaced) != 0;
        }
    if (onUnplaced) *onUnplaced = unplaced;
    return n;
}

static void FakeCases() {
    // A bright flare the scene never placed: bit 0 set, at the world origin.
    FlareData unplaced;
    unplaced.nodeFlags = 0x7701;
    unplaced.peakIntensity = 3.5f;
    unplaced.peakSize = 120.0f;
    StandInOptions today;
    CHECK(lightcal::WantsStandIn(unplaced, today), "defaults must keep today's choice: a ball on the unplaced flare");
    StandInOptions skipUnplaced;
    skipUnplaced.skipUnplaced = true;
    CHECK(!lightcal::WantsStandIn(unplaced, skipUnplaced), "StandInSkipUnplaced=1 must drop the unplaced flare");
    FlareData placed = unplaced;
    placed.nodeFlags = 0x7700;
    CHECK(lightcal::WantsStandIn(placed, skipUnplaced), "StandInSkipUnplaced=1 must keep a placed flare");
    StandInOptions off;
    off.enabled = false;
    CHECK(!lightcal::WantsStandIn(placed, off), "FlareStandIns=0 must draw nothing");
    FlareData camera = placed;
    camera.word34 = 0x3F80000F;  // 0x2 set: camera space
    CHECK(!lightcal::WantsStandIn(camera, today), "camera-space flares never get a ball");
    FlareData sky = placed;
    sky.peakSize = 400.0f;
    CHECK(!lightcal::WantsStandIn(sky, today), "sky-sized flares (size 400) never get a ball");

    // Motion: a gear flare that drifts 0.9 cm (the smallest move seen on Sam's gear) is moving from that frame on,
    // even after it comes back; a lamp flare that stays put, or drifts 0.4 cm, is not.
    lightcal::FlareMotion<4> motion;
    const float home[3] = {100.0f, 200.0f, 50.0f}, drift[3] = {100.9f, 200.0f, 50.0f}, tiny[3] = {100.0f, 200.4f, 50.0f};
    CHECK(!motion.Update(1, home, 1) && !motion.Update(2, home, 1), "first sight is never moved");
    CHECK(motion.Update(1, drift, 2), "a 0.9 cm drift counts as moved");
    CHECK(motion.Update(1, home, 3), "moved stays set after the flare comes back");
    CHECK(!motion.Update(2, tiny, 2) && !motion.Update(2, tiny, 3), "a 0.4 cm drift is not moved");
    motion.Sweep(3);
    CHECK(!motion.Update(3, drift, 4), "a new node starts unmoved");
    StandInOptions skipMoving;
    skipMoving.skipMoving = true;
    placed.moved = true;
    CHECK(lightcal::WantsStandIn(placed, today) && !lightcal::WantsStandIn(placed, skipMoving),
          "StandInSkipMoving=1 drops a moved flare, the default keeps it");

    // Lamps without a flare: the boundary is 100 cm inclusive, and a flare far away does not count.
    FlareData glow;
    glow.pos[0] = 100.0f;
    const float lamps[3][3] = {{0, 0, 0}, {50, 0, 0}, {500, 0, 0}};  // 100 cm, 50 cm and 400 cm from the flare
    CHECK(lightcal::CountLampsWithoutFlare(lamps, 3, &glow, 1) == 1, "one of three lamps has no flare within 100 cm");
    const float lamp101[1][3] = {{-1, 0, 0}};
    CHECK(lightcal::CountLampsWithoutFlare(lamp101, 1, &glow, 1) == 1, "101 cm away is outside");
    CHECK(lightcal::CountLampsWithoutFlare(lamps, 3, nullptr, 0) == 3, "no flares at all: every lamp is lonely");
}

static void DimCases() {
    auto ignore = [](const lightcal::TraceEvent&) {};
    // The custodial-style light: full for 50 frames, then at 5% of its peak but still published for 100 frames.
    {
        lightcal::LightChangeTrace<4> trace;
        uint32_t dimmed = 0, longest = 0;
        for (uint32_t f = 1; f <= 150; ++f) {
            trace.See(0x10, f <= 50 ? 2.0f : 0.1f, 0x21, f, ignore);
            trace.See(0x20, 1.0f, 0x21, f, ignore);
            trace.Sweep(f, ignore);
        }
        trace.DimStats(150, &dimmed, &longest);
        CHECK(dimmed == 1 && longest == 100, "dimmed light: got %u dimmed, longest %u frames, want 1 and 100",
              dimmed, longest);
    }
    // A flicker that dips under 10% for one frame every 10 frames never stays dimmed longer than one frame.
    {
        lightcal::LightChangeTrace<4> trace;
        uint32_t worst = 0;
        for (uint32_t f = 1; f <= 100; ++f) {
            trace.See(0x10, f % 10 ? 1.0f : 0.05f, 0x21, f, ignore);
            trace.Sweep(f, ignore);
            uint32_t dimmed = 0, longest = 0;
            trace.DimStats(f, &dimmed, &longest);
            worst = std::max(worst, longest);
        }
        CHECK(worst == 1, "flicker: longest dim run %u frames, want 1", worst);
    }
    // A light that is cut (stops publishing) is not counted as dimmed.
    {
        lightcal::LightChangeTrace<4> trace;
        for (uint32_t f = 1; f <= 20; ++f) {
            if (f <= 10) trace.See(0x10, 2.0f, 0x21, f, ignore);
            trace.Sweep(f, ignore);
        }
        uint32_t dimmed = 1, longest = 1;
        trace.DimStats(20, &dimmed, &longest);
        CHECK(dimmed == 0 && longest == 0, "a stopped light must not count as dimmed");
    }
}

static void Run20(const std::filesystem::path& folder) {
    std::vector<std::filesystem::path> files;
    for (const auto& e : std::filesystem::directory_iterator(folder))
        if (e.path().filename().string().size() > 11 &&
            e.path().filename().string().rfind("_lights.tsv") == e.path().filename().string().size() - 11)
            files.push_back(e.path());
    std::sort(files.begin(), files.end());
    CHECK(files.size() == 7, "run20: %zu dumps, want 7", files.size());
    if (files.empty()) return;

    const Dump camOn = Load(folder / "cam_on_lights.tsv");
    CHECK(camOn.flares.size() == 185, "cam_on: %zu flares, want 185", camOn.flares.size());
    size_t unplaced = 0;
    const size_t today = Picked(camOn, StandInOptions{}, &unplaced);
    CHECK(today == 109 && unplaced == 15, "cam_on defaults: %zu balls, %zu on unplaced flares, want 109 and 15",
          today, unplaced);
    StandInOptions skipUnplaced;
    skipUnplaced.skipUnplaced = true;
    const size_t skipped = Picked(camOn, skipUnplaced, &unplaced);
    CHECK(skipped == 94 && unplaced == 0, "cam_on StandInSkipUnplaced: %zu balls, %zu on unplaced, want 94 and 0",
          skipped, unplaced);
    size_t atOrigin = 0;
    for (const auto& [node, f] : camOn.flares)
        atOrigin += lightcal::WantsStandIn(f, StandInOptions{}) && f.pos[0] == 0 && f.pos[1] == 0 && f.pos[2] == 0;
    CHECK(atOrigin == 15, "cam_on: %zu balls drawn at the world origin today, want 15", atOrigin);

    std::vector<FlareData> flares;
    for (const auto& p : camOn.flares) flares.push_back(p.second);
    std::vector<float> lampPos;
    for (const auto& l : camOn.lamps) lampPos.insert(lampPos.end(), l.begin(), l.begin() + 3);
    const uint32_t lonely = lightcal::CountLampsWithoutFlare(reinterpret_cast<const float(*)[3]>(lampPos.data()),
                                                             camOn.lamps.size(), flares.data(), flares.size());
    CHECK(camOn.lamps.size() == 120 && lonely == 59, "cam_on: %zu lamps, %u without a flare, want 120 and 59",
          camOn.lamps.size(), lonely);

    // Motion over the session's dumps, one dump per frame: 21 flares move (10 guards' lights, 11 on Sam).
    lightcal::FlareMotion<512> motion;
    std::map<uintptr_t, bool> moved;
    uint32_t frame = 0;
    for (const auto& file : files) {
        ++frame;
        for (const auto& [node, f] : Load(file).flares) moved[node] = motion.Update(node, f.pos, frame);
        motion.Sweep(frame);
    }
    size_t movedCount = 0;
    for (const auto& m : moved) movedCount += m.second;
    CHECK(movedCount == 21, "run20: %zu flares moved across the dumps, want 21", movedCount);
}

int main(int argc, char** argv) {
    FakeCases();
    DimCases();
    if (argc > 1) Run20(argv[1]);
    else CHECK(false, "pass the fixtures/run20 folder");
    std::printf(g_failures ? "flare_standin_test: %d FAILED\n" : "flare_standin_test: all passed\n", g_failures);
    return g_failures ? 1 : 0;
}
