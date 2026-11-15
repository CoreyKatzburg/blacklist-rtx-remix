#pragma once
// The one place where Blacklist's light data becomes Remix lights. lightpub.cpp reads the game's LightNodes and
// calls these functions; tests/verify_proxy_tests.cpp runs the same functions on recorded light dumps
// (fixtures/run*/*_lights.tsv), and tests/verify_lights_and_materials.py checks that output.
//
// Engine facts used here (Blacklist_game.exe, DX9):
// - Point/spot brightness in the game's light shaders: colour x intensity x (1 - t^2)^2,
//   t = (distance - falloffStart) / (range - falloffStart), clamped to 0..1 (frame dump run2, ps 679A8A28).
// - Type-4 LightNodes are spherical-harmonics irradiance probes (FUN_018c3d80): for every object the engine sums
//   w x SH over the probes with flag 0x400, w = 1 - clamp(|p - centre|^2 / radius^2, 0, 1), radius at +0x440,
//   divides by the summed weight and uploads the 7 float4 at +0x230 as the object's cSHParams[7]
//   (order cAr, cAg, cAb, cBr, cBg, cBb, cC; Sloan's packing: x1 = dot(cA, (n, 1)), x2 = dot(cB, n.xyzz * n.yzzx),
//   x3 = cC.rgb * (n.x^2 - n.y^2)). The character shader (vs 87D8E2DF) clamps that at 0 and multiplies it by
//   cSHScale (c16.x), a renderer setting (FUN_01a03bf0: settings +0x240, times an optional factor); the proxy reads
//   the uploaded value at runtime (Tuning::shScale). The result scales albedo, so it is a radiance-like quantity:
//   a Remix light that gives a surface the irradiance E adds albedo x E / pi, the same scale.

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>

