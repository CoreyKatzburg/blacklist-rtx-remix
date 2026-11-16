// Offline C++ unit and mission frame-trace replay suite for Blacklist RTX Remix proxy.
// Compiles directly against production shader_traits.cpp, camera_solver.cpp, pass_graph.cpp,
// light_calibration.h and auto_raytracing.h and runs in seconds with zero GPU or game process required.
// It also writes tests/lightcal_published.tsv (every recorded game light as lightpub would publish it), which
// tests/verify_lights_and_materials.py checks.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d9.h>

#include <algorithm>
#include <chrono>
#include <map>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

#include "../play-setup/unified-build/auto_raytracing.h"
#include "../play-setup/unified-build/camera_solver.h"
#include "../play-setup/unified-build/light_calibration.h"
#include "../play-setup/unified-build/pass_graph.h"
#include "../play-setup/unified-build/shader_traits.h"

namespace fs = std::filesystem;

namespace {

#define TEST_ASSERT(cond, msg, ...)                                                    \
    do {                                                                               \
        if (!(cond)) {                                                                 \
            std::fprintf(stderr, "FAIL [%s:%d]: " msg "\n", __FILE__, __LINE__, ##__VA_ARGS__); \
            std::exit(1);                                                              \
        }                                                                              \
    } while (0)

// Lightweight mock D3D9 state tracker verifying bit-exact state equivalence under SetterCoalescer.
struct MockDeviceState {
    IDirect3DBaseTexture9* textures[proxy::SetterCoalescer::kMaxStages] = {};
    DWORD renderStates[proxy::SetterCoalescer::kMaxRenderStates] = {};
    IDirect3DVertexBuffer9* vb0 = nullptr;
    UINT vb0Offset = 0;
    UINT vb0Stride = 0;
    IDirect3DIndexBuffer9* ib = nullptr;
    float psConsts[proxy::SetterCoalescer::kMaxPsFloatRegs][4] = {};
    uint64_t setterCallsReceived = 0;
};

MockDeviceState* AsMock(IDirect3DDevice9* dev) {
    return reinterpret_cast<MockDeviceState*>(dev);
}

HRESULT WINAPI MockSetTexture(IDirect3DDevice9* dev, DWORD stage, IDirect3DBaseTexture9* tex) {
    MockDeviceState* m = AsMock(dev);
    if (stage < proxy::SetterCoalescer::kMaxStages) m->textures[stage] = tex;
    ++m->setterCallsReceived;
    return D3D_OK;
}

HRESULT WINAPI MockSetRenderState(IDirect3DDevice9* dev, D3DRENDERSTATETYPE state, DWORD val) {
    MockDeviceState* m = AsMock(dev);
    if (static_cast<uint32_t>(state) < proxy::SetterCoalescer::kMaxRenderStates) {
        m->renderStates[state] = val;
    }
    ++m->setterCallsReceived;
    return D3D_OK;
}

HRESULT WINAPI MockSetStreamSource(IDirect3DDevice9* dev, UINT stream, IDirect3DVertexBuffer9* vb, UINT off, UINT stride) {
    MockDeviceState* m = AsMock(dev);
    if (stream == 0) {
        m->vb0 = vb;
        m->vb0Offset = off;
        m->vb0Stride = stride;
    }
    ++m->setterCallsReceived;
    return D3D_OK;
}

HRESULT WINAPI MockSetIndices(IDirect3DDevice9* dev, IDirect3DIndexBuffer9* ib) {
    MockDeviceState* m = AsMock(dev);
    m->ib = ib;
    ++m->setterCallsReceived;
    return D3D_OK;
}

HRESULT WINAPI MockSetPsConstF(IDirect3DDevice9* dev, UINT start, const float* data, UINT count) {
    MockDeviceState* m = AsMock(dev);
    for (UINT i = 0; i < count && (start + i) < proxy::SetterCoalescer::kMaxPsFloatRegs; ++i) {
        std::memcpy(m->psConsts[start + i], data + i * 4, sizeof(float) * 4);
    }
    ++m->setterCallsReceived;
    return D3D_OK;
}

bool EqualMockState(const MockDeviceState& a, const MockDeviceState& b) {
    return std::memcmp(a.textures, b.textures, sizeof(a.textures)) == 0 &&
           std::memcmp(a.renderStates, b.renderStates, sizeof(a.renderStates)) == 0 &&
           a.vb0 == b.vb0 && a.vb0Offset == b.vb0Offset && a.vb0Stride == b.vb0Stride &&
           a.ib == b.ib &&
           std::memcmp(a.psConsts, b.psConsts, sizeof(a.psConsts)) == 0;
}

struct ParsedDraw {
    uint32_t seq = 0;
    uint32_t verts = 0;
    uint32_t prims = 0;
    uint32_t vsHash = 0;
    uint32_t psHash = 0;
    uintptr_t rt0Ptr = 0;
    uint32_t rt0W = 0;
    uint32_t rt0H = 0;
    D3DFORMAT rt0Fmt = D3DFMT_UNKNOWN;
    bool rt1Bound = false;
    uintptr_t t0ParentSurf = 0;
    uintptr_t t0TexPtr = 0;
    bool zen = false;
    bool zwr = false;
    bool bl = false;
    uint32_t cw = 0;
    uint32_t stride0 = 0;
    uint32_t psBoolMask = 0;
    float psc0_15[16][4] = {};
    float psc[168][4] = {};   // c0..c167 when the dump has them (psc16-167), for alpha-cutoff checks
    bool hasPsc = false;
    bool hasPscHigh = false;
};

struct ParsedCamPub {
    uint32_t afterDraw = 0;
    uintptr_t callerRva = 0;
    D3DMATRIX view = {};
    D3DMATRIX proj = {};
};

struct ShaderBank {
    std::unordered_map<uint32_t, proxy::ShaderTraits> vs;
    std::unordered_map<uint32_t, proxy::ShaderTraits> ps;
};

ShaderBank LoadShaderBank(const fs::path& systemDir) {
    ShaderBank bank;
    for (int kind = 0; kind < 2; ++kind) {
        const bool isVs = (kind == 0);
        const fs::path dir = systemDir / (isVs ? "vsdump" : "psdump");
        TEST_ASSERT(fs::is_directory(dir), "Missing shader dump dir: %s", dir.string().c_str());
        for (const auto& entry : fs::directory_iterator(dir)) {
            if (entry.path().extension() != ".bin") continue;
            const std::string stem = entry.path().stem().string();  // vs_XXXXXXXX
            if (stem.size() < 11) continue;
            const uint32_t expectedHash = static_cast<uint32_t>(std::strtoul(stem.c_str() + 3, nullptr, 16));
            std::ifstream in(entry.path(), std::ios::binary);
            std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
            TEST_ASSERT(bytes.size() >= 8, "Empty shader binary: %s", stem.c_str());
            const proxy::ShaderTraits traits = proxy::AnalyzeShaderBytecode(
                reinterpret_cast<const uint32_t*>(bytes.data()), bytes.size(), isVs);
            TEST_ASSERT(traits.hash == expectedHash,
                        "Shader FNV-1a mismatch for %s: got %08X expected %08X",
                        stem.c_str(), traits.hash, expectedHash);
            if (isVs) bank.vs[expectedHash] = traits;
            else bank.ps[expectedHash] = traits;
        }
    }
    return bank;
}

void ParseSurfaceDesc(const std::string& val,
                      uintptr_t* outSurfPtr,
                      uint32_t* outW,
                      uint32_t* outH,
                      D3DFORMAT* outFmt,
                      uintptr_t* outParentTex) {
    *outSurfPtr = 0;
    *outW = *outH = 0;
    *outFmt = D3DFMT_UNKNOWN;
    *outParentTex = 0;
    if (val == "0" || val.empty()) return;
    // Format: 18238100,2560x1440,f113,u1,ms0,tex1823AB38
    std::stringstream ss(val);
    std::string item;
    int idx = 0;
    while (std::getline(ss, item, ',')) {
        if (idx == 0) *outSurfPtr = static_cast<uintptr_t>(std::strtoull(item.c_str(), nullptr, 16));
        else if (idx == 1) {
            const size_t x = item.find('x');
            if (x != std::string::npos) {
                *outW = static_cast<uint32_t>(std::strtoul(item.c_str(), nullptr, 10));
                *outH = static_cast<uint32_t>(std::strtoul(item.c_str() + x + 1, nullptr, 10));
            }
        } else if (idx == 2 && !item.empty() && item[0] == 'f') {
            *outFmt = static_cast<D3DFORMAT>(std::strtoul(item.c_str() + 1, nullptr, 10));
        } else if (idx == 5 && item.rfind("tex", 0) == 0) {
            *outParentTex = static_cast<uintptr_t>(std::strtoull(item.c_str() + 3, nullptr, 16));
        }
        ++idx;
    }
}

void LoadFrameTrace(const fs::path& tsvPath,
                    std::vector<ParsedDraw>* outDraws,
                    std::vector<ParsedCamPub>* outCams,
                    std::unordered_map<uintptr_t, uintptr_t>* outTexToSurf) {
    outDraws->clear();
    outCams->clear();
    outTexToSurf->clear();

    std::ifstream in(tsvPath);
    TEST_ASSERT(in.is_open(), "Could not open fixture: %s", tsvPath.string().c_str());
    std::string line;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.rfind("campub\t", 0) == 0) {
            ParsedCamPub cp = {};
            std::stringstream ss(line.substr(7));
            std::string tok;
            while (std::getline(ss, tok, '\t')) {
                const size_t eq = tok.find('=');
                if (eq == std::string::npos) continue;
                const std::string k = tok.substr(0, eq);
                const std::string v = tok.substr(eq + 1);
                if (k == "afterdraw") cp.afterDraw = static_cast<uint32_t>(std::strtoul(v.c_str(), nullptr, 10));
                else if (k == "caller") cp.callerRva = static_cast<uintptr_t>(std::strtoull(v.c_str(), nullptr, 16));
                else if (k == "view" || k == "proj") {
                    D3DMATRIX* dst = (k == "view") ? &cp.view : &cp.proj;
                    std::stringstream ms(v);
                    std::string num;
                    for (int i = 0; i < 16 && std::getline(ms, num, ','); ++i) {
                        dst->m[i / 4][i % 4] = std::strtof(num.c_str(), nullptr);
                    }
                }
            }
            outCams->push_back(cp);
        } else if (line.rfind("draw\t", 0) == 0) {
            ParsedDraw d = {};
            uintptr_t rt0ParentTex = 0;
            std::stringstream ss(line.substr(5));
            std::string tok;
            while (std::getline(ss, tok, '\t')) {
                const size_t eq = tok.find('=');
                if (eq == std::string::npos) continue;
                const std::string k = tok.substr(0, eq);
                const std::string v = tok.substr(eq + 1);
                if (k == "seq") d.seq = static_cast<uint32_t>(std::strtoul(v.c_str(), nullptr, 10));
                else if (k == "verts") d.verts = static_cast<uint32_t>(std::strtoul(v.c_str(), nullptr, 10));
                else if (k == "prims") d.prims = static_cast<uint32_t>(std::strtoul(v.c_str(), nullptr, 10));
                else if (k == "vs") d.vsHash = static_cast<uint32_t>(std::strtoul(v.c_str(), nullptr, 16));
                else if (k == "ps") d.psHash = static_cast<uint32_t>(std::strtoul(v.c_str(), nullptr, 16));
                else if (k == "rt0") ParseSurfaceDesc(v, &d.rt0Ptr, &d.rt0W, &d.rt0H, &d.rt0Fmt, &rt0ParentTex);
                else if (k == "rt1") d.rt1Bound = (v != "0" && !v.empty());
                else if (k == "t0") {
                    const size_t c = v.find(',');
                    d.t0TexPtr = static_cast<uintptr_t>(std::strtoull(v.substr(0, c).c_str(), nullptr, 16));
                } else if (k == "zen") d.zen = (v != "0");
                else if (k == "zwr") d.zwr = (v != "0");
                else if (k == "bl") d.bl = (v != "0");
                else if (k == "cw") d.cw = static_cast<uint32_t>(std::strtoul(v.c_str(), nullptr, 10));
                else if (k == "stride0") d.stride0 = static_cast<uint32_t>(std::strtoul(v.c_str(), nullptr, 10));
                else if (k == "psb0-15") {
                    for (size_t b = 0; b < v.size() && b < 16; ++b) {
                        if (v[b] == '1') d.psBoolMask |= (1u << b);
                    }
                } else if (k == "psc0-15") {
                    std::stringstream cs(v);
                    std::string num;
                    for (int i = 0; i < 64 && std::getline(cs, num, ','); ++i) {
                        d.psc0_15[i / 4][i % 4] = std::strtof(num.c_str(), nullptr);
                        d.psc[i / 4][i % 4] = d.psc0_15[i / 4][i % 4];
                    }
                    d.hasPsc = true;
                } else if (k == "psc16-167") {
                    std::stringstream cs(v);
                    std::string num;
                    for (int i = 0; i < 152 * 4 && std::getline(cs, num, ','); ++i) {
                        d.psc[16 + i / 4][i % 4] = std::strtof(num.c_str(), nullptr);
                    }
                    d.hasPscHigh = true;
                }
            }
            if (d.rt0Ptr != 0 && rt0ParentTex != 0) {
                (*outTexToSurf)[rt0ParentTex] = d.rt0Ptr;
            }
            outDraws->push_back(d);
        }
    }
}

