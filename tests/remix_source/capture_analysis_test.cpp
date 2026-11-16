// Tests for src/dxso/dxso_capture_analysis.cpp (vertex capture analysis).
//
// Part 1 runs hand-built vs_3_0 token streams with known answers.
// Part 2 runs every vertex shader in the game's data/Shaders/d3d9/VertexShaders.bin and
// checks the properties the Remix changes rely on:
//   - the geometry hash never includes camera-only constants, and always includes the
//     constants that move a mesh (object transform, bones);
//   - every world shader with an input normal has a detected normal output, built from
//     the input normal with world-space (non-camera) constants only.
// Exit code 0 = pass. With --dump <file>, writes one JSON line per game shader.

#define _CRT_SECURE_NO_WARNINGS
// Standard headers first, so the access override below only reaches the analyzer's own class.
#include <algorithm>
#include <array>
#include <bitset>
#include <cstring>
#include <iterator>
#include <string>
#include <tuple>
#include <utility>
#include <vector>
#define private public
#include "../../src/dxvk-remix/src/dxso/dxso_capture_analysis.cpp"
#undef private

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <map>
#include <set>
#include <string>
#include <vector>

using namespace dxvk;

static int g_failures = 0;
#define CHECK(cond, ...) do { if (!(cond)) { ++g_failures; std::printf("FAIL %s:%d: ", __FILE__, __LINE__); std::printf(__VA_ARGS__); std::printf("\n"); } } while (0)

// --- tiny vs_3_0 assembler -------------------------------------------------------------
namespace as {
  constexpr uint32_t T = 0, V = 1, C = 2, O = 6, B = 14;
  uint32_t regTok(uint32_t type, uint32_t num) { return 0x80000000u | ((type & 7) << 28) | ((type & 0x18) << 8) | num; }
  uint32_t dst(uint32_t type, uint32_t num, uint32_t mask = 0xF) { return regTok(type, num) | (mask << 16); }
  uint32_t src(uint32_t type, uint32_t num, uint32_t swz = 0xE4) { return regTok(type, num) | (swz << 16); }
  uint32_t ins(uint32_t opcode, uint32_t length) { return opcode | (length << 24); }
  void dcl(std::vector<uint32_t>& c, uint32_t usage, uint32_t index, uint32_t d) {
    c.push_back(ins(31, 2)); c.push_back(0x80000000u | usage | (index << 16)); c.push_back(d);
  }
  void def(std::vector<uint32_t>& c, uint32_t r, float x, float y, float z, float w) {
    c.push_back(ins(81, 5)); c.push_back(dst(C, r));
    for (float f : { x, y, z, w }) { uint32_t u; std::memcpy(&u, &f, 4); c.push_back(u); }
  }
  void emit(std::vector<uint32_t>& c, uint32_t opcode, uint32_t d, std::initializer_list<uint32_t> srcs) {
    c.push_back(ins(opcode, uint32_t(1 + srcs.size()))); c.push_back(d);
    for (uint32_t s : srcs) c.push_back(s);
  }
  // CTAB comment: entries are (name, first float register, count).
  void ctab(std::vector<uint32_t>& c, const std::vector<std::tuple<std::string, int, int>>& entries) {
    std::vector<uint8_t> blob(28 + 20 * entries.size(), 0);
    auto put32 = [&](size_t at, uint32_t v) { std::memcpy(&blob[at], &v, 4); };
    put32(0, 28); put32(12, uint32_t(entries.size())); put32(16, 28);
    for (size_t k = 0; k < entries.size(); k++) {
      const size_t e = 28 + 20 * k;
      put32(e, uint32_t(blob.size()));
      const uint16_t set = 2;
      std::memcpy(&blob[e + 4], &set, 2);
      const uint16_t first = uint16_t(std::get<1>(entries[k])), count = uint16_t(std::get<2>(entries[k]));
      std::memcpy(&blob[e + 6], &first, 2);
      std::memcpy(&blob[e + 8], &count, 2);
      for (char ch : std::get<0>(entries[k])) blob.push_back(uint8_t(ch));
      blob.push_back(0);
    }
    while (blob.size() % 4) blob.push_back(0);
    c.push_back(0xFFFE | (uint32_t(1 + blob.size() / 4) << 16));
    c.push_back(0x42415443u);
    for (size_t i = 0; i < blob.size(); i += 4) { uint32_t w; std::memcpy(&w, &blob[i], 4); c.push_back(w); }
  }
}