namespace lightcal {

// ---- Fixture lights (game types 1 point, 2 spot) ----------------------------------------------------------------
constexpr float kMinFixtureRadiusCm = 20.0f;     // smallest emitter: a bulb in a small fitting
constexpr float kMaxFixtureRadiusCm = 35.0f;     // largest emitter: a ceiling panel / floodlight head
constexpr float kRadiusRangeFraction = 0.08f;    // emitter radius grows with the light's reach
constexpr float kRefRangeFraction = 0.50f;       // Remix matches the game here (fraction of range) ...
constexpr float kMaxRefDistanceCm = 180.0f;      // ... up to 1.8 m (normal room distance: >= 0.50x game median)
constexpr float kCloseCheckDistanceCm = 50.0f;   // "close-range wall": half a metre from the light
constexpr float kMaxCloseRatioTarget = 5.0f;     // blocks 9x-440x neon blowout while keeping >= 0.50x reach at 1.8 m
static_assert(kMaxCloseRatioTarget >= 1.3f && kMaxCloseRatioTarget <= 5.5f, "close-range cap must block blowout");

// ---- Weapon / attachment spotlights (guard rifle flashlights: range <= 100 cm, nearDistance > range) ------------
// Cross-checked against two witnesses in recorded dumps (f101477_lights.tsv, run14/after_probe_lights.tsv):
//   1) Paired FlareNode sits 9.07 cm behind LightNode.position right on the gun-mounted flashlight rail attachment.
//   2) Deferred spot shader ps 679A8A28 uploads cSpot0 c64.xyz == LightNode.position.
// nearSize (50x50) and nearDistance (150) define the narrow ~19-degree beam cone; range=50 was only a tiny floor dot
// because 2013 Blacklist faked the beam with a volumetric cone mesh (~16 m reach).
constexpr float kWeaponSpotMaxGameRangeCm = 100.0f;
constexpr float kWeaponSpotRadiusCm = 3.0f;      // tight 3 cm flashlight lens (never a 20-40 cm floodlight ball)
constexpr float kWeaponSpotReachCm = 1600.0f;    // 16 m real flashlight reach
constexpr float kWeaponSpotRefDistanceCm = 400.0f;
constexpr float kWeaponSpotVolumetricScale = 2.0f;

// ---- Room fill (game type 4, SH probes) -------------------------------------------------------------------------
constexpr uint32_t kProbeFlag = 0x400;            // the engine only blends probes with this flag
constexpr float kRoomFillMaxZoneRadiusCm = 4000.0f; // indoor/zone probes up to 40 m (incl. Consumption's 25 m rooms); > 40 m feeds sky dome
constexpr float kFillMinOffsetCm = 100.0f;        // the fill emitter sits outside a character standing at the probe
constexpr float kFillMaxOffsetCm = 400.0f;
constexpr float kFillRadiusFraction = 0.5f;       // emitter radius / offset: a wide, soft source (60 deg across)
constexpr float kLuminance[3] = {0.2126f, 0.7152f, 0.0722f};

struct NodeData {
    uint32_t type = 0;               // flags & 7: 1 point, 2 spot, 3 directional, 4 SH probe
    uint32_t flags = 0;              // +0x34
    float position[3] = {};          // +0x350
    float orientation[3][4] = {};    // +0x360, column 0 = the way the light shines
    float nearSize[2] = {};          // +0x3A0 spot: width and height of the beam's near plane
    float nearDistance = 0.0f;       // +0x3A8 spot: from the beam's apex to that plane
    float range = 0.0f;              // +0x3B4
    float falloffStart = 0.0f;       // +0x3BC
    float color[3] = {};             // +0x3C4
    float intensity = 0.0f;          // +0x44C
    float probeRadius = 0.0f;        // +0x440 (type 4)
    float sh[7][4] = {};             // +0x230 (type 4)
};

struct Tuning {
    float radianceScale = 1.0f;      // [Lights] RadianceScale, plain multiplier, 1 = as the game
    float sunRadianceScale = 1.0f;   // [Lights] SunRadianceScale
    float spotSoftness = 0.1f;       // [Lights] SpotSoftness
    float sunAngularDiameter = 0.53f;// [Lights] SunAngularDiameter, degrees
    float shScale = 1.0f;            // the game's cSHScale, read from its character draws (1 until one is seen)
};

enum class Kind : uint8_t { None, Sphere, Distant, Dome };

// What lightpub hands to Remix for one game light (all distances in game units = cm).
struct PublishedLight {
    Kind kind = Kind::None;
    float position[3] = {};
    float radius = 0.0f;
    float radiance[3] = {};
    float direction[3] = {0.0f, 0.0f, -1.0f};  // spot axis or sun direction
    bool shaped = false;                       // spot cone
    float coneAngleDegrees = 0.0f;
    float coneSoftness = 0.0f;
    float angularDiameterDegrees = 0.0f;       // sun
    float volumetricRadianceScale = 1.0f;      // 0 for fill: bounce light does not light dust
};

struct CalibratedSphere {
    float radiusCm;
    float radianceMultiplier;
};

inline float MaxComponent(const float c[3]) { return (std::max)((std::max)(c[0], c[1]), c[2]); }

inline void Normalize(float v[3], float fx, float fy, float fz) {
    const float length = std::sqrt(fx * fx + fy * fy + fz * fz);
    if (!(length > 1e-6f)) { v[0] = 0.0f; v[1] = 0.0f; v[2] = -1.0f; return; }
    v[0] = fx / length; v[1] = fy / length; v[2] = fz / length;
}

// The game's attenuation: (1 - t^2)^2 with t mapping [falloffStart..range] to [0..1].
inline float GameLightFalloff(float rangeCm, float falloffStartCm, float distanceCm) {
    const float span = rangeCm - falloffStartCm;
    float t = span > 1.0e-3f ? (distanceCm - falloffStartCm) / span : 1.0f;
    t = std::clamp(t, 0.0f, 1.0f);
    const float u = 1.0f - t * t;
    return u * u;
}

// Where the close-range cap is checked: 0.5 m, or half the reach of a light that does not get that far.
inline float CloseCheckDistance(float rangeCm, float radiusCm) {
    return (std::max)(radiusCm, (std::min)(kCloseCheckDistanceCm, 0.5f * rangeCm));
}

// A Remix sphere light of radiance L and radius r gives a facing surface at distance d the irradiance
// pi L (r/d)^2, i.e. albedo x L x (r/d)^2 after the Lambert 1/pi, the game's units. This picks r and a multiplier
// m (radiance = colour x intensity x m) so that Remix equals the game at the reference distance but never exceeds
// kMaxCloseRatioTarget x the game at the close-range distance. The game's curve is flat near the light and the
// physical one is not, so one of the two always wins; the cap wins for most lights.
inline CalibratedSphere CalibrateSphereLight(float rangeCm, float falloffStartCm, float userRadianceScale = 1.0f) {
    const float radius = std::clamp(rangeCm * kRadiusRangeFraction, kMinFixtureRadiusCm, kMaxFixtureRadiusCm);
    if (!(rangeCm > 0.0f && rangeCm < 1.0e5f)) return {radius, userRadianceScale};

    const float dRef = std::clamp(rangeCm * kRefRangeFraction, radius, kMaxRefDistanceCm);
    const float refRatio = dRef / radius;
    const float scaleMid = GameLightFalloff(rangeCm, falloffStartCm, dRef) * refRatio * refRatio;

    const float dClose = CloseCheckDistance(rangeCm, radius);
    const float closeRatio = dClose / radius;
    const float scaleCap = kMaxCloseRatioTarget * GameLightFalloff(rangeCm, falloffStartCm, dClose) * closeRatio * closeRatio;

    return {radius, (std::min)(scaleMid, scaleCap) * userRadianceScale};
}

// Remix's irradiance (in the game's colour units, per unit colour x intensity) at distance d from a calibrated sphere.
inline float RemixSphereIrradiance(const CalibratedSphere& c, float distanceCm) {
    const float d = (std::max)(distanceCm, c.radiusCm);
    return c.radianceMultiplier * (c.radiusCm / d) * (c.radiusCm / d);
}

// ---- SH probes -------------------------------------------------------------------------------------------------

inline bool IsProbe(const NodeData& n) {
    if (n.type != 4 || !(n.flags & kProbeFlag) || !(n.probeRadius > 1.1920929e-07f) || !(n.probeRadius < 1.0e7f))
        return false;
    for (const auto& reg : n.sh)
        for (float v : reg)
            if (!std::isfinite(v)) return false;
    return true;
}

// The engine's irradiance-like value for a surface with unit normal (nx, ny, nz), per colour channel.
inline void EvaluateSh(const float sh[7][4], float nx, float ny, float nz, float out[3]) {
    const float b[4] = {nx * ny, ny * nz, nz * nz, nz * nx};
    const float c = nx * nx - ny * ny;
    for (int ch = 0; ch < 3; ++ch) {
        const float* a = sh[ch];
        const float* bb = sh[3 + ch];
        out[ch] = a[0] * nx + a[1] * ny + a[2] * nz + a[3] + bb[0] * b[0] + bb[1] * b[1] + bb[2] * b[2] + bb[3] * b[3] +
                  sh[6][ch] * c;
    }
}

// Average of EvaluateSh over all directions (the L0 band): cA.w + cB.z / 3.
inline void ShMean(const float sh[7][4], float out[3]) {
    for (int ch = 0; ch < 3; ++ch) out[ch] = (std::max)(0.0f, sh[ch][3] + sh[3 + ch][2] / 3.0f);
}

// Direction the probe's light comes from: the luminance-weighted linear (L1) band. Overhead when it has none.
inline void ShDominantDirection(const float sh[7][4], float out[3]) {
    float d[3] = {};
    for (int ch = 0; ch < 3; ++ch)
        for (int k = 0; k < 3; ++k) d[k] += kLuminance[ch] * sh[ch][k];
    if (std::sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]) < 1e-6f) { out[0] = 0.0f; out[1] = 0.0f; out[2] = 1.0f; return; }
    Normalize(out, d[0], d[1], d[2]);
}