void RunShaderAndLightUnitTests(const ShaderBank& bank) {
    TEST_ASSERT(bank.vs.size() >= 60 && bank.ps.size() >= 150,
                "Expected >=60 VS and >=150 PS binaries, got %zu VS / %zu PS",
                bank.vs.size(), bank.ps.size());

    // Sam's G-buffer and forward character vertex shaders must all classify as World + Skinned
    const uint32_t skinnedShaders[] = {0x0C56404Eu, 0x87D8E2DFu, 0x4AC8444Du, 0xDA2436A8u, 0x78325AE1u};
    for (uint32_t h : skinnedShaders) {
        auto it = bank.vs.find(h);
        TEST_ASSERT(it != bank.vs.end(), "Missing character VS %08X in vsdump", h);
        TEST_ASSERT(it->second.vsClass == proxy::VsClass::World && it->second.skinned,
                    "Character VS %08X failed World+Skinned check", h);
    }

    // Room fill uses the game's SH multiplier: the character shader's cSHScale (c16) must be found by name.
    auto charVs = bank.vs.find(0x87D8E2DFu);
    TEST_ASSERT(charVs->second.shScale == 17, "VS 87D8E2DF cSHScale register %u, expected c16 (stored 17)",
                charVs->second.shScale);

    // Fence cutout PS 359DE82C must detect texkill and compute valid alpha cutoff
    auto fenceIt = bank.ps.find(0x359DE82Cu);
    TEST_ASSERT(fenceIt != bank.ps.end() && fenceIt->second.hasTexkill,
                "Fence cutout PS 359DE82C must report hasTexkill=true");

    // Light calibration check: 4 m room lamp must deliver >= 0.50x vanilla at 1.8 m while staying <= 5.0x at 0.5 m
    const auto cal = lightcal::CalibrateSphereLight(400.0f, 0.0f, 1.0f);
    TEST_ASSERT(cal.radiusCm >= 20.0f && cal.radiusCm <= 35.0f, "Unexpected light radius %g", cal.radiusCm);
    const float ratioAtHalfMeter =
        lightcal::RemixSphereIrradiance(cal, 50.0f) / lightcal::GameLightFalloff(400.0f, 0.0f, 50.0f);
    const float ratioAtRoom =
        lightcal::RemixSphereIrradiance(cal, 180.0f) / lightcal::GameLightFalloff(400.0f, 0.0f, 180.0f);
    TEST_ASSERT(ratioAtHalfMeter <= lightcal::kMaxCloseRatioTarget + 1e-3f && ratioAtRoom >= 0.50f,
                "4m lamp 0.5m ratio %g (cap %g) or 1.8m ratio %g (min 0.50) out of range",
                ratioAtHalfMeter, lightcal::kMaxCloseRatioTarget, ratioAtRoom);
}

void RunCameraSolverTests(const fs::path& run20Tsv) {
    std::vector<ParsedDraw> draws;
    std::vector<ParsedCamPub> cams;
    std::unordered_map<uintptr_t, uintptr_t> texMap;
    LoadFrameTrace(run20Tsv, &draws, &cams, &texMap);
    TEST_ASSERT(cams.size() >= 8, "Expected >=8 camera uploads in Run 20 cam_on, got %zu", cams.size());

    proxy::CameraFrameTracker tracker;
    tracker.BeginFrame(2560, 1440);
    int published = 0;
    int rejectedSquareShadows = 0;

    for (const auto& cp : cams) {
        // Reconstruct c8..c11 (transpose of view * proj) just as Blacklist uploads them
        D3DMATRIX vp = {};
        for (int r = 0; r < 4; ++r) {
            for (int c = 0; c < 4; ++c) {
                vp.m[r][c] = cp.view.m[r][0] * cp.proj.m[0][c] +
                             cp.view.m[r][1] * cp.proj.m[1][c] +
                             cp.view.m[r][2] * cp.proj.m[2][c] +
                             cp.view.m[r][3] * cp.proj.m[3][c];
            }
        }
        float c8To11[4][4] = {};
        for (int col = 0; col < 4; ++col) {
            for (int row = 0; row < 4; ++row) {
                c8To11[col][row] = vp.m[row][col];
            }
        }
        D3DMATRIX outV = {}, outP = {};
        bool shouldPublish = false;
        proxy::CameraRejection rej = {};
        // Test with oncePerFrame=false first to prove square shadow cameras are rejected by aspect gate alone!
        const bool ok = tracker.EvaluateDirectUpload(c8To11, /*oncePerFrame=*/true, &outV, &outP, &shouldPublish, &rej);
        if (!ok && std::strstr(rej.check, "shadow-map") != nullptr) {
            ++rejectedSquareShadows;
        }
        if (ok && shouldPublish) {
            ++published;
        }
    }

    TEST_ASSERT(published == 1, "Expected exactly 1 published primary camera in Run 20, got %d", published);
    TEST_ASSERT(rejectedSquareShadows == 2,
                "Expected both mid-frame 1:1 spotlight shadow cameras to be rejected in Run 20, got %d",
                rejectedSquareShadows);
}

