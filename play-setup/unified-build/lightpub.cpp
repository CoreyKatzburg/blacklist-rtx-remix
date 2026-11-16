#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include "lightpub.h"

#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "auto_raytracing.h"
#include "light_calibration.h"
#include "proxy_log.h"
// The 32-bit bridge client implements this same API and forwards it to the 64-bit runtime.
#define REMIX_ALLOW_X86
#include "ref/remix_c.h"

// Blacklist_game.exe (DX9, SHA-256 158EFC5D...5A05, fixed base 0x00400000, no ASLR).
// Mapped in experiments\remix-graphics\NOTES.md and confirmed by a runtime dump (G1, 174 nodes).
//   l3d object database: global pointer at 0x032F5A58
//     +0x1C  class registry (hash table: +0x08 buckets of {first, last}, +0x0C bucket count;
//            records chain through +0x50, record +0x04 = name hash, +0x10 = class index)
//     +0x2C  per-class-index array of list heads (head -> first object), +0x30 entry count
//     +0x78  -> pointer to the database CRITICAL_SECTION
//   objects: +0x1C class record, +0x28 next object of the same class
//   FUN_018B1750(const char*) = name hash used by the registry (pure function)
//   l3d::LightNode (vtable 0x02BE66C8, size 0x470):
//     +0x034 flags (& 7: 1 point, 2 spot, 3 directional, 4 SH probe; 0x400 = probe used by the engine)
//     +0x230 type 4: 7 float4 spherical-harmonics irradiance (cSHParams), +0x440 type 4: probe radius
//     +0x350 world position, +0x360 3x3 orientation (4x4 storage); column 0 is the way the light shines
//     +0x3A0/+0x3A4/+0x3A8 spot beam: width and height of its near plane, and that plane's distance from the
//            beam's apex. The light's position is the middle of the near plane.
//     +0x3B4 range, +0x3BC falloff start, +0x3C4 linear RGB colour, +0x44C intensity
//   Checked 2026-11-10 against the constants the game's own light shaders get (frame dump run2, 19 spots, the sun):
//   spot ps 679A8A28 cSpot0.proj, sun ps BDBFAC7B cDir0.dir (= minus column 0, the direction TO the light).
//   How these numbers become Remix lights: light_calibration.h (the probe layout is documented there too).
namespace {

constexpr uintptr_t kDatabaseGlobal = 0x032F5A58;
constexpr uintptr_t kNameHashFunction = 0x018B1750;
constexpr uint32_t kLightNodeVtable = 0x02BE66C8;
constexpr uint32_t kMaxTracked = 1024;

struct Settings {
    bool enable = true;
    bool includePoint = true;
    bool includeSpot = true;
    bool includeSun = true;
    bool includeFill = true;   // [Lights] Fill: room fill from the game's SH probes and the sky dome
    // RadianceScale / SunRadianceScale: plain multipliers on top of the brightness worked out from the game's own
    // numbers (light_calibration.h). 1 = as the game.
    lightcal::Tuning tuning;
    uint32_t maxLights = 256;
    uint32_t logEveryFrames = 600;
    // [Proxy] HidePostFx: read here so it reloads live with the rest of the ini.
    bool hidePostFx = false;
    // AutoRaytracing=1: switch rtx.enableRaytracing on once a level renders game lights, off when none (menus, loading).
    bool autoRaytracing = true;
    // FlareStandIns=1: a small glowing sphere at every game lens flare (lamp glow cards, gear and flashlight flares),
    // so bulbs and rifle-mounted flashlight lenses are visible in ray tracing.
    bool flareStandIns = true;
    // StandInSkipUnplaced=1 (default, lightcal::StandInOptions): no sphere on flares with node flag +0x2C bit 0 (16
    // of 17 such flares sit at the world origin). StandInSkipMoving=1: no sphere on flares that move (Sam's gear,
    // guards' and drones' lights). StandInSkipSamGear=1: no sphere on Sam's own gear flares only
    // (lightcal::IsSamGearFlare); guards' and drones' lights keep theirs.
    bool standInSkipUnplaced = lightcal::StandInOptions{}.skipUnplaced;
    bool standInSkipMoving = false;
    bool standInSkipSamGear = false;
    float standInRadius = 2.5f;       // max bulb radius in game units (cm), scaled per flare element size
    float standInIntensity = 20.0f;   // Remix emissive intensity
    // [Proxy] EnableDebuggingTelemetry=1: write lightpub_trace.csv next to the game exe (one line per light change).
    bool trace = false;
};

Settings g_settings;
remixapi_Interface g_remix = {};
bool g_ready = false;
lightpub::LogFn g_log = nullptr;
uint32_t g_frame = 0;
lightcal::LightLifecycleTracker<kMaxTracked> g_tracker;
uintptr_t g_lightClass = 0;

template <typename T>
bool Read(uintptr_t address, T* out) {
    __try {
        *out = *reinterpret_cast<const T*>(address);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

float IniFloat(const wchar_t* ini, const wchar_t* key, float fallback) {
    wchar_t text[64];
    wchar_t def[64];
    swprintf_s(def, L"%g", fallback);
    GetPrivateProfileStringW(L"Lights", key, def, text, 64, ini);
    return static_cast<float>(_wtof(text));
}

void LoadSettings(const wchar_t* ini) {
    Settings& s = g_settings;
    s.enable = GetPrivateProfileIntW(L"Lights", L"Enable", 1, ini) != 0;
    s.includePoint = GetPrivateProfileIntW(L"Lights", L"Point", 1, ini) != 0;
    s.includeSpot = GetPrivateProfileIntW(L"Lights", L"Spot", 1, ini) != 0;
    s.includeSun = GetPrivateProfileIntW(L"Lights", L"Sun", 1, ini) != 0;
    s.includeFill = GetPrivateProfileIntW(L"Lights", L"Fill", 1, ini) != 0;
    s.tuning.radianceScale = IniFloat(ini, L"RadianceScale", 1.0f);
    s.tuning.sunRadianceScale = IniFloat(ini, L"SunRadianceScale", 1.0f);
    s.tuning.spotSoftness = IniFloat(ini, L"SpotSoftness", 0.1f);
    s.tuning.sunAngularDiameter = IniFloat(ini, L"SunAngularDiameter", 0.53f);
    s.maxLights = GetPrivateProfileIntW(L"Lights", L"MaxLights", 256, ini);
    if (s.maxLights > kMaxTracked) s.maxLights = kMaxTracked;
    s.logEveryFrames = GetPrivateProfileIntW(L"Lights", L"LogEveryFrames", 600, ini);
    s.hidePostFx = GetPrivateProfileIntW(L"Proxy", L"HidePostFx", 0, ini) != 0;
    s.autoRaytracing = GetPrivateProfileIntW(L"Lights", L"AutoRaytracing", 1, ini) != 0;
    s.flareStandIns = GetPrivateProfileIntW(L"Lights", L"FlareStandIns", 1, ini) != 0;
    s.standInSkipUnplaced =
        GetPrivateProfileIntW(L"Lights", L"StandInSkipUnplaced", lightcal::StandInOptions{}.skipUnplaced, ini) != 0;
    s.standInSkipMoving = GetPrivateProfileIntW(L"Lights", L"StandInSkipMoving", 0, ini) != 0;
    s.standInSkipSamGear = GetPrivateProfileIntW(L"Lights", L"StandInSkipSamGear", 0, ini) != 0;
    s.standInRadius = IniFloat(ini, L"StandInRadius", 2.5f);
    s.standInIntensity = IniFloat(ini, L"StandInIntensity", 20.0f);
    s.trace = GetPrivateProfileIntW(L"Proxy", L"EnableDebuggingTelemetry", 0, ini) != 0;
}

// Mirrors FUN_018AF320: registry hash lookup by name hash.
uintptr_t FindClass(uintptr_t registry, uint32_t nameHash) {
    uintptr_t buckets = 0;
    uint32_t count = 0;
    if (!Read(registry + 0x08, &buckets) || !Read(registry + 0x0C, &count) || !buckets || !count) return 0;
    const uintptr_t bucket = buckets + (nameHash % count) * 8;
    uintptr_t record = 0, last = 0;
    if (!Read(bucket, &record) || !Read(bucket + 4, &last) || !record || !last) return 0;
    uintptr_t end = 0;
    if (!Read(last + 0x50, &end)) return 0;
    for (int guard = 0; record && record != end && guard < 4096; ++guard) {
        uint32_t hash = 0;
        if (!Read(record + 4, &hash)) return 0;
        if (hash == nameHash) return record;
        if (!Read(record + 0x50, &record)) return 0;
    }
    return 0;
}

bool ResolveLightClass(uintptr_t database) {
    if (g_lightClass) return true;
    uintptr_t registry = 0;
    if (!Read(database + 0x1C, &registry) || !registry) return false;
    using HashFn = uint32_t(__cdecl*)(const char*);
    static uint32_t nameHash = 0;  // cached: the game's name hash never changes
    if (!nameHash) {
        __try {
            nameHash = reinterpret_cast<HashFn>(kNameHashFunction)("LightNode");
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            return false;
        }
    }
    g_lightClass = FindClass(registry, nameHash);
    if (g_log) g_log("lightpub: LightNode name hash 0x%08X class record %08X", nameHash, (unsigned)g_lightClass);
    return g_lightClass != 0;
}

uint64_t Fnv(const void* data, size_t size, uint64_t h = 1469598103934665603ull) {
    const uint8_t* p = static_cast<const uint8_t*>(data);
    for (size_t i = 0; i < size; ++i) h = (h ^ p[i]) * 1099511628211ull;
    return h;
}

using lightcal::NodeData;

bool ReadNode(uintptr_t node, NodeData* out) {
    uint32_t vtable = 0, flags = 0;
    if (!Read(node, &vtable) || vtable != kLightNodeVtable || !Read(node + 0x34, &flags)) return false;
    out->flags = flags;
    out->type = flags & 7;
    __try {
        memcpy(out->position, reinterpret_cast<const void*>(node + 0x350), sizeof(out->position));
        memcpy(out->orientation, reinterpret_cast<const void*>(node + 0x360), sizeof(out->orientation));
        memcpy(out->nearSize, reinterpret_cast<const void*>(node + 0x3A0), sizeof(out->nearSize));
        out->nearDistance = *reinterpret_cast<const float*>(node + 0x3A8);
        out->range = *reinterpret_cast<const float*>(node + 0x3B4);
        out->falloffStart = *reinterpret_cast<const float*>(node + 0x3BC);
        memcpy(out->color, reinterpret_cast<const void*>(node + 0x3C4), sizeof(out->color));
        out->intensity = *reinterpret_cast<const float*>(node + 0x44C);
        if (out->type == 4) {
            memcpy(out->sh, reinterpret_cast<const void*>(node + 0x230), sizeof(out->sh));
            out->probeRadius = *reinterpret_cast<const float*>(node + 0x440);
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
    return true;
}

bool Wanted(const NodeData& n) {
    if (!lightcal::Qualifies(n)) return false;
    switch (n.type) {
    case 1: return g_settings.includePoint;
    case 2: return g_settings.includeSpot;
    case 3: return g_settings.includeSun;
    case 4: return g_settings.includeFill;   // room fill and the sky dome
    default: return false;
    }
}

remixapi_Float3D ToRemix(const float v[3]) { return {v[0], v[1], v[2]}; }

remixapi_LightHandle CreateLight(uint64_t hash, const lightcal::PublishedLight& p) {
    remixapi_LightInfo info = {};
    info.sType = REMIXAPI_STRUCT_TYPE_LIGHT_INFO;
    info.hash = hash;
    info.radiance = ToRemix(p.radiance);
    remixapi_LightInfoSphereEXT sphere = {};
    remixapi_LightInfoDistantEXT distant = {};
    remixapi_LightInfoDomeEXT dome = {};
    switch (p.kind) {
    case lightcal::Kind::Distant:
        distant.sType = REMIXAPI_STRUCT_TYPE_LIGHT_INFO_DISTANT_EXT;
        distant.direction = ToRemix(p.direction);
        distant.angularDiameterDegrees = p.angularDiameterDegrees;
        distant.volumetricRadianceScale = p.volumetricRadianceScale;
        info.pNext = &distant;
        break;
    case lightcal::Kind::Dome:
        // No texture: Remix then uses the radiance in every direction (light_helper.slangh sampleDomeLightTexture).
        dome.sType = REMIXAPI_STRUCT_TYPE_LIGHT_INFO_DOME_EXT;
        dome.transform = {{{1, 0, 0, 0}, {0, 1, 0, 0}, {0, 0, 1, 0}}};
        info.pNext = &dome;
        break;
    case lightcal::Kind::Sphere:
        sphere.sType = REMIXAPI_STRUCT_TYPE_LIGHT_INFO_SPHERE_EXT;
        sphere.position = ToRemix(p.position);
        sphere.radius = p.radius;
        sphere.volumetricRadianceScale = p.volumetricRadianceScale;
        if (p.shaped) {
            sphere.shaping_hasvalue = 1;
            sphere.shaping_value.direction = ToRemix(p.direction);
            sphere.shaping_value.coneAngleDegrees = p.coneAngleDegrees;
            sphere.shaping_value.coneSoftness = p.coneSoftness;
            sphere.shaping_value.focusExponent = 0.0f;
        }
        info.pNext = &sphere;
        break;
    default:
        return nullptr;
    }
    remixapi_LightHandle handle = nullptr;
    const remixapi_ErrorCode result = g_remix.CreateLight(&info, &handle);
    if (result != REMIXAPI_ERROR_CODE_SUCCESS) {
        if (g_log) g_log("lightpub: CreateLight failed %d for light %08X%08X kind %d", (int)result,
                         (unsigned)(hash >> 32), (unsigned)hash, (int)p.kind);
        return nullptr;
    }
    return handle;
}

const lightcal::TrackedEntry* FindTracked(uintptr_t node) {
    return g_tracker.Find(node);
}

// Copies qualifying lights while the game's database lock is held. No Remix calls here, so a
// slow bridge can never stall game threads waiting on that lock.
struct Snapshot {
    uintptr_t node;
    NodeData data;
};
Snapshot g_snapshot[kMaxTracked];

uint32_t CollectLights(uintptr_t database) {
    uintptr_t heads = 0;
    uint32_t classIndex = 0, headCount = 0;
    if (!Read(database + 0x2C, &heads) || !Read(database + 0x30, &headCount) ||
        !Read(g_lightClass + 0x10, &classIndex) || !heads || classIndex >= headCount) return 0;
    uintptr_t head = 0, node = 0;
    if (!Read(heads + classIndex * 4, &head) || !head || !Read(head, &node)) return 0;
    uint32_t count = 0;
    for (int guard = 0; node && guard < 8192 && count < g_settings.maxLights; ++guard) {
        uintptr_t cls = 0, next = 0;
        if (!Read(node + 0x1C, &cls) || !Read(node + 0x28, &next)) break;
        Snapshot& s = g_snapshot[count];
        s.data = {};
        if (cls == g_lightClass && ReadNode(node, &s.data) && Wanted(s.data)) {
            s.node = node;
            ++count;
        }
        node = next;
    }
    return count;
}

// Hash of the parameters Remix sees, quantised so flicker and sub-unit jitter do not force a
// destroy/create every frame (position 1 unit, colour x intensity 1/64, direction/cone 1/256, probe values 1/1024).
int32_t QuantizeFloat(float x) {
    if (!std::isfinite(x)) return 0;
    if (x > 1.0e8f) return 100000000;
    if (x < -1.0e8f) return -100000000;
    return static_cast<int32_t>(floorf(x + 0.5f));
}

uint64_t ParamsHash(const NodeData& n) {
    int32_t q[16 + 28];
    int k = 0;
    q[k++] = static_cast<int32_t>(n.type);
    for (int i = 0; i < 3; ++i) q[k++] = QuantizeFloat(n.position[i]);
    for (int i = 0; i < 3; ++i) q[k++] = QuantizeFloat(n.color[i] * n.intensity * 64.0f);
    for (int i = 0; i < 3; ++i) q[k++] = QuantizeFloat(n.orientation[i][0] * 256.0f);
    for (int i = 0; i < 2; ++i) q[k++] = QuantizeFloat(n.nearSize[i]);
    q[k++] = QuantizeFloat(n.nearDistance);
    q[k++] = QuantizeFloat(n.range);
    q[k++] = QuantizeFloat(n.falloffStart);
    q[k++] = QuantizeFloat(n.probeRadius);
    for (const auto& reg : n.sh)
        for (float v : reg) q[k++] = QuantizeFloat(v * 1024.0f);
    return Fnv(q, k * sizeof(int32_t));
}

// The game's cSHScale as last reported by the proxy (SetShScale), quantised for the light hashes.
int32_t g_shScaleQuantised = 256;

// The sky dome (light_calibration.h BuildSkyDome): one per level, rebuilt when its value changes.
remixapi_LightHandle g_dome = nullptr;
uint64_t g_domeHash = 0;
constexpr uint64_t kDomeHashSeed = 0xB1AC5D0E00000000ull;

void DestroyDome() {
    if (g_dome) g_remix.DestroyLight(g_dome);
    g_dome = nullptr;
    g_domeHash = 0;
}

bool DrawDome(uint32_t count) {
    static NodeData probes[kMaxTracked];
    size_t probeCount = 0;
    for (uint32_t i = 0; i < count; ++i)
        if (g_snapshot[i].data.type == 4) probes[probeCount++] = g_snapshot[i].data;
    lightcal::PublishedLight dome;
    if (!lightcal::BuildSkyDome(probes, probeCount, g_settings.tuning, &dome)) {
        DestroyDome();
        return false;
    }
    int32_t q[3];
    for (int k = 0; k < 3; ++k) q[k] = static_cast<int32_t>(floorf(dome.radiance[k] * 65536.0f + 0.5f));
    const uint64_t paramsHash = Fnv(q, sizeof(q), kDomeHashSeed) | 1;
    if (!g_dome || paramsHash != g_domeHash) {
        // Update dome in place on a constant handle hash so sky fill never flashes off for a frame.
        remixapi_LightHandle updated = CreateLight(kDomeHashSeed | 1ull, dome);
        if (updated) {
            g_dome = updated;
            g_domeHash = paramsHash;
        }
        if (g_log) g_log("lightpub: sky dome radiance %.5g %.5g %.5g -> %p", dome.radiance[0], dome.radiance[1],
                         dome.radiance[2], g_dome);
    }
    return g_dome && g_remix.DrawLightInstance(g_dome) == REMIXAPI_ERROR_CODE_SUCCESS;
}

// lightpub_trace.csv: frame,node,id,change,old,new. id is the light's handle hash in Remix. old and new are the
// game's peak colour x intensity, or the +0x34 flags in hex for a flags change.
lightcal::LightChangeTrace<kMaxTracked> g_trace;
FILE* g_traceFile = nullptr;

void UpdateTraceFile() {
    static bool openedOnce = false;
    if (g_settings.trace && !g_traceFile) {
        wchar_t path[MAX_PATH];
        const DWORD n = GetModuleFileNameW(nullptr, path, MAX_PATH);
        wchar_t* slash = n ? wcsrchr(path, L'\\') : nullptr;
        if (!slash || wcscpy_s(slash + 1, MAX_PATH - (slash + 1 - path), L"lightpub_trace.csv")) return;
        if (!(g_traceFile = lightcal::OpenTraceFile(path, openedOnce))) return;
        if (!openedOnce) fputs("frame,node,id,change,old,new\n", g_traceFile);
        openedOnce = true;
        g_trace.Clear();   // the trace runs without the file too; restart it so every light gets a started line
        if (g_log) g_log("lightpub: light change trace -> lightpub_trace.csv");
    } else if (!g_settings.trace && g_traceFile) {
        fclose(g_traceFile);
        g_traceFile = nullptr;
    }
}

// Positions of this frame's published point and spot lights, for the "lamps without a flare" count.
float g_lampPos[kMaxTracked][3];
uint32_t g_lampCount = 0;

void WriteTrace(const lightcal::TraceEvent& e) {
    if (!g_traceFile) return;  // the trace also runs without the file, for the dimmed-light counts
    const unsigned node = static_cast<unsigned>(e.node);
    const unsigned long long id = lightcal::StableNodeLightHash(e.node);
    const char* change = lightcal::TraceChangeName(e.change);
    if (e.change == lightcal::TraceChange::Flags)
        fprintf(g_traceFile, "%u,%08X,%016llX,%s,%08X,%08X\n", g_frame, node, id, change, e.oldFlags, e.newFlags);
    else
        fprintf(g_traceFile, "%u,%08X,%016llX,%s,%.6g,%.6g\n", g_frame, node, id, change, e.oldBrightness,
                e.newBrightness);
}

void SubmitLights(uint32_t count) {
    uint32_t drawn = 0, created = 0, updated = 0, fills = 0;
    g_lampCount = 0;
    for (uint32_t i = 0; i < count; ++i) {
        const Snapshot& s = g_snapshot[i];
        lightcal::PublishedLight published;
        // Level-wide probes (> 40 m) give no sphere light of their own; they feed the sky dome below.
        if (!lightcal::ConvertGameLight(s.data, g_settings.tuning, &published)) continue;
        if (s.data.type == 4) ++fills;
        const uint64_t hash = s.data.type == 4
            ? Fnv(&g_shScaleQuantised, sizeof(g_shScaleQuantised), ParamsHash(s.data)) : ParamsHash(s.data);
        void* rawHandle = nullptr;
        g_tracker.Upsert(
            s.node, hash, published, g_frame,
            [](uint64_t stableHash, const lightcal::PublishedLight& pub) -> void* {
                return CreateLight(stableHash, pub);
            },
            &created, &updated, &rawHandle);
        const auto handle = static_cast<remixapi_LightHandle>(rawHandle);
        if (handle && g_remix.DrawLightInstance(handle) == REMIXAPI_ERROR_CODE_SUCCESS) ++drawn;
        if (!handle) continue;
        g_trace.See(s.node, lightcal::MaxComponent(s.data.color) * s.data.intensity, s.data.flags, g_frame, WriteTrace);
        if ((s.data.type == 1 || s.data.type == 2) && g_lampCount < kMaxTracked)
            memcpy(g_lampPos[g_lampCount++], s.data.position, sizeof(g_lampPos[0]));
    }
    g_trace.Sweep(g_frame, WriteTrace);
    if (g_traceFile) fflush(g_traceFile);
    const bool dome = g_settings.includeFill && DrawDome(count);
    // Destroy ONLY lights that left the engine database (level streaming or despawn).
    g_tracker.SweepUnseen(g_frame, [](void* rawHandle) {
        g_remix.DestroyLight(static_cast<remixapi_LightHandle>(rawHandle));
    });
    if (g_log && g_settings.logEveryFrames && g_frame % g_settings.logEveryFrames == 0)
        g_log("lightpub: frame %u qualifying %u drawn %u (room fill %u, sky dome %d) created %u updated %u tracked %u",
              g_frame, count, drawn, fills, dome ? 1 : 0, created, updated, static_cast<unsigned>(g_tracker.Count()));
}

void DestroyAllTracked() {
    g_tracker.ClearAll([](void* rawHandle) {
        g_remix.DestroyLight(static_cast<remixapi_LightHandle>(rawHandle));
    });
    DestroyDome();
}

void RedrawTracked() {
    for (size_t i = 0; i < g_tracker.Count(); ++i) {
        auto& e = g_tracker.Entries()[i];
        if (e.handle) g_remix.DrawLightInstance(static_cast<remixapi_LightHandle>(e.handle));
        e.lastSeen = g_frame;
    }
    if (g_dome) g_remix.DrawLightInstance(g_dome);
}

// Live tuning: d3d9_proxy.ini is re-read when its modification time changes, so [Lights]
// values and [RemixConfig] overrides can be adjusted while the game is running.
wchar_t g_iniPath[MAX_PATH] = {};
FILETIME g_iniWriteTime = {};
constexpr uint32_t kReloadCheckFrames = 60;

FILETIME IniWriteTime() {
    WIN32_FILE_ATTRIBUTE_DATA data = {};
    if (!g_iniPath[0] || !GetFileAttributesExW(g_iniPath, GetFileExInfoStandard, &data)) return {};
    return data.ftLastWriteTime;
}

// Every option goes to Remix through here, so the proxy knows whether Remix is ray tracing whichever path switched it
// (the ini's [RemixConfig], AutoRaytracing or the proxy). Remix's own default is on.
bool g_remixRaytracing = true;
remixapi_ErrorCode SetConfig(const char* key, const char* value) {
    if (!_stricmp(key, "rtx.enableRaytracing"))
        g_remixRaytracing = !(_stricmp(value, "False") == 0 || strcmp(value, "0") == 0);
    return g_remix.SetConfigVariable(key, value);
}

// Options the proxy itself needs while one of its modes is on (SetRemixOverrides). Sent after [RemixConfig] every
// time it is applied, so an ini reload can't undo them.
struct RemixOverride { char key[96]; char value[64]; char restore[64]; };
RemixOverride g_overrides[16];
int g_overrideCount = 0;

void SendOverrides() {
    for (int i = 0; i < g_overrideCount; ++i) {
        const remixapi_ErrorCode result = SetConfig(g_overrides[i].key, g_overrides[i].value);
        if (g_log) g_log("lightpub: SetConfigVariable %s = %s (proxy override) -> %d", g_overrides[i].key,
                         g_overrides[i].value, (int)result);
    }
}

// Each `rtx.option = value` line in [RemixConfig] is passed to Remix's SetConfigVariable, which
// changes the option at runtime. Remix keeps the last value set, so deleting a line does not undo it.
void ApplyRemixConfig() {
    if (!g_remix.SetConfigVariable) return;
    struct SendOverridesAtEnd { ~SendOverridesAtEnd() { SendOverrides(); } } sendOverridesAtEnd;
    char section[8192];
    char ini[MAX_PATH];
    if (!WideCharToMultiByte(CP_ACP, 0, g_iniPath, -1, ini, MAX_PATH, nullptr, nullptr)) return;
    const DWORD length = GetPrivateProfileSectionA("RemixConfig", section, sizeof(section), ini);
    for (const char* entry = section; length && *entry; entry += strlen(entry) + 1) {
        char line[512];
        strncpy_s(line, entry, _TRUNCATE);
        char* eq = strchr(line, '=');
        if (!eq || line[0] == ';' || line[0] == '#') continue;
        *eq = '\0';
        char* key = line;
        char* value = eq + 1;
        while (*key == ' ' || *key == '\t') ++key;
        for (char* end = key + strlen(key); end > key && (end[-1] == ' ' || end[-1] == '\t'); --end) end[-1] = '\0';
        while (*value == ' ' || *value == '\t') ++value;
        const remixapi_ErrorCode result = SetConfig(key, value);
        if (g_log) g_log("lightpub: SetConfigVariable %s = %s -> %d", key, value, (int)result);
    }
}

proxy::AutoRaytracing g_autoRt;   // [Lights] AutoRaytracing (auto_raytracing.h)
bool g_showGamePicture = false;

void UpdateAutoRaytracing(bool inLevel) {
    if (!g_settings.autoRaytracing || !g_remix.SetConfigVariable) return;
    const proxy::AutoRtAction action = g_autoRt.Update(inLevel, g_showGamePicture);
    if (!action.send) return;
    // Keep-alive first: Remix applies both at the end of the same frame, so the scene is never cleared for a pause.
    char frames[16];
    sprintf_s(frames, "%u", action.keepScene ? proxy::kKeepSceneFrames : 0u);
    const remixapi_ErrorCode k = SetConfig("rtx.sceneKeepAliveFrames", frames);
    const remixapi_ErrorCode r = SetConfig("rtx.enableRaytracing", action.raytracing ? "True" : "False");
    if (g_log) g_log("lightpub: AutoRaytracing -> %s, scene %s (%d %d)", action.raytracing ? "on" : "off",
                     action.raytracing ? "live" : action.keepScene ? "kept for the pause" : "cleared", (int)r, (int)k);
}

// "skip sam gear" sits before the other two skips so the line still ends in "skip moving N)", which the harness
// matches after a reload.
void LogSettings(const char* what) {
    if (g_log) g_log("lightpub: %s (scale %g, sun %g, fill %d, max %u, flare stand-ins %d, trace %d, "
                     "skip sam gear %d, stand-in skip unplaced %d, skip moving %d)", what,
                     g_settings.tuning.radianceScale, g_settings.tuning.sunRadianceScale, g_settings.includeFill ? 1 : 0,
                     g_settings.maxLights, g_settings.flareStandIns ? 1 : 0, g_settings.trace ? 1 : 0,
                     g_settings.standInSkipSamGear ? 1 : 0, g_settings.standInSkipUnplaced ? 1 : 0,
                     g_settings.standInSkipMoving ? 1 : 0);
}

uint32_t g_seenIniEpoch = 0;
void ReloadIfChanged() {
    const uint32_t epoch = proxy::PeekIniChangeEpoch();
    if (epoch == g_seenIniEpoch) return;
    g_seenIniEpoch = epoch;
    const FILETIME now = IniWriteTime();
    if (CompareFileTime(&now, &g_iniWriteTime) == 0) return;
    g_iniWriteTime = now;
    LoadSettings(g_iniPath);
    ApplyRemixConfig();
    g_autoRt.Reassert();   // the ini may have set rtx.enableRaytracing; AutoRaytracing re-asserts its state
    LogSettings("settings reloaded");
}

// The lock pointer comes from game memory; check once per database that it reads as a
// plausible CRITICAL_SECTION before ever handing it to TryEnterCriticalSection.
bool PlausibleLock(CRITICAL_SECTION* lock) {
    CRITICAL_SECTION copy;
    if (!Read(reinterpret_cast<uintptr_t>(lock), &copy)) return false;
    return copy.LockCount <= 0 && copy.RecursionCount >= 0 && copy.RecursionCount < 1000;
}

}  // namespace

namespace lightpub {

bool SetRemixConfig(const char* key, const char* value) {
    if (!g_ready || !g_remix.SetConfigVariable) return false;
    return SetConfig(key, value) == REMIXAPI_ERROR_CODE_SUCCESS;
}

void SetRemixOverrides(const char* const (*list)[3], int count) {
    // The old list's options go back to their restore values (then the ini wins where it sets them).
    if (g_ready && g_remix.SetConfigVariable)
        for (int i = 0; i < g_overrideCount; ++i) SetConfig(g_overrides[i].key, g_overrides[i].restore);
    g_overrideCount = 0;
    for (int i = 0; i < count && i < static_cast<int>(sizeof(g_overrides) / sizeof(g_overrides[0])); ++i) {
        strncpy_s(g_overrides[i].key, list[i][0], _TRUNCATE);
        strncpy_s(g_overrides[i].value, list[i][1], _TRUNCATE);
        strncpy_s(g_overrides[i].restore, list[i][2], _TRUNCATE);
        g_overrideCount = i + 1;
    }
    if (g_ready) ApplyRemixConfig();   // ini first, then the overrides
}

bool Initialize(HMODULE remixClient, const wchar_t* iniPath, LogFn log) {
    g_log = log;
    LoadSettings(iniPath);
    if (!g_settings.enable) return false;
    if (!remixClient) {
        if (g_log) g_log("lightpub: no Remix client module; disabled");
        return false;
    }
    auto init = reinterpret_cast<PFN_remixapi_InitializeLibrary>(GetProcAddress(remixClient, "remixapi_InitializeLibrary"));
    if (!init) {
        if (g_log) g_log("lightpub: remixapi_InitializeLibrary not exported; disabled");
        return false;
    }
    remixapi_InitializeLibraryInfo info = {};
    info.sType = REMIXAPI_STRUCT_TYPE_INITIALIZE_LIBRARY_INFO;
    info.version = REMIXAPI_VERSION_MAKE(REMIXAPI_VERSION_MAJOR, REMIXAPI_VERSION_MINOR, REMIXAPI_VERSION_PATCH);
    const remixapi_ErrorCode result = init(&info, &g_remix);
    if (result != REMIXAPI_ERROR_CODE_SUCCESS || !g_remix.CreateLight || !g_remix.DrawLightInstance || !g_remix.DestroyLight) {
        if (g_log) g_log("lightpub: Remix API init failed (%d); is exposeRemixApi = True in bridge.conf?", (int)result);
        return false;
    }
    g_ready = true;
    wcsncpy_s(g_iniPath, iniPath, _TRUNCATE);
    g_iniWriteTime = IniWriteTime();
    ApplyRemixConfig();
    LogSettings("enabled");
    return true;
}

// ---- Flare stand-ins ----------------------------------------------------------------------------------
// Lens flares are l3d::FlareNode objects in the same database as the lights (Ghidra: vtable 0x02BE66DC; the game
// destroys them in FUN_018a3af0). Fields: see the flare section of light_calibration.h; +0x8C/+0x90 element
// array/count (0x68 bytes each: +0x04 intensity, +0x18/+0x1C size, +0x38 RGBA), +0xD4 world position.
// g_flares holds every flare of the frame; standIn marks the ones that get a glowing sphere.
struct FlareSnap {
    uintptr_t node;
    lightcal::FlareData data;
    float color[3];
    float radiusCm;
    bool standIn;
};
constexpr uint32_t kMaxFlares = 512;
FlareSnap g_flares[kMaxFlares];
lightcal::FlareData g_flareData[kMaxFlares];   // copy of g_flares[i].data for CountLampsWithoutFlare
uint32_t g_flareCount = 0;
uintptr_t g_flareClass = 0;
lightcal::FlareMotion<kMaxFlares> g_flareMotion;
remixapi_MeshHandle g_standInMesh[64] = {};   // one sphere per quantised colour (4 levels per channel)

uint32_t CollectFlares(uintptr_t database) {
    if (!g_flareClass) {
        static uint32_t nameHash = 0;
        uintptr_t registry = 0;
        if (!nameHash) {
            __try {
                nameHash = reinterpret_cast<uint32_t(__cdecl*)(const char*)>(kNameHashFunction)("FlareNode");
            } __except (EXCEPTION_EXECUTE_HANDLER) {
                return 0;
            }
        }
        if (!Read(database + 0x1C, &registry) || !registry || !(g_flareClass = FindClass(registry, nameHash))) return 0;
    }
    uintptr_t heads = 0, head = 0, node = 0;
    uint32_t classIndex = 0, headCount = 0;
    if (!Read(database + 0x2C, &heads) || !Read(database + 0x30, &headCount) || !Read(g_flareClass + 0x10, &classIndex) ||
        !heads || classIndex >= headCount || !Read(heads + classIndex * 4, &head) || !head || !Read(head, &node)) return 0;
    lightcal::StandInOptions options;
    options.enabled = g_settings.flareStandIns;
    options.skipUnplaced = g_settings.standInSkipUnplaced;
    options.skipMoving = g_settings.standInSkipMoving;
    options.skipSamGear = g_settings.standInSkipSamGear;
    uint32_t count = 0;
    for (int guard = 0; node && guard < 8192 && count < kMaxFlares; ++guard) {
        uintptr_t cls = 0, next = 0, elements = 0;
        uint32_t elementCount = 0;
        if (!Read(node + 0x1C, &cls) || !Read(node + 0x28, &next)) break;
        FlareSnap& f = g_flares[count];
        lightcal::FlareData& d = f.data;
        d = {};
        if (cls == g_flareClass && Read(node + 0x2C, &d.nodeFlags) && Read(node + 0x34, &d.word34) &&
            Read(node + 0xD4, &d.pos[0]) && Read(node + 0xD8, &d.pos[1]) && Read(node + 0xDC, &d.pos[2])) {
            // Colour and size of the brightest element.
            f.color[0] = f.color[1] = f.color[2] = 0.0f;
            if (Read(node + 0x8C, &elements) && Read(node + 0x90, &elementCount) && elements) {
                d.elementCount = elementCount;
                for (uint32_t i = 0; i < elementCount && i < 8; ++i) {
                    const uintptr_t e = elements + i * 0x68;
                    float intensity = 0, sx = 0, sy = 0, rgb[3] = {};
                    Read(e + 0x04, &intensity);
                    Read(e + 0x18, &sx);
                    Read(e + 0x1C, &sy);
                    if (i == 0) {
                        d.size0[0] = sx;
                        d.size0[1] = sy;
                    }
                    for (int k = 0; k < 3; ++k) Read(e + 0x38 + k * 4, &rgb[k]);
                    if (intensity > d.peakIntensity && isfinite(intensity)) {
                        d.peakIntensity = intensity;
                        if (isfinite(sx) && isfinite(sy) && sx > 0.0f && sy > 0.0f) d.peakSize = 0.5f * (sx + sy);
                        for (int k = 0; k < 3; ++k) f.color[k] = rgb[k];
                    }
                }
            }
            d.moved = g_flareMotion.Update(node, d.pos, g_frame);
            f.node = node;
            // Scale bulb radius cleanly by flare sprite size: tight ~1.2-1.8 cm lens on gun-rail/gear flares
            // (s=8..16), up to standInRadius (~2.5-3.5 cm) on ceiling fixtures; WantsStandIn skips giant sky/sun
            // coronas (s >= 400).
            f.radiusCm = std::clamp(d.peakSize * 0.08f, 1.2f, g_settings.standInRadius);
            f.standIn = lightcal::WantsStandIn(d, options);
            g_flareData[count] = d;
            ++count;
        }
        node = next;
    }
    g_flareMotion.Sweep(g_frame);
    return count;
}

remixapi_MeshHandle StandInMesh(const float color[3]) {
    float peak = fmaxf(fmaxf(color[0], color[1]), color[2]);
    if (!(peak > 0.0f) || !isfinite(peak)) peak = 1.0f;
    int q[3];
    for (int k = 0; k < 3; ++k) q[k] = static_cast<int>(fminf(fmaxf(color[k] / peak, 0.0f), 1.0f) * 3.0f + 0.5f);
    const int key = q[0] * 16 + q[1] * 4 + q[2];
    if (g_standInMesh[key]) return g_standInMesh[key];
    remixapi_MaterialInfoOpaqueEXT opaque = {};
    opaque.sType = REMIXAPI_STRUCT_TYPE_MATERIAL_INFO_OPAQUE_EXT;
    opaque.albedoConstant = {0.8f, 0.8f, 0.8f};
    opaque.opacityConstant = 1.0f;
    opaque.roughnessConstant = 0.3f;
    opaque.thinFilmThickness_value = 200.0f;
    opaque.alphaTestType = 7;  // always pass
    remixapi_MaterialInfo material = {};
    material.sType = REMIXAPI_STRUCT_TYPE_MATERIAL_INFO;
    material.pNext = &opaque;
    material.hash = 0xB1AC5EED00000000ull | static_cast<uint64_t>(key);
    material.emissiveIntensity = g_settings.standInIntensity;
    material.emissiveColorConstant = {q[0] / 3.0f, q[1] / 3.0f, q[2] / 3.0f};
    material.spriteSheetRow = material.spriteSheetCol = 1;
    material.filterMode = material.wrapModeU = material.wrapModeV = 1;
    remixapi_MaterialHandle materialHandle = nullptr;
    if (g_remix.CreateMaterial(&material, &materialHandle) != REMIXAPI_ERROR_CODE_SUCCESS) return nullptr;
    // Unit sphere: 8 rings x 12 segments.
    constexpr int kRings = 8, kSegments = 12;
    static remixapi_HardcodedVertex vertices[(kRings + 1) * (kSegments + 1)];
    static uint32_t indices[kRings * kSegments * 6];
    int v = 0, i = 0;
    for (int r = 0; r <= kRings; ++r) {
        for (int sgm = 0; sgm <= kSegments; ++sgm) {
            const float theta = 3.14159265f * r / kRings, phi = 6.2831853f * sgm / kSegments;
            const float n[3] = {sinf(theta) * cosf(phi), sinf(theta) * sinf(phi), cosf(theta)};
            remixapi_HardcodedVertex& out = vertices[v++];
            out = {};
            for (int k = 0; k < 3; ++k) out.position[k] = out.normal[k] = n[k];
            out.texcoord[0] = static_cast<float>(sgm) / kSegments;
            out.texcoord[1] = static_cast<float>(r) / kRings;
            out.color = 0xFFFFFFFF;
        }
    }
    for (int r = 0; r < kRings; ++r) {
        for (int sgm = 0; sgm < kSegments; ++sgm) {
            const uint32_t a = r * (kSegments + 1) + sgm, b = a + kSegments + 1;
            const uint32_t quad[6] = {a, b, a + 1, a + 1, b, b + 1};
            for (uint32_t index : quad) indices[i++] = index;
        }
    }
    remixapi_MeshInfoSurfaceTriangles surface = {};
    surface.vertices_values = vertices;
    surface.vertices_count = v;
    surface.indices_values = indices;
    surface.indices_count = i;
    surface.material = materialHandle;
    remixapi_MeshInfo mesh = {};
    mesh.sType = REMIXAPI_STRUCT_TYPE_MESH_INFO;
    mesh.hash = 0xB1AC5EED10000000ull | static_cast<uint64_t>(key);
    mesh.surfaces_values = &surface;
    mesh.surfaces_count = 1;
    if (g_remix.CreateMesh(&mesh, &g_standInMesh[key]) != REMIXAPI_ERROR_CODE_SUCCESS) g_standInMesh[key] = nullptr;
    if (g_log) g_log("lightpub: stand-in sphere for colour key %d -> %p", key, g_standInMesh[key]);
    return g_standInMesh[key];
}

// Stand-ins drawn this frame, and how many of them sit on unplaced (+0x2C bit 0), moving, or Sam's gear flares.
uint32_t g_standInsDrawn = 0, g_standInsUnplaced = 0, g_standInsMoving = 0, g_standInsSamGear = 0;

void DrawFlareStandIns() {
    const bool api = g_remix.CreateMesh && g_remix.CreateMaterial && g_remix.DrawInstance;
    uint32_t drawn = 0, unplaced = 0, moving = 0, samGear = 0;
    for (uint32_t n = 0; api && n < g_flareCount; ++n) {
        const FlareSnap& f = g_flares[n];
        if (!f.standIn) continue;
        const float r = f.radiusCm > 0.0f ? f.radiusCm : g_settings.standInRadius;
        remixapi_InstanceInfo instance = {};
        instance.sType = REMIXAPI_STRUCT_TYPE_INSTANCE_INFO;
        instance.mesh = StandInMesh(f.color);
        if (!instance.mesh) continue;
        const float* p = f.data.pos;
        instance.transform = {{{r, 0, 0, p[0]}, {0, r, 0, p[1]}, {0, 0, r, p[2]}}};
        instance.doubleSided = true;
        if (g_remix.DrawInstance(&instance) != REMIXAPI_ERROR_CODE_SUCCESS) continue;
        ++drawn;
        unplaced += (f.data.nodeFlags & lightcal::kFlareNodeUnplaced) != 0;
        moving += f.data.moved;
        samGear += lightcal::IsSamGearFlare(f.data);
    }
    g_standInsDrawn = drawn;
    g_standInsUnplaced = unplaced;
    g_standInsMoving = moving;
    g_standInsSamGear = samGear;
    if (g_log && g_settings.logEveryFrames && !(g_frame % g_settings.logEveryFrames))
        g_log("lightpub: flare stand-ins %u drawn of %u flares (enabled %d, max radius %g, intensity %g)", drawn,
              g_flareCount, g_settings.flareStandIns ? 1 : 0, g_settings.standInRadius, g_settings.standInIntensity);
}

// "lightpub: stray" line, every LogEveryFrames frames, for gate checks. Each count is this frame's value; "max" is
// the highest value since the previous stray line, so a spike between lines is not lost.
//   lamps without flare: published point/spot lights with no flare within 1 m (lightcal::CountLampsWithoutFlare)
//   stand-ins on unplaced / moving / sam gear: stand-in spheres drawn on flares with +0x2C bit 0 / flares that
//   moved / Sam's own gear flares (lightcal::IsSamGearFlare, a subset of moving)
//   dimmed: published lights below 10% of their own peak brightness; longest dim: frames the longest of them has
//   been dimmed (how long a light the game nearly switched off keeps lighting the Remix scene)
// New fields go at the end of the line; the gate's pattern reads it from the start.
void UpdateStrayCounts() {
    struct Max { uint32_t lonely, unplaced, moving, dimmed, longestDim, samGear; };
    static Max window = {};
    const uint32_t lonely = lightcal::CountLampsWithoutFlare(g_lampPos, g_lampCount, g_flareData, g_flareCount);
    uint32_t dimmed = 0, longestDim = 0;
    g_trace.DimStats(g_frame, &dimmed, &longestDim);
    window.lonely = std::max(window.lonely, lonely);
    window.unplaced = std::max(window.unplaced, g_standInsUnplaced);
    window.moving = std::max(window.moving, g_standInsMoving);
    window.dimmed = std::max(window.dimmed, dimmed);
    window.longestDim = std::max(window.longestDim, longestDim);
    window.samGear = std::max(window.samGear, g_standInsSamGear);
    if (!g_log || !g_settings.logEveryFrames || g_frame % g_settings.logEveryFrames) return;
    g_log("lightpub: stray frame %u lamps %u without flare %u (max %u) flares %u stand-ins %u on unplaced %u (max %u) "
          "on moving %u (max %u) dimmed %u (max %u) longest dim %u frames (max %u) on sam gear %u (max %u)",
          g_frame, g_lampCount, lonely, window.lonely, g_flareCount, g_standInsDrawn, g_standInsUnplaced,
          window.unplaced, g_standInsMoving, window.moving, dimmed, window.dimmed, longestDim, window.longestDim,
          g_standInsSamGear, window.samGear);
    window = {};
}

void OnPresent() {
    if (!g_ready) return;
    ++g_frame;
    ReloadIfChanged();
    UpdateTraceFile();
    if (!g_settings.enable) return;  // switched off live; ReloadIfChanged already removed the lights
    static uintptr_t lastDatabase = 0;
    static CRITICAL_SECTION* checkedLock = nullptr;
    uintptr_t database = 0;
    if (!Read(kDatabaseGlobal, &database) || !database) { UpdateAutoRaytracing(false); return; }
    if (database != lastDatabase) {
        // New database (level load): class record and lights from the old one are invalid.
        DestroyAllTracked();
        g_lightClass = 0;
        g_flareClass = 0;
        g_flareCount = 0;
        g_flareMotion.Clear();
        checkedLock = nullptr;
        lastDatabase = database;
    }
    if (!ResolveLightClass(database)) return;
    uintptr_t lockHolder = 0;
    CRITICAL_SECTION* lock = nullptr;
    if (!Read(database + 0x78, &lockHolder) || !lockHolder || !Read(lockHolder, &lock) || !lock) return;
    if (lock != checkedLock) {
        if (!PlausibleLock(lock)) {
            RedrawTracked();
            return;
        }
        checkedLock = lock;
    }
    // Never wait on the game's lock from inside Present. If it is busy, redraw last frame's
    // lights unchanged so nothing flickers off for a frame.
    if (!TryEnterCriticalSection(lock)) {
        RedrawTracked();
        DrawFlareStandIns();
        return;
    }
    const uint32_t count = CollectLights(database);
    // Every flare is collected; FlareStandIns=0 only stops the spheres (the stray line then shows stand-ins 0).
    g_flareCount = CollectFlares(database);
    LeaveCriticalSection(lock);
    UpdateAutoRaytracing(count > 0);
    SubmitLights(count);
    DrawFlareStandIns();
    UpdateStrayCounts();
}

bool HidePostFx() { return g_ready && g_settings.hidePostFx; }

void SetShowGamePicture(bool show) { g_showGamePicture = show; }

void SetShScale(float scale) {
    if (!(scale >= 0.0f && scale < 1.0e4f)) return;
    const int32_t q = static_cast<int32_t>(floorf(scale * 256.0f + 0.5f));
    if (q == g_shScaleQuantised) return;
    if (g_log) g_log("lightpub: game SH scale (cSHScale) %g, was %g", scale, g_settings.tuning.shScale);
    g_shScaleQuantised = q;
    g_settings.tuning.shScale = scale;
}

bool RaytracingOn() { return g_autoRt.State() != 0 && g_remixRaytracing; }

static void FilePrintf(HANDLE file, const char* format, ...) {
    char text[2048];
    va_list args;
    va_start(args, format);
    const int n = vsnprintf_s(text, sizeof(text), _TRUNCATE, format, args);
    va_end(args);
    DWORD written = 0;
    WriteFile(file, text, static_cast<DWORD>(n < 0 ? strlen(text) : static_cast<size_t>(n)), &written, nullptr);
}

// Lens flares (glow cards on lamps, Sam's gear lights, flashlight flares) are l3d::FlareNode objects in the same
// database as the lights (Ghidra: vtable 0x02BE66DC). Printed fields: nodeflags +0x2C (bit 0 = unplaced, see
// light_calibration.h), index +0x30 (-1 until placed), w34 +0x34 (low bits flags, 0x2 camera space; the upper bits
// hold other data), f84 +0x84 as a float (meaning unknown; 1 on most world lamps), moved (FlareMotion),
// standin (a sphere is drawn this frame), elements +0x8C/+0x90 (0x68 bytes each: +0x04 intensity, +0x18/+0x1C
// size, +0x38 RGBA), pos +0xD4.
void DumpFlares(HANDLE file, uintptr_t database) {
    static uint32_t nameHash = 0;
    uintptr_t registry = 0, flareClass = 0, heads = 0, head = 0, node = 0;
    uint32_t classIndex = 0, headCount = 0;
    if (!nameHash) {
        __try {
            nameHash = reinterpret_cast<uint32_t(__cdecl*)(const char*)>(kNameHashFunction)("FlareNode");
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            return;
        }
    }
    if (!Read(database + 0x1C, &registry) || !registry || !(flareClass = FindClass(registry, nameHash)) ||
        !Read(database + 0x2C, &heads) || !Read(database + 0x30, &headCount) || !Read(flareClass + 0x10, &classIndex) ||
        !heads || classIndex >= headCount || !Read(heads + classIndex * 4, &head) || !head || !Read(head, &node)) {
        FilePrintf(file, "error\tno FlareNode class or list\n");
        return;
    }
    for (int guard = 0; node && guard < 8192; ++guard) {
        uintptr_t cls = 0, next = 0, vtable = 0, elements = 0;
        if (!Read(node + 0x1C, &cls) || !Read(node + 0x28, &next)) break;
        uint32_t nodeFlags = 0, word34 = 0, count = 0;
        int32_t index = 0;
        float f84 = 0.0f, pos[3] = {};
        if (cls == flareClass && Read(node, &vtable) && Read(node + 0x2C, &nodeFlags) && Read(node + 0x30, &index) &&
            Read(node + 0x34, &word34) && Read(node + 0x84, &f84) && Read(node + 0x8C, &elements) &&
            Read(node + 0x90, &count) && Read(node + 0xD4, &pos[0]) && Read(node + 0xD8, &pos[1]) &&
            Read(node + 0xDC, &pos[2])) {
            const FlareSnap* snap = nullptr;
            for (uint32_t i = 0; i < g_flareCount && !snap; ++i)
                if (g_flares[i].node == node) snap = &g_flares[i];
            FilePrintf(file, "flare\tnode=%08X\tvtable=%08X\tnodeflags=%08X\tindex=%d\tw34=%08X\tf84=%g\t"
                       "pos=%.6g,%.6g,%.6g\tmoved=%d\tsamgear=%d\tstandin=%d\telements=%u",
                       static_cast<unsigned>(node), static_cast<unsigned>(vtable), nodeFlags, index, word34, f84,
                       pos[0], pos[1], pos[2], snap && snap->data.moved ? 1 : 0,
                       snap && lightcal::IsSamGearFlare(snap->data) ? 1 : 0, snap && snap->standIn ? 1 : 0, count);
            for (uint32_t i = 0; elements && i < count && i < 8; ++i) {
                float intensity = 0, size[2] = {}, rgba[4] = {};
                const uintptr_t e = elements + i * 0x68;
                Read(e + 0x04, &intensity);
                for (int k = 0; k < 2; ++k) Read(e + 0x18 + k * 4, &size[k]);
                for (int k = 0; k < 4; ++k) Read(e + 0x38 + k * 4, &rgba[k]);
                FilePrintf(file, "\te%u=i%.4g,s%.4g,%.4g,c%.4g,%.4g,%.4g,%.4g", i, intensity, size[0], size[1],
                           rgba[0], rgba[1], rgba[2], rgba[3]);
            }
            FilePrintf(file, "\traw=");
            for (uint32_t offset = 0x20; offset < 0xFC; offset += 4) {
                uint32_t word = 0;
                Read(node + offset, &word);
                FilePrintf(file, offset == 0x20 ? "%08X" : " %08X", word);
            }
            FilePrintf(file, "\n");
        }
        node = next;
    }
}

void DumpLights(const wchar_t* path) {
    HANDLE file = CreateFileW(path, GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return;
    FilePrintf(file, "settings\tready=%d\trton=%d\tradianceScale=%g\tsunRadianceScale=%g\tfill=%d\ttracked=%u\tflareStandIns=%d\t"
               "standInSkipUnplaced=%d\tstandInSkipMoving=%d\tstandInSkipSamGear=%d\tflares=%u\tstandins=%u\n",
               g_ready ? 1 : 0, g_autoRt.State(), g_settings.tuning.radianceScale, g_settings.tuning.sunRadianceScale,
               g_settings.includeFill ? 1 : 0, static_cast<unsigned>(g_tracker.Count()),
               g_settings.flareStandIns ? 1 : 0, g_settings.standInSkipUnplaced ? 1 : 0,
               g_settings.standInSkipMoving ? 1 : 0, g_settings.standInSkipSamGear ? 1 : 0, g_flareCount,
               g_standInsDrawn);
    uintptr_t database = 0, heads = 0, head = 0, node = 0, lockHolder = 0;
    uint32_t classIndex = 0, headCount = 0;
    if (!Read(kDatabaseGlobal, &database) || !database || !g_lightClass || !Read(database + 0x2C, &heads) ||
        !Read(database + 0x30, &headCount) || !Read(g_lightClass + 0x10, &classIndex) || !heads ||
        classIndex >= headCount || !Read(heads + classIndex * 4, &head) || !head || !Read(head, &node)) {
        FilePrintf(file, "error\tno light database or light class\n");
        CloseHandle(file);
        return;
    }
    CRITICAL_SECTION* lock = nullptr;
    bool locked = false;
    if (Read(database + 0x78, &lockHolder) && lockHolder && Read(lockHolder, &lock) && lock && PlausibleLock(lock)) {
        for (int attempt = 0; attempt < 200 && !locked; ++attempt) {
            locked = TryEnterCriticalSection(lock) != 0;
            if (!locked) Sleep(1);
        }
    }
    FilePrintf(file, "walk\tlocked=%d\n", locked ? 1 : 0);
    for (int guard = 0; node && guard < 8192; ++guard) {
        uintptr_t cls = 0, next = 0;
        if (!Read(node + 0x1C, &cls) || !Read(node + 0x28, &next)) break;
        NodeData n = {};
        uint32_t flags = 0, nodeFlags = 0;
        if (cls == g_lightClass && ReadNode(node, &n) && Read(node + 0x34, &flags) && Read(node + 0x2C, &nodeFlags)) {
            const lightcal::TrackedEntry* t = FindTracked(node);
            FilePrintf(file, "light\tnode=%08X\tnodeflags=%08X\tflags=%08X\ttype=%u\twanted=%d\ttracked=%d\tpos=%.6g,%.6g,%.6g\trow0=%.6g,%.6g,%.6g\trow1=%.6g,%.6g,%.6g\trow2=%.6g,%.6g,%.6g\tnear=%.6g,%.6g,%.6g\trange=%.6g\tfalloffStart=%.6g\tcolor=%.6g,%.6g,%.6g\tintensity=%.6g",
                       static_cast<unsigned>(node), nodeFlags, flags, n.type, Wanted(n) ? 1 : 0, t && t->handle ? 1 : 0,
                       n.position[0], n.position[1], n.position[2],
                       n.orientation[0][0], n.orientation[0][1], n.orientation[0][2],
                       n.orientation[1][0], n.orientation[1][1], n.orientation[1][2],
                       n.orientation[2][0], n.orientation[2][1], n.orientation[2][2],
                       n.nearSize[0], n.nearSize[1], n.nearDistance, n.range, n.falloffStart,
                       n.color[0], n.color[1], n.color[2], n.intensity);
            // Raw words, to find which field marks a light as switched off or hidden.
            static const struct { const char* name; uint32_t from, to; } kRanges[] = {{"raw20", 0x20, 0x390}, {"raw390", 0x390, 0x460}};
            for (const auto& range : kRanges) {
                FilePrintf(file, "\t%s=", range.name);
                for (uint32_t offset = range.from; offset < range.to; offset += 4) {
                    uint32_t word = 0;
                    Read(node + offset, &word);
                    FilePrintf(file, offset == range.from ? "%08X" : " %08X", word);
                }
            }
            FilePrintf(file, "\n");
        } else {
            FilePrintf(file, "other\tnode=%08X\tclass=%08X\n", static_cast<unsigned>(node), static_cast<unsigned>(cls));
        }
        node = next;
    }
    DumpFlares(file, database);
    if (locked) LeaveCriticalSection(lock);
    CloseHandle(file);
}

void Shutdown() {
    if (!g_ready) return;
    DestroyAllTracked();
    g_ready = false;
}

}  // namespace lightpub