static bool rangesContain(const DxsoCaptureAnalysis& a, uint32_t r) {
  for (auto [first, count] : a.geometryConstantRanges) if (r >= first && r < uint32_t(first) + count) return true;
  return false;
}

// Static mesh like the game's: oPos = gWorldToProj * (gDrawScale * v0); TEXCOORD4 = gLocalToWorld * (2*n - 1);
// TEXCOORD2 = gLocalToWorld * tangent; fog in TEXCOORD5 uses cViewPos_VS and cFog (c12).
static std::vector<uint32_t> staticMeshShader() {
  using namespace as;
  std::vector<uint32_t> c = { 0xFFFE0300u };
  ctab(c, { { "gViewportOffset", 1, 1 }, { "gDrawScale", 4, 1 }, { "gLocalToWorld", 5, 3 }, { "gWorldToProj", 8, 4 },
            { "cFog_Density_Start_Range", 12, 1 }, { "cViewPos_VS", 13, 1 } });
  def(c, 0, 0.0f, 1.0f, 2.0f, -1.0f);
  dcl(c, 0, 0, dst(V, 0)); dcl(c, 3, 0, dst(V, 1)); dcl(c, 6, 0, dst(V, 2));
  dcl(c, 0, 0, dst(O, 0)); dcl(c, 5, 2, dst(O, 2)); dcl(c, 5, 4, dst(O, 4)); dcl(c, 5, 5, dst(O, 5));
  emit(c, 5, dst(T, 0, 0x7), { src(C, 4), src(V, 0) });            // mul r0.xyz, c4, v0
  emit(c, 1, dst(T, 0, 0x8), { src(V, 0, 0xFF) });                 // mov r0.w, v0.w
  emit(c, 9, dst(T, 1, 0x1), { src(T, 0), src(C, 5) });            // dp4 r1.x, r0, c5   (world position)
  emit(c, 9, dst(T, 1, 0x2), { src(T, 0), src(C, 6) });
  emit(c, 9, dst(T, 1, 0x4), { src(T, 0), src(C, 7) });
  for (uint32_t k = 0; k < 4; k++) emit(c, 9, dst(T, 2, 1u << k), { src(T, 0), src(C, 8 + k) }); // r2 = WVP * r0
  emit(c, 4, dst(O, 0, 0x3), { src(C, 1), src(T, 2, 0xFF), src(T, 2) }); // mad o0.xy, c1, r2.w, r2   (jitter)
  emit(c, 1, dst(O, 0, 0xC), { src(T, 2) });
  emit(c, 3, dst(T, 3, 0x7), { src(T, 1), src(C, 13) });           // fog: r3 = worldPos - camera
  emit(c, 8, dst(T, 3, 0x1), { src(T, 3), src(T, 3) });
  emit(c, 5, dst(O, 5, 0xF), { src(T, 3, 0x00), src(C, 12) });
  emit(c, 4, dst(T, 4, 0x7), { src(V, 1), src(C, 0, 0xAA), src(C, 0, 0xFF) }); // n*2-1
  for (uint32_t k = 0; k < 3; k++) emit(c, 8, dst(O, 4, 1u << k), { src(T, 4), src(C, 5 + k) });
  emit(c, 1, dst(O, 4, 0x8), { src(C, 0, 0x00) });
  for (uint32_t k = 0; k < 3; k++) emit(c, 8, dst(O, 2, 1u << k), { src(V, 2), src(C, 5 + k) });
  emit(c, 1, dst(O, 2, 0x8), { src(C, 0, 0x00) });
  c.push_back(0x0000FFFFu);
  return c;
}