// Room fill for one room-sized probe: a wide soft sphere on the bright side of the probe (where its light comes from),
// sized and bright enough that a surface at the probe facing it receives the probe's average value. The far side
// is left to Remix's real bounce light, which is what the baked probe recorded in the first place.
inline bool BuildRoomFill(const NodeData& n, const Tuning& tuning, PublishedLight* out) {
    if (!IsProbe(n) || n.probeRadius > kRoomFillMaxZoneRadiusCm) return false;
    float mean[3];
    ShMean(n.sh, mean);
    if (!(MaxComponent(mean) > 1e-5f)) return false;
    float dir[3];
    ShDominantDirection(n.sh, dir);
    const float offset = std::clamp(n.probeRadius, kFillMinOffsetCm, kFillMaxOffsetCm);
    const float radius = kFillRadiusFraction * offset;
    const float gain = (offset / radius) * (offset / radius) * tuning.shScale * tuning.radianceScale;
    *out = {};
    out->kind = Kind::Sphere;
    for (int k = 0; k < 3; ++k) {
        out->position[k] = n.position[k] + dir[k] * offset;
        out->radiance[k] = mean[k] * gain;
    }
    out->radius = radius;
    out->volumetricRadianceScale = 0.0f;
    return true;
}

// Sky-dome fill: the level-wide probe (the largest one, which covers every point the room probes miss) becomes a
// uniform Remix dome light of the same value. Remix only reaches the dome through rays that leave the level, so
// it lights what is open to the sky and nothing indoors.
inline bool BuildSkyDome(const NodeData* nodes, size_t count, const Tuning& tuning, PublishedLight* out) {
    const NodeData* widest = nullptr;
    for (size_t i = 0; i < count; ++i)
        if (IsProbe(nodes[i]) && nodes[i].probeRadius > kRoomFillMaxZoneRadiusCm &&
            (!widest || nodes[i].probeRadius > widest->probeRadius))
            widest = &nodes[i];
    if (!widest) return false;
    float mean[3];
    ShMean(widest->sh, mean);
    if (!(MaxComponent(mean) > 1e-6f)) return false;
    *out = {};
    out->kind = Kind::Dome;
    for (int k = 0; k < 3; ++k) out->radiance[k] = mean[k] * tuning.shScale * tuning.radianceScale;
    out->volumetricRadianceScale = 0.0f;
    return true;
}