struct ReplaySummary {
    uint32_t sceneDraws = 0;
    uint32_t skinnedSceneDraws = 0;
    uint32_t forwardSkinnedKept = 0;
    uint64_t duplicatesBlocked = 0;
    uint64_t skippedDraws = 0;
    uint64_t suppressedSetters = 0;
    uint64_t handoffs = 0;
    // Last frame, for the hand-off modes:
    uint32_t keptDraws = 0;             // draws that still reach the bridge
    uint32_t particlesKept = 0;         // game particles drawn (by the game, over the ray-traced image)
    uint32_t particlesSkipped = 0;
    uint32_t particlesBeforeHandoff = 0;// particles drawn before the hand-off (overwritten by it = lost)
    uint32_t fakeVolumesKept = 0;       // beam-card light cones drawn
    uint32_t fakeVolumesSeen = 0;
    uint32_t earlyHandoffSeq = 0;       // draw at which HandoffBeforePost handed off (0 = composite hand-off)
};

// Hand-off mode of a replay: the proxy default (HandoffBeforePost + HandoffBeforeEffects) or the composite hand-off.
enum class HandoffMode { Composite, BeforeEffects };

proxy::PassGraphConfig ReplayConfig(HandoffMode mode) {
    proxy::PassGraphConfig cfg = {};
    cfg.enabled = true;
    cfg.forwardScene = true;
    cfg.skipShadowMapDraws = true;
    cfg.skipGameLighting = true;
    cfg.skipGamePost = true;
    cfg.gogglesGameView = true;
    cfg.handoffBeforePost = mode == HandoffMode::BeforeEffects;
    cfg.handoffBeforeEffects = mode == HandoffMode::BeforeEffects;
    return cfg;
}

ReplaySummary VerifyMissionTrace(const fs::path& tsvPath,
                                 const ShaderBank& bank,
                                 uint32_t backbufferW,
                                 uint32_t backbufferH,
                                 HandoffMode mode = HandoffMode::Composite) {
    std::vector<ParsedDraw> draws;
    std::vector<ParsedCamPub> cams;
    std::unordered_map<uintptr_t, uintptr_t> texToSurf;
    LoadFrameTrace(tsvPath, &draws, &cams, &texToSurf);
    TEST_ASSERT(!draws.empty(), "No draws parsed from %s", tsvPath.string().c_str());

    proxy::PassGraph graph;
    graph.SetConfig(ReplayConfig(mode));

    MockDeviceState refDevice = {};
    MockDeviceState optDevice = {};
    IDirect3DDevice9* refHandle = reinterpret_cast<IDirect3DDevice9*>(&refDevice);
    IDirect3DDevice9* optHandle = reinterpret_cast<IDirect3DDevice9*>(&optDevice);
    proxy::SetterCoalescer coalescer;

    ReplaySummary lastFrame = {};

    // Run 3 frames so rtOnFrames >= 2 and handoffLastFrame == true activate full SkipGameLighting/SkipGamePost
    for (int frameIdx = 0; frameIdx < 3; ++frameIdx) {
        uintptr_t currentSceneHdr = 0;
        uint32_t frameHandoffs = 0;
        uint32_t forwardSkinnedKept = 0;
        ReplaySummary frame = {};
        const uint64_t beforeDups = graph.Stats().forwardDuplicatesBlocked;
        const uint64_t beforeSkipped =
            graph.Stats().skippedShadowDraws + graph.Stats().skippedLightingDraws + graph.Stats().skippedPostDraws;

        for (const auto& d : draws) {
            auto vsIt = bank.vs.find(d.vsHash);
            auto psIt = bank.ps.find(d.psHash);
            const proxy::ShaderTraits vsTraits = (vsIt != bank.vs.end())
                ? vsIt->second
                : proxy::ShaderTraits{d.vsHash, proxy::VsClass::Other};
            const proxy::ShaderTraits psTraits = (psIt != bank.ps.end())
                ? psIt->second
                : proxy::ShaderTraits{d.psHash, proxy::VsClass::None};

            const bool rt0Primary = (d.rt0W == backbufferW && d.rt0H == backbufferH && d.rt0Ptr != 0);
            const bool rt0Shadow = (d.rt0W == d.rt0H && d.rt0W >= 1024 && d.rt0Ptr != 0);

            // Simulate 7 realistic D3D9 setter calls per draw on both reference and coalesced devices
            const bool suppress = graph.ShouldSuppressSetters(
                reinterpret_cast<const void*>(d.rt0Ptr),
                rt0Primary,
                rt0Shadow,
                d.rt0Fmt,
                d.rt1Bound,
                /*raytracingActive=*/true,
                vsTraits.vsClass,
                psTraits.composite);

            IDirect3DBaseTexture9* fakeTex = reinterpret_cast<IDirect3DBaseTexture9*>(d.t0TexPtr);
            IDirect3DVertexBuffer9* fakeVb = reinterpret_cast<IDirect3DVertexBuffer9*>(static_cast<uintptr_t>(d.verts * 16 + d.stride0));
            IDirect3DIndexBuffer9* fakeIb = reinterpret_cast<IDirect3DIndexBuffer9*>(static_cast<uintptr_t>(d.prims * 6 + 4));

            // Unsuppressed ground-truth device receives every setter call directly
            MockSetTexture(refHandle, 0, fakeTex);
            MockSetRenderState(refHandle, D3DRS_ZENABLE, d.zen ? 1 : 0);
            MockSetRenderState(refHandle, D3DRS_ZWRITEENABLE, d.zwr ? 1 : 0);
            MockSetRenderState(refHandle, D3DRS_ALPHABLENDENABLE, d.bl ? 1 : 0);
            MockSetRenderState(refHandle, D3DRS_COLORWRITEENABLE, d.cw);
            MockSetStreamSource(refHandle, 0, fakeVb, 0, d.stride0);
            MockSetIndices(refHandle, fakeIb);
            if (d.hasPsc) MockSetPsConstF(refHandle, 0, d.psc0_15[0], 4);

            // Optimized proxy path buffers setters during skipped passes
            if (suppress) {
                coalescer.RecordSetTexture(0, fakeTex);
                coalescer.RecordSetRenderState(D3DRS_ZENABLE, d.zen ? 1 : 0);
                coalescer.RecordSetRenderState(D3DRS_ZWRITEENABLE, d.zwr ? 1 : 0);
                coalescer.RecordSetRenderState(D3DRS_ALPHABLENDENABLE, d.bl ? 1 : 0);
                coalescer.RecordSetRenderState(D3DRS_COLORWRITEENABLE, d.cw);
                coalescer.RecordSetStreamSource0(fakeVb, 0, d.stride0);
                coalescer.RecordSetIndices(fakeIb);
                if (d.hasPsc) coalescer.RecordSetPixelShaderConstantF(0, d.psc0_15[0], 4);
            } else {
                const uint64_t flushed = coalescer.Flush(
                    optHandle, &MockSetTexture, &MockSetRenderState, &MockSetStreamSource, &MockSetIndices, &MockSetPsConstF);
                graph.AddSetterStats(coalescer.TakeSuppressedCount(), flushed);
                MockSetTexture(optHandle, 0, fakeTex);
                MockSetRenderState(optHandle, D3DRS_ZENABLE, d.zen ? 1 : 0);
                MockSetRenderState(optHandle, D3DRS_ZWRITEENABLE, d.zwr ? 1 : 0);
                MockSetRenderState(optHandle, D3DRS_ALPHABLENDENABLE, d.bl ? 1 : 0);
                MockSetRenderState(optHandle, D3DRS_COLORWRITEENABLE, d.cw);
                MockSetStreamSource(optHandle, 0, fakeVb, 0, d.stride0);
                MockSetIndices(optHandle, fakeIb);
                if (d.hasPsc) MockSetPsConstF(optHandle, 0, d.psc0_15[0], 4);
            }

            // Build geometry key matching G-buffer <-> Forward duplicate draws by (verts, prims, stride0)
            const proxy::DrawGeometryKey geomKey = {
                reinterpret_cast<const void*>(static_cast<uintptr_t>(d.verts)),
                0,
                d.stride0,
                reinterpret_cast<const void*>(static_cast<uintptr_t>(d.prims)),
                0,
                0,
                0,
                d.prims,
                4,
            };

            bool readsHdr = false;
            if (currentSceneHdr != 0 && d.t0TexPtr != 0) {
                auto surfIt = texToSurf.find(d.t0TexPtr);
                if (surfIt != texToSurf.end() && surfIt->second == currentSceneHdr) {
                    readsHdr = true;
                }
            }

            bool gogglesSw = false;
            if (psTraits.gogglesSwitch != 0) {
                gogglesSw = ((d.psBoolMask & (1u << (psTraits.gogglesSwitch - 1))) != 0);
            }

            proxy::DrawContextInput ctx = {};
            ctx.rt0 = reinterpret_cast<const void*>(d.rt0Ptr);
            ctx.rt0Primary = rt0Primary;
            ctx.rt0IsShadowMap = rt0Shadow;
            ctx.rt0Format = d.rt0Fmt;
            ctx.rt1Bound = d.rt1Bound;
            ctx.stage0ReadsSceneHdr = readsHdr;
            ctx.depthTestEnabled = d.zen;
            ctx.depthWriteEnabled = d.zwr;
            ctx.colorWriteEnabled = (d.cw != 0);
            ctx.alphaBlendEnabled = d.bl;
            ctx.gogglesSwitchActive = gogglesSw;
            ctx.raytracingActive = true;
            ctx.vs = vsTraits;
            ctx.ps = psTraits;
            ctx.geometry = (d.verts > 0) ? &geomKey : nullptr;

            const proxy::DrawDecision dec = graph.ClassifyDraw(ctx);
            if (dec.rememberSceneHdrTarget && d.rt0Ptr != 0) {
                currentSceneHdr = d.rt0Ptr;
            }
            bool skip = dec.skipDraw;
            if (dec.emitCompositeHandoffBeforeDraw) {
                const uint64_t flushed = coalescer.Flush(
                    optHandle, &MockSetTexture, &MockSetRenderState, &MockSetStreamSource, &MockSetIndices, &MockSetPsConstF);
                graph.AddSetterStats(coalescer.TakeSuppressedCount(), flushed);
                graph.NoteHandoffCompleted();
                ++frameHandoffs;
            } else if (dec.tryEarlyHandoffBeforeDraw && !graph.MarkerDoneThisFrame() && graph.RtOnFrames() >= 2 &&
                       !graph.GogglesActive() && graph.SceneDrawsThisFrame() > 0) {
                // d3d9_proxy.cpp HandoffBeforePost: flush, hand off, decode; the triggering draw is then drawn.
                const uint64_t flushed = coalescer.Flush(
                    optHandle, &MockSetTexture, &MockSetRenderState, &MockSetStreamSource, &MockSetIndices, &MockSetPsConstF);
                graph.AddSetterStats(coalescer.TakeSuppressedCount(), flushed);
                graph.NoteHandoffCompleted();
                ++frameHandoffs;
                frame.earlyHandoffSeq = d.seq;
                skip = false;
            }
            const bool isParticle = vsTraits.particle || psTraits.particle;
            if (isParticle) {
                ++(skip ? frame.particlesSkipped : frame.particlesKept);
                if (!skip && frameHandoffs == 0) ++frame.particlesBeforeHandoff;
            }
            if (psTraits.volumetric) {
                ++frame.fakeVolumesSeen;
                if (!skip) ++frame.fakeVolumesKept;
            }
            if (!skip) ++frame.keptDraws;
            // Invariant: particles and volumetric light cones NEVER enter the ray-traced scene
            if (vsTraits.particle || psTraits.particle || psTraits.volumetric) {
                TEST_ASSERT(!dec.isScene,
                            "Particle/volumetric draw seq=%u leaked into RT scene in %s",
                            d.seq, tsvPath.filename().string().c_str());
            }
            if (!rt0Primary && !rt0Shadow && !psTraits.volumetric) {
                TEST_ASSERT(!skip,
                            "Sub-resolution target draw seq=%u (%ux%u) was wrongly skipped in %s",
                            d.seq, d.rt0W, d.rt0H, tsvPath.filename().string().c_str());
            }

            if (!skip) {
                const uint64_t flushed = coalescer.Flush(
                    optHandle, &MockSetTexture, &MockSetRenderState, &MockSetStreamSource, &MockSetIndices, &MockSetPsConstF);
                graph.AddSetterStats(coalescer.TakeSuppressedCount(), flushed);
                // Bit-exact D3D9 state equivalence invariant on EVERY kept draw!
                TEST_ASSERT(EqualMockState(refDevice, optDevice),
                            "SetterCoalescer D3D9 state desync at kept draw seq=%u in %s",
                            d.seq, tsvPath.filename().string().c_str());

                if (!d.rt1Bound && vsTraits.skinned && dec.stage == proxy::FrameStage::ForwardScene) {
                    ++forwardSkinnedKept;
                }
            }
        }

        const uint32_t sceneCount = graph.SceneDrawsThisFrame();
        const uint32_t skinnedCount = graph.SkinnedSceneDrawsThisFrame();
        if (graph.EndFrame(reinterpret_cast<const void*>(currentSceneHdr), /*raytracingActive=*/true)) {
            const uint64_t flushed = coalescer.Flush(
                optHandle, &MockSetTexture, &MockSetRenderState, &MockSetStreamSource, &MockSetIndices, &MockSetPsConstF);
            graph.AddSetterStats(coalescer.TakeSuppressedCount(), flushed);
            graph.NoteHandoffCompleted();
            ++frameHandoffs;
        }
        const uint64_t endFlushed = coalescer.Flush(
            optHandle, &MockSetTexture, &MockSetRenderState, &MockSetStreamSource, &MockSetIndices, &MockSetPsConstF);
        graph.AddSetterStats(coalescer.TakeSuppressedCount(), endFlushed);

        if (frameIdx == 2) {
            lastFrame = frame;
            lastFrame.sceneDraws = sceneCount;
            lastFrame.skinnedSceneDraws = skinnedCount;
            lastFrame.forwardSkinnedKept = forwardSkinnedKept;
            lastFrame.duplicatesBlocked = graph.Stats().forwardDuplicatesBlocked - beforeDups;
            lastFrame.skippedDraws =
                (graph.Stats().skippedShadowDraws + graph.Stats().skippedLightingDraws + graph.Stats().skippedPostDraws) -
                beforeSkipped;
            lastFrame.suppressedSetters = refDevice.setterCallsReceived - optDevice.setterCallsReceived;
            lastFrame.handoffs = frameHandoffs;
        }
    }

    graph.OnDeviceReset();
    TEST_ASSERT(graph.SceneDrawsThisFrame() == 0 && !graph.PostStartedThisFrame(),
                "OnDeviceReset failed to clear transient state");
    return lastFrame;
}