static void testSynthetic() {
  const auto code = staticMeshShader();
  const auto a = analyzeVertexShaderForCapture(code.data(), code.size());
  CHECK(a.valid, "synthetic static mesh shader should analyse");
  CHECK(a.hasNormalOutput && a.normalOutputUsage == 5 && a.normalOutputIndex == 4,
        "normal should be TEXCOORD4 (got has=%d usage=%u index=%u)", a.hasNormalOutput, a.normalOutputUsage, a.normalOutputIndex);
  CHECK(a.hasGeometryConstantRanges, "static mesh should get geometry ranges");
  for (uint32_t r : { 4u, 5u, 6u, 7u }) CHECK(rangesContain(a, r), "c%u (scale/local-to-world) must be hashed", r);
  for (uint32_t r : { 1u, 8u, 9u, 10u, 11u, 12u, 13u }) CHECK(!rangesContain(a, r), "c%u (camera/fog) must not be hashed", r);
  CHECK(!rangesContain(a, 0), "c0 is a literal (def) and is already in the bytecode hash");
  CHECK(!a.hasVertexAnimation, "static mesh without wind/wave constants must not set hasVertexAnimation");

  // Without an object-motion constant the transform hides in gWorldToProj: keep the full hash.
  {
    using namespace as;
    std::vector<uint32_t> c = { 0xFFFE0300u };
    ctab(c, { { "gDrawScale", 4, 1 }, { "gWorldToProj", 8, 4 } });
    dcl(c, 0, 0, dst(V, 0)); dcl(c, 0, 0, dst(O, 0));
    emit(c, 5, dst(T, 0, 0xF), { src(C, 4), src(V, 0) });
    for (uint32_t k = 0; k < 4; k++) emit(c, 9, dst(O, 0, 1u << k), { src(T, 0), src(C, 8 + k) });
    c.push_back(0x0000FFFFu);
    const auto b = analyzeVertexShaderForCapture(c.data(), c.size());
    CHECK(b.valid && !b.hasGeometryConstantRanges, "depth-only shader without gLocalToWorld must keep the full hash");
  }

  // A temp reused after the position is written must not leak its later sources into the position.
  {
    using namespace as;
    std::vector<uint32_t> c = { 0xFFFE0300u };
    ctab(c, { { "gLocalToWorld", 5, 3 }, { "gWorldToProj", 8, 4 }, { "cSHParams", 17, 7 } });
    dcl(c, 0, 0, dst(V, 0)); dcl(c, 0, 0, dst(O, 0)); dcl(c, 5, 6, dst(O, 1));
    for (uint32_t k = 0; k < 4; k++) emit(c, 9, dst(O, 0, 1u << k), { src(V, 0), src(C, 8 + k) });
    emit(c, 1, dst(T, 0), { src(C, 17) });                // r0 reused for lighting after oPos
    emit(c, 1, dst(O, 1), { src(T, 0) });
    c.push_back(0x0000FFFFu);
    const auto b = analyzeVertexShaderForCapture(c.data(), c.size());
    CHECK(b.valid && b.hasGeometryConstantRanges && !rangesContain(b, 17), "lighting constant must not be hashed");
    CHECK(rangesContain(b, 5) && rangesContain(b, 7), "gLocalToWorld always hashed when declared");
  }

  // Indexed bone palette: the whole gBones array is hashed; values written inside a branch keep earlier sources.
  {
    using namespace as;
    std::vector<uint32_t> c = { 0xFFFE0300u };
    ctab(c, { { "gWorldToProj", 8, 4 }, { "gBones", 41, 12 } });
    dcl(c, 0, 0, dst(V, 0)); dcl(c, 2, 0, dst(V, 1)); dcl(c, 0, 0, dst(O, 0));
    emit(c, 46, dst(3, 0, 0x1), { src(V, 1) });                                  // mova a0.x, v1
    uint32_t rel = src(C, 41) | (1u << 13);
    c.push_back(as::ins(9, 4)); c.push_back(dst(T, 0, 0x1)); c.push_back(src(V, 0)); c.push_back(rel); c.push_back(as::src(3, 0, 0x00));
    c.push_back(as::ins(40, 1)); c.push_back(src(B, 0));                        // if b0
    emit(c, 1, dst(T, 0, 0x2), { src(C, 20) });                                   //   mov r0.y, c20 (undeclared)
    c.push_back(as::ins(43, 0));                                                // endif
    for (uint32_t k = 0; k < 4; k++) emit(c, 9, dst(O, 0, 1u << k), { src(T, 0), src(C, 8 + k) });
    c.push_back(0x0000FFFFu);
    const auto b = analyzeVertexShaderForCapture(c.data(), c.size());
    CHECK(b.valid && b.hasGeometryConstantRanges, "skinned shader should analyse");
    CHECK(rangesContain(b, 41) && rangesContain(b, 52), "all bone registers hashed");
    CHECK(rangesContain(b, 20), "undeclared register read by the position is hashed (conservative)");
    CHECK(!rangesContain(b, 8), "camera matrix of a skinned shader not hashed");
  }

  // Subroutines are not analysed: the shader keeps stock behaviour.
  {
    std::vector<uint32_t> c = { 0xFFFE0300u, as::ins(25, 1), as::src(18, 1), as::ins(28, 0), 0x0000FFFFu };
    CHECK(!analyzeVertexShaderForCapture(c.data(), c.size()).valid, "call/label must mark the analysis invalid");
  }

  // Forward-lit skinned SH shader like vs_87D8E2DF (consumes NORMAL0 inside `if b0` into SH color,
  // never writes a normal output): must synthesize skinned world normal from v3 + gBones!
  {
    using namespace as;
    std::vector<uint32_t> c = { 0xFFFE0300u };
    ctab(c, { { "gWorldToProj", 8, 4 }, { "cSHParams", 17, 7 }, { "gBones", 41, 156 } });
    def(c, 0, 765.0f, 0.0f, 0.0f, 0.0f);
    def(c, 3, 2.0f, -1.0f, 0.0f, 0.0f);
    dcl(c, 0, 0, dst(V, 0)); dcl(c, 3, 0, dst(V, 3)); dcl(c, 2, 0, dst(V, 6)); dcl(c, 1, 0, dst(V, 7));
    dcl(c, 0, 0, dst(O, 0)); dcl(c, 5, 6, dst(O, 3));
    emit(c, 5, dst(T, 0), { src(C, 0, 0x00), src(V, 6) });
    emit(c, 46, dst(3, 0, 0x1), { src(T, 0) });
    uint32_t rel = src(C, 41) | (1u << 13);
    c.push_back(as::ins(9, 4)); c.push_back(dst(T, 1, 0x1)); c.push_back(src(V, 0)); c.push_back(rel); c.push_back(as::src(3, 0, 0x00));
    for (uint32_t k = 0; k < 4; k++) emit(c, 9, dst(O, 0, 1u << k), { src(T, 1), src(C, 8 + k) });
    c.push_back(as::ins(40, 1)); c.push_back(src(B, 0));
    emit(c, 4, dst(T, 5, 0x7), { src(V, 3), src(C, 3, 0x00), src(C, 3, 0x55) });
    emit(c, 9, dst(T, 5, 0x1), { src(C, 17), src(T, 5) });
    emit(c, 2, dst(O, 3), { src(T, 5), src(V, 0) });
    c.push_back(as::ins(43, 0));
    c.push_back(0x0000FFFFu);
    const auto b = analyzeVertexShaderForCapture(c.data(), c.size());
    CHECK(b.valid && !b.hasNormalOutput && b.hasSynthesizedNormal, "vs_87D8E2DF pattern must synthesize smooth normal");
    CHECK(b.synthHasSkinning && b.synthNormalInputReg == 3 && b.synthBlendIndicesReg == 6 &&
          b.synthBlendWeightReg == 7 && b.synthBonesFirstReg == 41 && b.synthBoneIndexScale == 765.0f,
          "vs_87D8E2DF skinning normal synthesis metadata mismatch");
  }

  // Skinned shaders 0x6ec90 / 0x7106c write `mul o5, c5.xxxy, v3.w` with def c5 = (0,1,0,0):
  // TEXCOORD4.xyz is always zero and reads only the w channel of NORMAL0. That is not a normal
  // output; the shader must fall back to the synthesized skinned normal. The control writes
  // `mul o5, c5.yyyy, v3` (the real normal) and must still be detected.
  for (const bool readsNormalXyz : { false, true }) {
    using namespace as;
    std::vector<uint32_t> c = { 0xFFFE0300u };
    ctab(c, { { "gWorldToProj", 8, 4 }, { "gBones", 41, 156 } });
    def(c, 5, 0.0f, 1.0f, 0.0f, 0.0f);
    dcl(c, 0, 0, dst(V, 0)); dcl(c, 3, 0, dst(V, 3)); dcl(c, 2, 0, dst(V, 6)); dcl(c, 1, 0, dst(V, 7));
    dcl(c, 0, 0, dst(O, 0)); dcl(c, 5, 4, dst(O, 5));
    for (uint32_t k = 0; k < 4; k++) emit(c, 9, dst(O, 0, 1u << k), { src(V, 0), src(C, 8 + k) });
    if (readsNormalXyz) {
      emit(c, 5, dst(O, 5), { src(C, 5, 0x55), src(V, 3) });      // mul o5, c5.yyyy, v3
    } else {
      emit(c, 5, dst(O, 5), { src(C, 5, 0x40), src(V, 3, 0xFF) }); // mul o5, c5.xxxy, v3.w
    }
    c.push_back(0x0000FFFFu);
    const auto z = analyzeVertexShaderForCapture(c.data(), c.size());
    if (readsNormalXyz) {
      CHECK(z.valid && z.hasNormalOutput && z.normalOutputUsage == 5 && z.normalOutputIndex == 4,
            "o5 = v3 must be the normal output (has=%d)", z.hasNormalOutput);
    } else {
      CHECK(z.valid && !z.hasNormalOutput && z.hasSynthesizedNormal && z.synthHasSkinning,
            "o5 = c5.xxxy * v3.w is not a normal output (has=%d synth=%d)", z.hasNormalOutput, z.hasSynthesizedNormal);
    }
  }
}