// ---- One game light -> one Remix light --------------------------------------------------------------------------

inline bool Qualifies(const NodeData& n) {
    if (n.type == 4) return IsProbe(n);
    if (n.type < 1 || n.type > 3) return false;  // 0 = copies/templates
    return MaxComponent(n.color) * n.intensity > 1e-4f;  // also rejects NaN
}
inline bool IsWeaponSpotlight(const NodeData& n) {
    return n.type == 2 && n.range > 0.0f && n.range <= kWeaponSpotMaxGameRangeCm &&
           n.nearDistance > n.range && n.nearSize[0] > 0.0f && n.nearSize[1] > 0.0f;
}

inline CalibratedSphere CalibrateWeaponSpotlight(float userRadianceScale = 1.0f) {
    const float ratio = kWeaponSpotRefDistanceCm / kWeaponSpotRadiusCm;
    const float falloff = GameLightFalloff(kWeaponSpotReachCm, 0.0f, kWeaponSpotRefDistanceCm);
    return {kWeaponSpotRadiusCm, falloff * ratio * ratio * userRadianceScale};
}


inline bool ConvertGameLight(const NodeData& n, const Tuning& tuning, PublishedLight* out) {
    if (!Qualifies(n)) return false;
    if (n.type == 4) return BuildRoomFill(n, tuning, out);
    *out = {};
    // Column 0 of the orientation is the way the light shines.
    Normalize(out->direction, n.orientation[0][0], n.orientation[1][0], n.orientation[2][0]);
    if (n.type == 3) {
        // A Remix distant light of radiance L lights a facing surface like the game's colour L: no factor.
        out->kind = Kind::Distant;
        for (int k = 0; k < 3; ++k) out->radiance[k] = n.color[k] * n.intensity * tuning.sunRadianceScale;
        out->angularDiameterDegrees = tuning.sunAngularDiameter;
        return true;
    }
    const bool weaponSpot = IsWeaponSpotlight(n);
    const CalibratedSphere cal = weaponSpot
        ? CalibrateWeaponSpotlight(tuning.radianceScale)
        : CalibrateSphereLight(n.range, n.falloffStart, tuning.radianceScale);
    out->kind = Kind::Sphere;
    for (int k = 0; k < 3; ++k) {
        // Keep position anchored at n.position (confirmed by FlareNode at 9.07 cm and cSpot0 c64.xyz).
        out->position[k] = n.position[k];
        out->radiance[k] = n.color[k] * n.intensity * cal.radianceMultiplier;
    }
    out->radius = cal.radiusCm;
    if (n.type == 2) {
        float halfAngle = 45.0f;
        if (weaponSpot) {
            // Frustum divergence from apex: halfWidth = 0.5 * sqrt(w * h) at nearDistance -> ~9.46 deg half-angle (~19 deg beam).
            const float halfWidth = 0.5f * std::sqrt(n.nearSize[0] * n.nearSize[1]);
            halfAngle = std::atan2(halfWidth, n.nearDistance) * 57.29578f;
            out->volumetricRadianceScale = kWeaponSpotVolumetricScale;
        } else {
            // Architectural spotlights: match the game's rectangular beam + round cookie at half range.
            const float reach = n.range * 0.5f;
            if (n.nearSize[0] > 0.0f && n.nearSize[1] > 0.0f && n.nearDistance > 0.0f && reach > 0.0f) {
                const float halfWidth = std::sqrt(n.nearSize[0] * n.nearSize[1]) * 0.5f * (n.nearDistance + reach) / n.nearDistance;
                halfAngle = std::atan2(halfWidth, reach) * 57.29578f;
            }
        }
        out->shaped = true;
        out->coneAngleDegrees = std::clamp(halfAngle, 1.0f, 89.0f);
        out->coneSoftness = tuning.spotSoftness;
    }
    return true;
}