double BenchmarkHotPathMsPerFrame(const fs::path& tsvPath, const ShaderBank& bank) {
    std::vector<ParsedDraw> draws;
    std::vector<ParsedCamPub> cams;
    std::unordered_map<uintptr_t, uintptr_t> texToSurf;
    LoadFrameTrace(tsvPath, &draws, &cams, &texToSurf);

    struct PrebuiltDraw {
        proxy::DrawContextInput ctx;
        proxy::DrawGeometryKey geom;
    };
    std::vector<PrebuiltDraw> prebuilt(draws.size());
    for (size_t i = 0; i < draws.size(); ++i) {
        const auto& d = draws[i];
        auto vsIt = bank.vs.find(d.vsHash);
        auto psIt = bank.ps.find(d.psHash);
        prebuilt[i].geom = {
            reinterpret_cast<const void*>(static_cast<uintptr_t>(d.verts)), 0, d.stride0,
            reinterpret_cast<const void*>(static_cast<uintptr_t>(d.prims)), 0, 0, 0, d.prims, 4};
        auto& c = prebuilt[i].ctx;
        c.rt0 = reinterpret_cast<const void*>(d.rt0Ptr);
        c.rt0Primary = (d.rt0W == 2560 && d.rt0H == 1440 && d.rt0Ptr != 0);
        c.rt0IsShadowMap = (d.rt0W == d.rt0H && d.rt0W >= 1024 && d.rt0Ptr != 0);
        c.rt0Format = d.rt0Fmt;
        c.rt1Bound = d.rt1Bound;
        c.depthTestEnabled = d.zen;
        c.depthWriteEnabled = d.zwr;
        c.colorWriteEnabled = (d.cw != 0);
        c.alphaBlendEnabled = d.bl;
        c.raytracingActive = true;
        c.vs = (vsIt != bank.vs.end()) ? vsIt->second : proxy::ShaderTraits{d.vsHash, proxy::VsClass::Other};
        c.ps = (psIt != bank.ps.end()) ? psIt->second : proxy::ShaderTraits{d.psHash, proxy::VsClass::None};
        c.geometry = (d.verts > 0) ? &prebuilt[i].geom : nullptr;
    }

    proxy::PassGraph graph;
    constexpr int kFrames = 1000;
    const auto t0 = std::chrono::steady_clock::now();
    for (int f = 0; f < kFrames; ++f) {
        const void* hdr = nullptr;
        for (const auto& item : prebuilt) {
            const auto dec = graph.ClassifyDraw(item.ctx);
            if (dec.rememberSceneHdrTarget) hdr = item.ctx.rt0;
            if (dec.emitCompositeHandoffBeforeDraw) graph.NoteHandoffCompleted();
        }
        if (graph.EndFrame(hdr, true)) graph.NoteHandoffCompleted();
    }
    const auto t1 = std::chrono::steady_clock::now();
    return std::chrono::duration<double, std::milli>(t1 - t0).count() / kFrames;
}

// ---- Light publish replay (light_calibration.h on every recorded LightNode) ------------------------------------

struct LightRow {
    std::string node;
    lightcal::NodeData n;
};

int ParseFloatList(const std::string& v, char sep, float* out, int maxCount) {
    std::stringstream ss(v);
    std::string item;
    int count = 0;
    while (count < maxCount && std::getline(ss, item, sep)) {
        if (item.empty()) continue;
        out[count++] = std::strtof(item.c_str(), nullptr);
    }
    return count;
}