// --- game data ---------------------------------------------------------------------------
struct GameShader { size_t offset; std::vector<uint32_t> tokens; };

static std::vector<GameShader> loadGameShaders(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
  std::vector<uint32_t> words(bytes.size() / 4);
  std::memcpy(words.data(), bytes.data(), words.size() * 4);
  std::vector<GameShader> out;
  for (size_t i = 0; i < words.size(); i++) {
    if (words[i] != 0xFFFE0300u) continue;
    size_t j = i + 1;
    while (j < words.size() && words[j] != 0x0000FFFFu) {
      const uint32_t tok = words[j];
      j += ((tok & 0xFFFF) == 0xFFFE) ? 1 + ((tok >> 16) & 0x7FFF) : 1 + ((tok >> 24) & 0xF);
    }
    if (j >= words.size()) continue;
    out.push_back({ i * 4, std::vector<uint32_t>(words.begin() + i, words.begin() + j + 1) });
    i = j; // the version token also appears inside each constant table; skip the shader body
  }
  return out;
}

static void testGameShaders(const std::string& path, const char* dumpPath) {
  const auto shaders = loadGameShaders(path);
  if (shaders.empty()) {
    std::printf("SKIP game shaders: %s not found or empty\n", path.c_str());
    return;
  }
  std::FILE* dump = dumpPath ? std::fopen(dumpPath, "w") : nullptr;
  const std::set<std::string> camera = { "gWorldToProj", "gViewportOffset", "gViewportSB", "cViewPos_VS", "cProjSkinMesh" };
  const std::set<std::string> worldRotation = { "gLocalToWorld", "gBones", "gDrawScale", "gForces" };
  size_t valid = 0, world = 0, worldWithNormalInput = 0, normalsFound = 0, withRanges = 0, zeroNormalShadersSeen = 0;
  std::map<std::string, int> normalSemantics;

  for (const auto& sh : shaders) {
    Analyzer an(sh.tokens.data(), sh.tokens.size());
    const auto a = Analyzer(sh.tokens.data(), sh.tokens.size()).run();
    // Re-run the internals for per-output detail.
    an.parse(); an.matchControlFlow();
    State st; an.execute(0, an.m_code.size(), st, Sources(), false);

    std::map<std::string, std::pair<uint16_t, uint16_t>> ctab;
    for (const auto& e : an.m_ctab) if (e.set == 2) ctab[e.name] = { e.first, e.count };
    auto regName = [&](uint32_t r) -> std::string {
      for (auto& [n, fc] : ctab) if (r >= fc.first && r < uint32_t(fc.first) + fc.second) return n;
      return an.m_defined.test(r) ? "<literal>" : "<undeclared>";
    };
    const bool isWorld = ctab.count("gWorldToProj") > 0;
    bool hasNormalInput = false;
    for (int u : an.m_inputUsage) if (u == 3) hasNormalInput = true;

    valid += a.valid;
    CHECK(a.valid, "game shader at 0x%zx failed to analyse", sh.offset);
    // These write `mul o5, cN.<literal zero xyz>, v3.w`: TEXCOORD4.xyz is always (0,0,0), so they must
    // synthesize the normal. Skinned: 78BCCBDD, 514A5FAA. World: 6E196E77, F6CAEE4E, 5EA537BA, 0335612F.
    const bool zeroSkinned = sh.offset == 0x6ec90 || sh.offset == 0x7106c;
    const bool zeroWorld = sh.offset == 0x6e840 || sh.offset == 0x6f3a0 || sh.offset == 0x70c0c || sh.offset == 0x71780;
    if (zeroSkinned || zeroWorld) {
      zeroNormalShadersSeen++;
      CHECK(!a.hasNormalOutput && a.hasSynthesizedNormal && (zeroSkinned ? a.synthHasSkinning : a.synthHasLocalToWorld),
            "0x%zx: zero TEXCOORD4 must not be the normal output (has=%d synth=%d)", sh.offset, a.hasNormalOutput, a.hasSynthesizedNormal);
    }

    if (isWorld) {
      world++;
      // Geometry hash rule.
      if (a.hasGeometryConstantRanges) {
        withRanges++;
        for (auto& [name, fc] : ctab) {
          for (uint32_t r = fc.first; r < uint32_t(fc.first) + fc.second; r++) {
            const bool inHash = rangesContain(a, r);
            if (camera.count(name)) CHECK(!inHash, "0x%zx: camera constant %s (c%u) is hashed", sh.offset, name.c_str(), r);
            if (name == "gLocalToWorld" || name == "gBones") CHECK(inHash, "0x%zx: %s (c%u) not hashed", sh.offset, name.c_str(), r);
          }
        }
        // Every non-camera declared constant that can reach the position must be hashed.
        const bool expectVertexAnim = ctab.count("gWindAnimInfo1") > 0 || ctab.count("cWaveHeights") > 0;
        CHECK(a.hasVertexAnimation == expectVertexAnim, "0x%zx: expected hasVertexAnimation=%d, got %d",
              sh.offset, int(expectVertexAnim), int(a.hasVertexAnimation));
        const int pos = an.outputRegister(0, 0);
        Sources slice; for (uint32_t c = 0; c < 4; c++) slice |= st.outputs[pos * 4 + c];
        for (uint32_t r = 0; r < kMaxFloatConsts; r++) {
          if (slice.test(constBit(r)) && !camera.count(regName(r))) CHECK(rangesContain(a, r), "0x%zx: c%u (%s) reaches the position but is not hashed", sh.offset, r, regName(r).c_str());
        }
      } else {
        CHECK(!ctab.count("gLocalToWorld") && !ctab.count("gBones"), "0x%zx: world shader with object motion constants lost its ranges", sh.offset);
      }
      // Normal output rule.
      if (hasNormalInput) {
        worldWithNormalInput++;
        if (!a.hasNormalOutput && std::getenv("CAT_EXPLAIN")) {
          std::printf("no normal 0x%zx skinned=%d:", sh.offset, int(ctab.count("gBones")));
          for (uint32_t o = 0; o < kMaxOutputs; o++) {
            if (an.m_outputUsage[o] < 0) continue;
            std::printf(" [u%d.%d mask%x:", an.m_outputUsage[o] & 0xFF, an.m_outputUsage[o] >> 8, an.m_outputMask[o]);
            for (uint32_t c = 0; c < 3; c++) {
              std::printf(" {");
              for (uint32_t v = 0; v < kMaxInputs; v++) {
                for (uint32_t k = 0; k < 4; k++) {
                  if (st.outputs[o * 4 + c].test(inputBit(v, k)) && an.m_inputUsage[v] >= 0) std::printf("%d.%d.%c ", an.m_inputUsage[v] & 0xFF, an.m_inputUsage[v] >> 8, "xyzw"[k]);
                }
              }
              std::printf("}");
            }
            std::printf("]");
          }
          std::printf("\n");
        }
        if (a.hasNormalOutput) {
          normalsFound++;
          normalSemantics[(a.normalOutputUsage == 5 ? "TEXCOORD" : std::to_string(a.normalOutputUsage)) + std::to_string(a.normalOutputIndex)]++;
          const int o = an.outputRegister(a.normalOutputUsage, a.normalOutputIndex);
          for (uint32_t c = 0; c < 3; c++) {
            const Sources s = st.outputs[o * 4 + c];
            for (uint32_t r = 0; r < kMaxFloatConsts; r++) {
              if (!s.test(constBit(r))) continue;
              const std::string n = regName(r);
              CHECK(n == "<literal>" || worldRotation.count(n), "0x%zx: normal output reads %s (c%u), not a world rotation", sh.offset, n.c_str(), r);
            }
          }
        }
        else {
          CHECK(a.hasSynthesizedNormal, "0x%zx: world shader with normal input has neither normalOutput nor synthesizedNormal (flat-triangle bug)", sh.offset);
          if (a.hasSynthesizedNormal) {
            normalsFound++;
            normalSemantics[a.synthHasSkinning ? "SYNTH_SKINNED" : (a.synthHasLocalToWorld ? "SYNTH_WORLD" : "SYNTH_OBJ")]++;
          }
        }
      }
    }

    if (dump) {
      std::fprintf(dump, "{\"offset\":%zu,\"valid\":%s,\"world\":%s,\"normalInput\":%s,\"normal\":%s",
                   sh.offset, a.valid ? "true" : "false", isWorld ? "true" : "false", hasNormalInput ? "true" : "false",
                   a.hasNormalOutput ? "true" : "false");
      if (a.hasNormalOutput) std::fprintf(dump, ",\"normalUsage\":%u,\"normalIndex\":%u", a.normalOutputUsage, a.normalOutputIndex);
      std::fprintf(dump, ",\"ranges\":[");
      for (size_t k = 0; k < a.geometryConstantRanges.size(); k++)
        std::fprintf(dump, "%s[%u,%u]", k ? "," : "", a.geometryConstantRanges[k].first, a.geometryConstantRanges[k].second);
      std::fprintf(dump, "],\"hasRanges\":%s}\n", a.hasGeometryConstantRanges ? "true" : "false");
    }
  }
  if (dump) std::fclose(dump);

  std::printf("game shaders: %zu, analysed: %zu, world: %zu (with geometry ranges: %zu), world with input normal: %zu, normal output found: %zu\n",
              shaders.size(), valid, world, withRanges, worldWithNormalInput, normalsFound);
  for (auto& [k, v] : normalSemantics) std::printf("  normal in %s: %d shaders\n", k.c_str(), v);
  // Every world shader with an input normal (172/172 = 100%) must either emit a normal output
  // or synthesize a smooth normal so zero world or character shaders fall back to flat triangles.
  const std::map<std::string, int> expected = {
    { "SYNTH_OBJ", 5 }, { "SYNTH_SKINNED", 21 }, { "SYNTH_WORLD", 48 },
    { "TEXCOORD1", 5 }, { "TEXCOORD2", 21 }, { "TEXCOORD3", 3 }, { "TEXCOORD4", 69 }
  };
  CHECK(shaders.size() == 263 && world == 198 && worldWithNormalInput == 172 && zeroNormalShadersSeen == 6, "game shader inventory changed");
  CHECK(normalsFound == worldWithNormalInput && normalSemantics == expected,
        "normal output detection changed (found %zu of %zu)", normalsFound, worldWithNormalInput);
}

int main(int argc, char** argv) {
  std::string gamePath = "D:\\Steam\\steamapps\\common\\Tom Clancy's Splinter Cell Blacklist\\data\\Shaders\\d3d9\\VertexShaders.bin";
  const char* dumpPath = nullptr;
  for (int i = 1; i < argc; i++) {
    if (std::string(argv[i]) == "--game" && i + 1 < argc) gamePath = argv[++i];
    else if (std::string(argv[i]) == "--dump" && i + 1 < argc) dumpPath = argv[++i];
  }
  testSynthetic();
  testGameShaders(gamePath, dumpPath);
  std::printf(g_failures ? "capture_analysis_test: %d FAILURES\n" : "capture_analysis_test: PASS\n", g_failures);
  return g_failures ? 1 : 0;
}