// Pure state tracker for game-to-Remix light lifecycle: updates moving/pulsing lights in place via CreateLight
// with a stable per-node handle hash (never DestroyLight + CreateLight on motion, which resets Remix's temporal
// light history and causes camera-turn / patrolling-flashlight flicker). DestroyLight runs only when a node
// disappears from the engine database.
struct TrackedEntry {
    uintptr_t node = 0;
    uint64_t stableHash = 0;
    uint64_t paramsHash = 0;
    void* handle = nullptr;
    uint32_t lastSeen = 0;
};

inline uint64_t StableNodeLightHash(uintptr_t node) {
    uint64_t h = 0xB1AC11570000ull;
    const uint8_t* p = reinterpret_cast<const uint8_t*>(&node);
    for (size_t i = 0; i < sizeof(node); ++i) h = (h ^ p[i]) * 1099511628211ull;
    return h | 1ull;
}

template <size_t MaxTracked>
class LightLifecycleTracker {
public:
    template <typename CreateOrUpdateFn>
    void Upsert(uintptr_t node, uint64_t paramsHash, const PublishedLight& pub, uint32_t frame,
                CreateOrUpdateFn&& createOrUpdate, uint32_t* createdCount, uint32_t* updatedCount, void** outHandle) {
        TrackedEntry* t = Find(node);
        if (!t && count_ < MaxTracked) {
            t = &entries_[count_++];
            *t = {node, StableNodeLightHash(node), 0, nullptr, 0};
        }
        if (!t) {
            *outHandle = nullptr;
            return;
        }
        if (!t->handle) {
            t->handle = createOrUpdate(t->stableHash, pub);
            t->paramsHash = paramsHash;
            if (t->handle && createdCount) ++*createdCount;
        } else if (t->paramsHash != paramsHash) {
            // Update in place on the identical handle hash; never call DestroyLight while the node stays active.
            void* updated = createOrUpdate(t->stableHash, pub);
            if (updated) t->handle = updated;
            t->paramsHash = paramsHash;
            if (updatedCount) ++*updatedCount;
        }
        t->lastSeen = frame;
        *outHandle = t->handle;
    }

    template <typename DestroyFn>
    void SweepUnseen(uint32_t frame, DestroyFn&& destroyFn, uint32_t* destroyedCount = nullptr) {
        for (size_t i = 0; i < count_;) {
            if (entries_[i].lastSeen != frame) {
                if (entries_[i].handle) {
                    destroyFn(entries_[i].handle);
                    if (destroyedCount) ++*destroyedCount;
                }
                entries_[i] = entries_[--count_];
            } else {
                ++i;
            }
        }
    }

    template <typename DestroyFn>
    void ClearAll(DestroyFn&& destroyFn) {
        for (size_t i = 0; i < count_; ++i) {
            if (entries_[i].handle) destroyFn(entries_[i].handle);
        }
        count_ = 0;
    }

    TrackedEntry* Find(uintptr_t node) {
        for (size_t i = 0; i < count_; ++i)
            if (entries_[i].node == node) return &entries_[i];
        return nullptr;
    }
    const TrackedEntry* Find(uintptr_t node) const {
        for (size_t i = 0; i < count_; ++i)
            if (entries_[i].node == node) return &entries_[i];
        return nullptr;
    }
    size_t Count() const { return count_; }
    TrackedEntry* Entries() { return entries_; }

private:
    TrackedEntry entries_[MaxTracked] = {};
    size_t count_ = 0;
};


}  // namespace lightcal