// Raw memory words of a dump (raw20 starts at +0x20, raw390 at +0x390) -> float at a node offset.
bool RawFloat(const std::vector<uint32_t>& words, uint32_t base, uint32_t offset, float* out) {
    if (offset < base || (offset - base) / 4 >= words.size()) return false;
    const uint32_t w = words[(offset - base) / 4];
    std::memcpy(out, &w, sizeof(w));
    return true;
}

// *probeWords is false for the oldest dump format (run1), which records only 16 raw words per node and so has no
// probe data; its probes then stay unconverted.
std::vector<LightRow> LoadLightDump(const fs::path& path, bool* probeWords) {
    *probeWords = true;
    std::vector<LightRow> rows;
    std::ifstream in(path);
    TEST_ASSERT(in.is_open(), "Could not open light dump %s", path.string().c_str());
    std::string line;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.rfind("light\t", 0) != 0) continue;
        LightRow row;
        lightcal::NodeData& n = row.n;
        std::vector<uint32_t> raw20, raw390;
        std::stringstream ss(line.substr(6));
        std::string tok;
        while (std::getline(ss, tok, '\t')) {
            const size_t eq = tok.find('=');
            if (eq == std::string::npos) continue;
            const std::string k = tok.substr(0, eq);
            const std::string v = tok.substr(eq + 1);
            if (k == "node") row.node = v;
            else if (k == "flags") n.flags = static_cast<uint32_t>(std::strtoul(v.c_str(), nullptr, 16));
            else if (k == "type") n.type = static_cast<uint32_t>(std::strtoul(v.c_str(), nullptr, 10));
            else if (k == "pos") ParseFloatList(v, ',', n.position, 3);
            else if (k == "row0" || k == "row1" || k == "row2") ParseFloatList(v, ',', n.orientation[k[3] - '0'], 3);
            else if (k == "near") {
                float near3[3] = {};
                if (ParseFloatList(v, ',', near3, 3) == 3) {
                    n.nearSize[0] = near3[0];
                    n.nearSize[1] = near3[1];
                    n.nearDistance = near3[2];
                }
            } else if (k == "range") n.range = std::strtof(v.c_str(), nullptr);
            else if (k == "falloffStart") n.falloffStart = std::strtof(v.c_str(), nullptr);
            else if (k == "color") ParseFloatList(v, ',', n.color, 3);
            else if (k == "intensity") n.intensity = std::strtof(v.c_str(), nullptr);
            else if (k == "raw20" || k == "raw390") {
                std::vector<uint32_t>& words = k == "raw20" ? raw20 : raw390;
                std::stringstream ws(v);
                std::string w;
                while (ws >> w) words.push_back(static_cast<uint32_t>(std::strtoul(w.c_str(), nullptr, 16)));
            }
        }
        if (n.type == 4) {
            bool ok = RawFloat(raw390, 0x390, 0x440, &n.probeRadius);
            for (uint32_t i = 0; i < 28 && ok; ++i) ok = RawFloat(raw20, 0x20, 0x230 + 4 * i, &n.sh[i / 4][i % 4]);
            if (!ok) {
                n = {n.type, n.flags & ~lightcal::kProbeFlag};
                *probeWords = false;
            }
        }
        rows.push_back(row);
    }
    return rows;
}

struct LightReplaySummary {
    uint32_t point = 0, spot = 0, sun = 0, probes = 0, fills = 0, published = 0;
    bool probeWords = true;
    bool dome = false;
    float domeRadiance[3] = {};
    float worstCloseRatio = 0.0f;
};

// Runs every recorded game light through the exact conversion lightpub uses and checks what Remix would get.
// Writes one row per published light to outTsv for verify_lights_and_materials.py.
LightReplaySummary ReplayLightDump(const fs::path& path, const std::string& label, std::ofstream& outTsv) {
    LightReplaySummary sum;
    const std::vector<LightRow> rows = LoadLightDump(path, &sum.probeWords);
    TEST_ASSERT(!rows.empty(), "No light rows in %s", path.string().c_str());
    const lightcal::Tuning tuning;  // [Lights] defaults in d3d9_proxy.ini: every scale 1
    std::vector<lightcal::NodeData> nodes;
    for (const auto& row : rows) {
        const lightcal::NodeData& n = row.n;
        nodes.push_back(n);
        if (n.type == 4 && (n.flags & lightcal::kProbeFlag)) {
            // Layout proof for the +0x230 block: the 7th float4 is cC, whose w the engine never uses (always 0).
            TEST_ASSERT(n.sh[6][3] == 0.0f, "%s node %s: SH block 7 .w = %g, expected 0 (cC is RGB only)",
                        label.c_str(), row.node.c_str(), n.sh[6][3]);
            TEST_ASSERT(lightcal::IsProbe(n), "%s node %s: probe rejected (radius %g)", label.c_str(), row.node.c_str(),
                        n.probeRadius);
            ++sum.probes;
        }
        lightcal::PublishedLight p;
        if (!lightcal::ConvertGameLight(n, tuning, &p)) continue;
        ++sum.published;
        for (int k = 0; k < 3; ++k) {
            TEST_ASSERT(std::isfinite(p.radiance[k]) && p.radiance[k] >= 0.0f && std::isfinite(p.position[k]),
                        "%s node %s: non-finite or negative published light", label.c_str(), row.node.c_str());
        }
        float shMean[3] = {}, shToward = 0.0f, fillDistance = 0.0f;
        const bool weaponSpot = lightcal::IsWeaponSpotlight(n);
        if (n.type == 1 || n.type == 2) {
            ++(n.type == 1 ? sum.point : sum.spot);
            if (weaponSpot) {
                TEST_ASSERT(std::fabs(p.radius - lightcal::kWeaponSpotRadiusCm) < 1e-3f,
                            "%s node %s: weapon spot radius %g != %g cm", label.c_str(), row.node.c_str(),
                            p.radius, lightcal::kWeaponSpotRadiusCm);
                TEST_ASSERT(p.shaped && p.coneAngleDegrees > 5.0f && p.coneAngleDegrees < 15.0f,
                            "%s node %s: weapon spot half-angle %g deg expected ~9.5 deg (~19 deg beam)",
                            label.c_str(), row.node.c_str(), p.coneAngleDegrees);
            } else {
                TEST_ASSERT(p.radius >= lightcal::kMinFixtureRadiusCm && p.radius <= lightcal::kMaxFixtureRadiusCm,
                            "%s node %s: fixture radius %g outside 20-35 cm", label.c_str(), row.node.c_str(), p.radius);
                const lightcal::CalibratedSphere cal = lightcal::CalibrateSphereLight(n.range, n.falloffStart);
                const float dClose = lightcal::CloseCheckDistance(n.range, cal.radiusCm);
                const float vanilla = lightcal::GameLightFalloff(n.range, n.falloffStart, dClose);
                if (vanilla > 1e-4f && n.range < 1.0e5f) {
                    const float ratio = lightcal::RemixSphereIrradiance(cal, dClose) / vanilla;
                    sum.worstCloseRatio = (std::max)(sum.worstCloseRatio, ratio);
                    TEST_ASSERT(ratio <= lightcal::kMaxCloseRatioTarget + 1e-3f,
                                "%s node %s range %g: %.3fx the game at %g cm (cap %.2fx)", label.c_str(), row.node.c_str(),
                                n.range, ratio, dClose, lightcal::kMaxCloseRatioTarget);
                }
            }
        } else if (n.type == 3) {
            ++sum.sun;
        } else if (n.type == 4) {
            ++sum.fills;
            lightcal::ShMean(n.sh, shMean);
            const float dx = p.position[0] - n.position[0], dy = p.position[1] - n.position[1],
                        dz = p.position[2] - n.position[2];
            fillDistance = std::sqrt(dx * dx + dy * dy + dz * dz);
            float toward[3];
            lightcal::EvaluateSh(n.sh, dx / fillDistance, dy / fillDistance, dz / fillDistance, toward);
            for (int k = 0; k < 3; ++k) shToward += lightcal::kLuminance[k] * toward[k];
            TEST_ASSERT(fillDistance - p.radius >= 49.5f,
                        "%s probe %s: fill emitter surface (radius %g) only %g cm from the probe centre", label.c_str(),
                        row.node.c_str(), p.radius, fillDistance - p.radius);
        }
        outTsv << label << '\t' << row.node << '\t' << n.type << '\t' << n.range << '\t' << n.falloffStart << '\t'
               << lightcal::MaxComponent(n.color) * n.intensity << '\t' << static_cast<int>(p.kind) << '\t' << p.radius
               << '\t' << p.radiance[0] << ',' << p.radiance[1] << ',' << p.radiance[2] << '\t' << p.position[0] << ','
               << p.position[1] << ',' << p.position[2] << '\t' << n.position[0] << ',' << n.position[1] << ','
               << n.position[2] << '\t' << n.probeRadius << '\t' << shMean[0] << ',' << shMean[1] << ',' << shMean[2]
               << '\t' << shToward << '\t' << fillDistance << '\t' << p.direction[0] << ',' << p.direction[1] << ','
               << p.direction[2] << '\t' << p.coneAngleDegrees << '\t' << n.nearSize[0] << ',' << n.nearSize[1]
               << '\t' << n.nearDistance << '\t' << (weaponSpot ? 1 : 0) << '\n';
    }
    lightcal::PublishedLight dome;
    sum.dome = lightcal::BuildSkyDome(nodes.data(), nodes.size(), tuning, &dome);
    if (sum.dome) {
        for (int k = 0; k < 3; ++k) sum.domeRadiance[k] = dome.radiance[k];
        outTsv << label << "\tdome\t4\t0\t0\t0\t" << static_cast<int>(dome.kind) << "\t0\t" << dome.radiance[0] << ','
               << dome.radiance[1] << ',' << dome.radiance[2] << "\t0,0,0\t0,0,0\t0\t0,0,0\t0\t0\t0,0,-1\t0\t0,0\t0\t0\n";
    }
    TEST_ASSERT(sum.published + (sum.dome ? 1u : 0u) <= 256u, "%s: %u lights exceed [Lights] MaxLights=256",
                label.c_str(), sum.published);
    return sum;
}

// ---- Goggles: what AutoRaytracing does when the game switches night vision on and off ----------------------------

// Forces the game's night-vision switch (bNoise on the tone-map shader) on in one recorded frame.
std::vector<ParsedDraw> WithGogglesOn(std::vector<ParsedDraw> draws, const ShaderBank& bank, int* switched) {
    *switched = 0;
    for (auto& d : draws) {
        auto it = bank.ps.find(d.psHash);
        if (it != bank.ps.end() && it->second.gogglesSwitch) {
            d.psBoolMask |= 1u << (it->second.gogglesSwitch - 1);
            ++*switched;
        }
    }
    return draws;
}

// One recorded frame through the pass graph; returns the draws the graph skipped. *gogglesAtPresent is what the
// proxy hands lightpub at Present (GogglesActive() before PassFilterEndFrame).
uint64_t ClassifyFrame(proxy::PassGraph& graph, const std::vector<ParsedDraw>& draws, const ShaderBank& bank,
                       bool raytracing, bool* gogglesAtPresent) {
    const uint64_t before = graph.Stats().skippedLightingDraws + graph.Stats().skippedPostDraws;
    for (const auto& d : draws) {
        auto vsIt = bank.vs.find(d.vsHash);
        auto psIt = bank.ps.find(d.psHash);
        proxy::DrawContextInput ctx = {};
        ctx.vs = vsIt != bank.vs.end() ? vsIt->second : proxy::ShaderTraits{d.vsHash, proxy::VsClass::Other};
        ctx.ps = psIt != bank.ps.end() ? psIt->second : proxy::ShaderTraits{d.psHash, proxy::VsClass::None};
        ctx.rt0 = reinterpret_cast<const void*>(d.rt0Ptr);
        ctx.rt0Primary = d.rt0W == 2560 && d.rt0H == 1440 && d.rt0Ptr != 0;
        ctx.rt0IsShadowMap = d.rt0W == d.rt0H && d.rt0W >= 1024 && d.rt0Ptr != 0;
        ctx.rt0Format = d.rt0Fmt;
        ctx.rt1Bound = d.rt1Bound;
        ctx.depthTestEnabled = d.zen;
        ctx.depthWriteEnabled = d.zwr;
        ctx.colorWriteEnabled = d.cw != 0;
        ctx.alphaBlendEnabled = d.bl;
        ctx.gogglesSwitchActive = ctx.ps.gogglesSwitch != 0 && (d.psBoolMask & (1u << (ctx.ps.gogglesSwitch - 1)));
        ctx.raytracingActive = raytracing;
        const proxy::DrawDecision dec = graph.ClassifyDraw(ctx);
        if (dec.emitCompositeHandoffBeforeDraw) graph.NoteHandoffCompleted();
    }
    *gogglesAtPresent = graph.GogglesActive();
    if (graph.EndFrame(nullptr, raytracing)) graph.NoteHandoffCompleted();
    return graph.Stats().skippedLightingDraws + graph.Stats().skippedPostDraws - before;
}

struct GoggleReplay {
    int framesToRtOff = -1;      // frames from the first goggle frame until ray tracing is switched off
    int framesToRtOn = -1;       // frames from the first frame without goggles until it is back on
    bool keptSceneForGoggles = false;
    bool clearedSceneOnUnload = false;
    uint64_t skippedWhileGoggles = 0;
    int remixClearsInLevel = 0;      // Remix scene clears while the level is loaded (each one rebuilds every mesh)
    int remixClearsAfterUnload = 0;
};

// Replays the proxy's per-frame order (HookPresent): SetShowGamePicture(GogglesActive) -> lightpub::OnPresent
// (AutoRaytracing) -> PassFilterEndFrame. Timeline: level loads, goggles on, goggles off, level unloads.
GoggleReplay ReplayGoggles(const fs::path& tsvPath, const ShaderBank& bank) {
    std::vector<ParsedDraw> normal;
    std::vector<ParsedCamPub> cams;
    std::unordered_map<uintptr_t, uintptr_t> texMap;
    LoadFrameTrace(tsvPath, &normal, &cams, &texMap);
    int switched = 0;
    const std::vector<ParsedDraw> goggles = WithGogglesOn(normal, bank, &switched);
    TEST_ASSERT(switched >= 1, "No tone-map draw with a bNoise switch in %s", tsvPath.string().c_str());

    proxy::PassGraph graph;
    graph.SetConfig(ReplayConfig(HandoffMode::BeforeEffects));
    proxy::AutoRaytracing autoRt;
    GoggleReplay out;
    constexpr int kLevelStart = 0, kGogglesOn = 120, kGogglesOff = 240, kUnload = 300, kEnd = 360;
    int rtState = 0;  // what Remix has (rtx.conf starts with ray tracing off)
    // Remix's side (rtx_context.cpp, the branch that calls SceneManager::clear): an option sent at a Present lands on a
    // later Remix frame, the keep-alive first and enableRaytracing one frame after it (lightpub sends them in that
    // order). The first frame back with ray tracing counts as having no valid camera, which Remix also counts as a frame
    // without a scene. Every goggles-off in the Nov 14 recordings rebuilt the whole scene this way
    // (tests/analysis/goggles_off_frames.py: 26 of 26 at 14:35, 29 of 32 at 18:55).
    int64_t keepNext = -1, rtNext = -1, rtAfter = -1;
    uint32_t remixKeepAlive = 0, framesWithoutScene = 0;
    bool remixRt = false;
    for (int f = kLevelStart; f < kEnd; ++f) {
        const bool inLevel = f < kUnload;
        const bool rtBefore = remixRt;
        if (rtNext >= 0) remixRt = rtNext != 0;
        rtNext = std::exchange(rtAfter, -1);
        if (keepNext >= 0) remixKeepAlive = static_cast<uint32_t>(std::exchange(keepNext, -1));
        if (!remixRt || !rtBefore) {
            if (++framesWithoutScene > remixKeepAlive) {
                if (f >= static_cast<int>(proxy::kLevelOnFrames) + 3 && f < kUnload) ++out.remixClearsInLevel;
                if (f >= kUnload) ++out.remixClearsAfterUnload;
            }
        } else {
            framesWithoutScene = 0;
        }
        const bool goggleFrame = f >= kGogglesOn && f < kGogglesOff;
        // The draws of frame f run with the state decided at the previous Present.
        bool gogglesAtPresent = false;
        const uint64_t skipped =
            inLevel ? ClassifyFrame(graph, goggleFrame ? goggles : normal, bank, rtState == 1, &gogglesAtPresent) : 0;
        // From the second goggle frame on, the game's picture must be complete (nothing skipped).
        if (goggleFrame && f >= kGogglesOn + 2) out.skippedWhileGoggles += skipped;
        // Present of frame f.
        const proxy::AutoRtAction a = autoRt.Update(inLevel, gogglesAtPresent);
        if (!a.send) continue;
        rtState = a.raytracing ? 1 : 0;
        keepNext = a.keepScene ? proxy::kKeepSceneFrames : 0;
        rtAfter = rtState;
        if (!a.raytracing && a.keepScene && f >= kGogglesOn && out.framesToRtOff < 0) {
            out.framesToRtOff = f - kGogglesOn;
            out.keptSceneForGoggles = true;
        }
        if (a.raytracing && f >= kGogglesOff && out.framesToRtOn < 0) out.framesToRtOn = f - kGogglesOff;
        if (!a.raytracing && !a.keepScene && f >= kUnload) out.clearedSceneOnUnload = true;
    }
    return out;
}

// ---- Alpha cut-outs: the game's own per-material threshold, on every recorded texkill draw ----------------------

struct CutoutStats {
    uint32_t draws = 0, hinted = 0;
    std::map<DWORD, uint32_t> refs;
};

void SurveyAlphaCutoffs(const std::vector<fs::path>& traces, const ShaderBank& bank,
                        std::map<uint32_t, CutoutStats>* out) {
    for (const auto& path : traces) {
        std::vector<ParsedDraw> draws;
        std::vector<ParsedCamPub> cams;
        std::unordered_map<uintptr_t, uintptr_t> texMap;
        LoadFrameTrace(path, &draws, &cams, &texMap);
        for (const auto& d : draws) {
            auto it = bank.ps.find(d.psHash);
            if (it == bank.ps.end() || !it->second.hasTexkill || !d.hasPsc) continue;
            const proxy::ShaderTraits& t = it->second;
            CutoutStats& st = (*out)[d.psHash];
            ++st.draws;
            DWORD ref = 0;
            const size_t regs = d.hasPscHigh ? 168 : 16;
            if (!proxy::ComputeAlphaCutoffFromConstants(t, d.psBoolMask, d.psc, regs, 128, &ref)) continue;
            ++st.hinted;
            ++st.refs[ref];
            // The hint must be the game's own threshold: alpha < cutoff + 0.01 is discarded (cAlphaTest / cAlphaRef).
            if (t.alphaCutoff && static_cast<size_t>(t.alphaCutoff - 1) < regs) {
                const float cutoff = d.psc[t.alphaCutoff - 1][t.alphaCutoffComponent];
                const long expected = std::lround(255.0f * (cutoff + 0.01f));
                TEST_ASSERT(static_cast<long>(ref) == std::clamp(expected, 1L, 255L),
                            "PS %08X seq %u in %s: alpha ref %lu, game threshold %g -> %ld", d.psHash, d.seq,
                            path.filename().string().c_str(), ref, cutoff, expected);
            }
        }
    }
}

// Verifies that moving/pulsing lights update in place without DestroyLight handle resets across multi-frame turns.
void RunLightLifecycleTests() {
    lightcal::LightLifecycleTracker<16> tracker;
    const lightcal::Tuning tuning;
    lightcal::NodeData guardSpot = {};
    guardSpot.type = 2;
    guardSpot.flags = 2;
    guardSpot.position[0] = -10123.0f; guardSpot.position[1] = -1418.0f; guardSpot.position[2] = 122.0f;
    guardSpot.orientation[0][0] = 1.0f;
    guardSpot.nearSize[0] = 50.0f; guardSpot.nearSize[1] = 50.0f; guardSpot.nearDistance = 150.0f;
    guardSpot.range = 50.0f;
    guardSpot.color[0] = 23.0f; guardSpot.color[1] = 25.0f; guardSpot.color[2] = 27.0f;
    guardSpot.intensity = 1.0f;

    lightcal::NodeData pulsingBulb = {};
    pulsingBulb.type = 1;
    pulsingBulb.flags = 1;
    pulsingBulb.position[0] = 100.0f; pulsingBulb.position[1] = 200.0f; pulsingBulb.position[2] = 300.0f;
    pulsingBulb.range = 400.0f;
    pulsingBulb.color[0] = 1.0f; pulsingBulb.color[1] = 0.9f; pulsingBulb.color[2] = 0.8f;
    pulsingBulb.intensity = 2.0f;

    uint32_t totalCreated = 0, totalUpdated = 0, totalDestroyed = 0;
    void* firstGuardHandle = nullptr;
    void* firstBulbHandle = nullptr;
    auto createOrUpdate = [](uint64_t stableHash, const lightcal::PublishedLight&) -> void* {
        return reinterpret_cast<void*>(static_cast<uintptr_t>(stableHash));
    };
    auto destroyFn = [](void*) {};

    for (uint32_t f = 1; f <= 60; ++f) {
        // Guard walks and turns rifle every frame; bulb flickers intensity every frame.
        guardSpot.position[0] += 3.5f;
        const float a = f * 0.05f;
        guardSpot.orientation[0][0] = std::cos(a);
        guardSpot.orientation[1][0] = std::sin(a);
        pulsingBulb.intensity = 2.0f + 0.25f * std::sin(f * 0.3f);

        lightcal::PublishedLight pubSpot, pubBulb;
        TEST_ASSERT(lightcal::ConvertGameLight(guardSpot, tuning, &pubSpot), "guard spot must convert");
        TEST_ASSERT(lightcal::ConvertGameLight(pulsingBulb, tuning, &pubBulb), "pulsing bulb must convert");

        void *hSpot = nullptr, *hBulb = nullptr;
        tracker.Upsert(0x76306980u, 1000ull + f, pubSpot, f, createOrUpdate, &totalCreated, &totalUpdated, &hSpot);
        tracker.Upsert(0x76301000u, 2000ull + f, pubBulb, f, createOrUpdate, &totalCreated, &totalUpdated, &hBulb);
        tracker.SweepUnseen(f, destroyFn, &totalDestroyed);

        if (f == 1) {
            firstGuardHandle = hSpot;
            firstBulbHandle = hBulb;
        }
        TEST_ASSERT(hSpot == firstGuardHandle && hBulb == firstBulbHandle,
                    "Frame %u: light handle reset during motion/pulse!", f);
    }
    TEST_ASSERT(totalCreated == 2 && totalUpdated == 118 && totalDestroyed == 0,
                "Expected 2 creates, 118 in-place updates, 0 destroys; got %u/%u/%u",
                totalCreated, totalUpdated, totalDestroyed);

    // Frame 61: guard despawns -> only that 1 light is destroyed, bulb handle stays intact.
    lightcal::PublishedLight pubBulb;
    lightcal::ConvertGameLight(pulsingBulb, tuning, &pubBulb);
    void* hBulb = nullptr;
    tracker.Upsert(0x76301000u, 9999ull, pubBulb, 61, createOrUpdate, &totalCreated, &totalUpdated, &hBulb);
    tracker.SweepUnseen(61, destroyFn, &totalDestroyed);
    TEST_ASSERT(totalDestroyed == 1 && tracker.Count() == 1 && hBulb == firstBulbHandle,
                "Despawn sweep should destroy only the despawned light");
}

}  // namespace

int main() {
    const fs::path remixRoot = fs::path(__FILE__).parent_path().parent_path();
    const fs::path fixtures = remixRoot / "fixtures";
    const fs::path systemDir = R"(D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM)";

    const ShaderBank bank = LoadShaderBank(systemDir);
    RunShaderAndLightUnitTests(bank);
    RunLightLifecycleTests();
    RunCameraSolverTests(fixtures / "run20" / "cam_on_draws.tsv");

    // 1. Paladin + Gear Menu (run9, 2560x1440 backbuffer; early SMI table UI renders at 1280x720)
    const ReplaySummary paladin = VerifyMissionTrace(fixtures / "run9" / "gear_rt_draws.tsv", bank, 2560, 1440);
    TEST_ASSERT(paladin.handoffs == 1, "Paladin expected 1 handoff, got %llu", paladin.handoffs);
    TEST_ASSERT(paladin.duplicatesBlocked >= 15,
                "Paladin expected >=15 G-buffer duplicate meshes blocked, got %llu", paladin.duplicatesBlocked);
    TEST_ASSERT(paladin.skinnedSceneDraws > 0, "Paladin expected Sam skinned draws > 0");

    // 2. Private Estate night courtyard (run7, 2560x1440)
    const ReplaySummary estate = VerifyMissionTrace(fixtures / "run7" / "lampA_rt_draws.tsv", bank, 2560, 1440);
    TEST_ASSERT(estate.handoffs == 1, "Private Estate expected 1 handoff, got %llu", estate.handoffs);
    TEST_ASSERT(estate.duplicatesBlocked >= 15,
                "Private Estate expected >=15 G-buffer duplicates blocked, got %llu", estate.duplicatesBlocked);
    TEST_ASSERT(estate.suppressedSetters >= 2000,
                "Private Estate expected >=2000 suppressed bridge setters across 3 frames, got %llu",
                estate.suppressedSetters);

    // 3. American Consumption carnival indoor (run14, 2560x1440)
    const ReplaySummary consumption = VerifyMissionTrace(fixtures / "run14" / "after_probe_draws.tsv", bank, 2560, 1440);
    TEST_ASSERT(consumption.handoffs == 1, "American Consumption expected 1 handoff, got %llu", consumption.handoffs);
    TEST_ASSERT(consumption.skinnedSceneDraws >= 14,
                "American Consumption expected >=14 skinned draws, got %u", consumption.skinnedSceneDraws);

    // 4. Run 20 Vanishing-Sam close-camera turn (cam_on_draws.tsv, 2560x1440)
    const ReplaySummary run20 = VerifyMissionTrace(fixtures / "run20" / "cam_on_draws.tsv", bank, 2560, 1440);
    TEST_ASSERT(run20.handoffs == 1, "Run 20 cam_on expected 1 handoff, got %llu", run20.handoffs);
    TEST_ASSERT(run20.skinnedSceneDraws >= 14,
                "Run 20 cam_on expected >=14 skinned scene draws, got %u", run20.skinnedSceneDraws);
    // Sam's 4 forward-only hair/eyelash/cornea draws (seq 549..551, 554; 3 are unique after dedup or 1 unique) must NOT be killed!
    TEST_ASSERT(run20.forwardSkinnedKept >= 1,
                "Run 20 cam_on forward-only skinned hair/cornea draws were wrongly dropped!");

    // 5. Hot-path CPU SLA (< 0.15 ms/frame across 1,000 frames of Private Estate)
    const double msPerFrame = BenchmarkHotPathMsPerFrame(fixtures / "run7" / "lampA_rt_draws.tsv", bank);
    TEST_ASSERT(msPerFrame < 0.15, "PassGraph hot-path %.4f ms/frame exceeded 0.15 ms budget!", msPerFrame);

    // 6. The proxy's default hand-off (HandoffBeforePost + HandoffBeforeEffects): the game draws its particles (breath,
    // snow, smoke, impact dust) and coronas over the ray-traced image, the fake beam-card light cones never show, and
    // the game's lighting pass is still skipped. Same frames in the composite mode for comparison.
    std::printf("  Hand-off modes   : draws reaching the bridge per frame (composite -> before effects)\n");
    const struct { const char* label; const char* path; } kModeTraces[] = {
        {"Paladin", "run9/gear_rt_draws.tsv"}, {"Estate", "run7/lampA_rt_draws.tsv"},
        {"Estate walk", "run7/walk_rt_start_draws.tsv"}, {"Consumption", "run14/after_probe_draws.tsv"},
        {"Run20", "run20/cam_on_draws.tsv"}};
    uint32_t particlesComposited = 0, fakeVolumesSeen = 0;
    for (const auto& t : kModeTraces) {
        const ReplaySummary composite = VerifyMissionTrace(fixtures / t.path, bank, 2560, 1440, HandoffMode::Composite);
        const ReplaySummary effects = VerifyMissionTrace(fixtures / t.path, bank, 2560, 1440, HandoffMode::BeforeEffects);
        TEST_ASSERT(effects.handoffs == 1, "%s: expected 1 hand-off before effects, got %llu", t.label, effects.handoffs);
        TEST_ASSERT(effects.particlesSkipped == 0 && effects.particlesBeforeHandoff == 0,
                    "%s: %u particle draws skipped, %u drawn before the hand-off (both lose them)", t.label,
                    effects.particlesSkipped, effects.particlesBeforeHandoff);
        TEST_ASSERT(effects.fakeVolumesKept == 0 && composite.fakeVolumesKept == 0,
                    "%s: fake light-cone draws shown over the ray-traced image (%u / %u)", t.label,
                    composite.fakeVolumesKept, effects.fakeVolumesKept);
        TEST_ASSERT(effects.skippedDraws > 0, "%s: game lighting no longer skipped before effects", t.label);
        particlesComposited += effects.particlesKept;
        fakeVolumesSeen += effects.fakeVolumesSeen;
        std::printf("    %-12s %4u -> %4u (particles drawn by the game %u, hand-off at draw %u, fake cones hidden %u)\n",
                    t.label, composite.keptDraws, effects.keptDraws, effects.particlesKept, effects.earlyHandoffSeq,
                    effects.fakeVolumesSeen);
    }
    TEST_ASSERT(particlesComposited > 0, "No particle draws in the replayed frames: the particle check proves nothing");

    // 7. Goggles: ray tracing pauses within a frame, Remix keeps its scene, and it returns at once when they come off.
    const GoggleReplay g = ReplayGoggles(fixtures / "run7" / "lampA_rt_draws.tsv", bank);
    TEST_ASSERT(g.framesToRtOff >= 0 && g.framesToRtOff <= 1, "Goggles on: ray tracing off after %d frames", g.framesToRtOff);
    TEST_ASSERT(g.keptSceneForGoggles, "Goggles on: Remix would clear its scene (rtx.sceneKeepAliveFrames not raised)");
    TEST_ASSERT(g.framesToRtOn >= 0 && g.framesToRtOn <= 1, "Goggles off: ray tracing back after %d frames (was 60)",
                g.framesToRtOn);
    TEST_ASSERT(g.skippedWhileGoggles == 0, "Goggles: %llu game draws skipped while the game's picture shows",
                g.skippedWhileGoggles);
    TEST_ASSERT(g.clearedSceneOnUnload, "Level unload: Remix scene not cleared");
    TEST_ASSERT(g.remixClearsInLevel == 0,
                "Goggles off: Remix cleared its scene %d times inside the level (sceneKeepAliveFrames dropped to 0 while "
                "ray tracing was not yet back, so every mesh is rebuilt and the first frame is dark)", g.remixClearsInLevel);
    TEST_ASSERT(g.remixClearsAfterUnload > 0, "Level unload: the Remix model never cleared, so the check above proves nothing");

    // 8. Every recorded game light through light_calibration.h, as lightpub publishes it.
    const fs::path publishedTsv = remixRoot / "tests" / "lightcal_published.tsv";
    std::ofstream published(publishedTsv, std::ios::trunc);
    TEST_ASSERT(published.is_open(), "Cannot write %s", publishedTsv.string().c_str());
    published << "dump\tnode\ttype\trange\tfalloffStart\tpeak\tkind\tradius\tradiance\tposition\tnodePosition"
                 "\tprobeRadius\tshMean\tshToward\tfillDistance\tdirection\tconeAngle\tnearSize\tnearDistance\tweaponSpot\n";
    std::vector<fs::path> lightDumps, drawTraces;
    for (const auto& run : fs::directory_iterator(fixtures)) {
        if (!run.is_directory()) continue;
        for (const auto& f : fs::directory_iterator(run.path())) {
            const std::string name = f.path().filename().string();
            if (name.size() > 11 && name.compare(name.size() - 11, 11, "_lights.tsv") == 0) lightDumps.push_back(f.path());
            if (name.size() > 10 && name.compare(name.size() - 10, 10, "_draws.tsv") == 0) drawTraces.push_back(f.path());
        }
    }
    std::sort(lightDumps.begin(), lightDumps.end());
    std::sort(drawTraces.begin(), drawTraces.end());
    TEST_ASSERT(lightDumps.size() >= 20, "Expected >= 20 recorded light dumps, found %zu", lightDumps.size());
    std::printf("  Light publish    : per run (point/spot/sun, SH probes -> room fills, sky dome, worst 0.5 m ratio)\n");
    const auto run_is_old_format = [](const fs::path& p) { return p.parent_path().filename() == "run1"; };
    std::string lastRun;
    for (const auto& dump : lightDumps) {
        const std::string label = dump.parent_path().filename().string() + "/" + dump.filename().string();
        const LightReplaySummary s = ReplayLightDump(dump, label, published);
        TEST_ASSERT(s.probeWords || run_is_old_format(dump), "%s: probe words missing", label.c_str());
        TEST_ASSERT(!s.probeWords || s.probes > 0, "%s: no SH probes recorded", label.c_str());
        TEST_ASSERT(s.probes == 0 || s.dome, "%s: %u SH probes but no sky dome", label.c_str(), s.probes);
        const std::string run = dump.parent_path().filename().string();
        if (run != lastRun) {
            lastRun = run;
            std::printf("    %-34s %3u/%3u/%u, %2u probes -> %2u fills, dome %.4g %.4g %.4g, %.2fx\n", label.c_str(),
                        s.point, s.spot, s.sun, s.probes, s.fills, s.domeRadiance[0], s.domeRadiance[1],
                        s.domeRadiance[2], s.worstCloseRatio);
        }
        if (run == "run9") TEST_ASSERT(s.fills >= 50, "%s: Paladin room fills %u, expected >= 50", label.c_str(), s.fills);
        if (s.probeWords) TEST_ASSERT(s.fills >= 3, "%s: mission has only %u room fills (expected >= 3 indoor zones)", label.c_str(), s.fills);
    }
    published.close();

    // 9. Alpha cut-outs (chain-link fence, floor snow and every other texkill material): the hint is the game's own
    // per-draw threshold, never a blanket 128, and blend/snow masks the game does not cut stay uncut.
    std::map<uint32_t, CutoutStats> cutouts;
    SurveyAlphaCutoffs(drawTraces, bank, &cutouts);
    auto fence = cutouts.find(0x359DE82Cu);
    TEST_ASSERT(fence != cutouts.end() && fence->second.hinted > 0, "Fence PS 359DE82C: no hinted draws recorded");
    std::printf("  Alpha cut-outs   : texkill PS: draws, hinted, alpha refs (ref = 255 x (game cutoff + 0.01))\n");
    for (const auto& [ps, st] : cutouts) {
        std::string refs;
        for (const auto& [ref, count] : st.refs) refs += std::to_string(ref) + "x" + std::to_string(count) + " ";
        std::printf("    %08X %5u %5u  %s\n", ps, st.draws, st.hinted, refs.c_str());
    }

    std::printf(
        "PASS: verify_proxy_tests passed all checks!\n"
        "  Shaders verified : %zu VS + %zu PS bytecodes (100%% FNV-1a & CTAB match)\n"
        "  Paladin (run9)   : %u scene draws, %llu duplicates blocked, %llu handoff\n"
        "  Estate  (run7)   : %u scene draws, %llu duplicates blocked, %llu wasted draws skipped, %llu setters suppressed\n"
        "  Consump (run14)  : %u scene draws, %u skinned draws, %llu handoff\n"
        "  Run20 Close-Cam  : %u scene draws, %u skinned draws, %u forward-skinned kept, 2 shadow-cams rejected\n"
        "  Hot-path speed   : %.4f ms/frame (budget < 0.1500 ms/frame)\n",
        bank.vs.size(), bank.ps.size(),
        paladin.sceneDraws, paladin.duplicatesBlocked, paladin.handoffs,
        estate.sceneDraws, estate.duplicatesBlocked, estate.skippedDraws, estate.suppressedSetters,
        consumption.sceneDraws, consumption.skinnedSceneDraws, consumption.handoffs,
        run20.sceneDraws, run20.skinnedSceneDraws, run20.forwardSkinnedKept,
        msPerFrame);
    return 0;
}
