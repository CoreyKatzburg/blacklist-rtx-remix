// Tests for small pure pieces of the Remix runtime changes (compiled from the real headers):
//   - rtx_camera_cut.h:            camera-cut rule against jump distances recorded in a real session
//   - rtx_instance_trace_class.h:  the "unordered" flag is recomputed on every update (Sam fade case)
//   - rtx_sparse_unique_cache.h:   retire() frees the object but never hands its index to a new one
//   - rtx_telemetry.h:             line format and the background writer (truncate, order, flush on close)
// Usage: runtime_logic_test.exe <scratch dir>. Writes <scratch dir>\telemetry_sample.jsonl for the
// Python contract check (check_telemetry_contract.py). Exit code 0 = pass.

#define _CRT_SECURE_NO_WARNINGS
#include <functional>
#include "../../src/dxvk-remix/src/dxvk/rtx_render/rtx_camera_cut.h"
#include "../../src/dxvk-remix/src/dxvk/rtx_render/rtx_instance_trace_class.h"
#include "../../src/dxvk-remix/src/dxvk/rtx_render/rtx_sparse_unique_cache.h"
#include "../../src/dxvk-remix/src/dxvk/rtx_render/rtx_telemetry.h"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

using namespace dxvk;

static int g_failures = 0;
#define CHECK(cond, ...) do { if (!(cond)) { ++g_failures; std::printf("FAIL line %d: ", __LINE__); std::printf(__VA_ARGS__); std::printf("\n"); } } while (0)

static void testCameraCut() {
  // Per-frame camera jumps logged as Remix "camera cuts" with the old 300-unit threshold
  // (perf audit 2026-11-11, session pid 27064, cuts_out.txt).
  const float gameplayJumps[] = { 524.1f, 425.6f, 604.6f, 534.3f, 487.0f, 485.9f, 344.3f, 422.6f,
                                  453.7f, 329.6f, 328.6f, 328.7f, 327.9f, 500.7f, 567.6f, 318.5f };
  const float teleports[] = { 4017.5f, 3917.2f, 4430.8f, 3388.3f, 3770.0f, 6686.2f };
  for (float d : gameplayJumps) {
    CHECK(!isCameraCutJump(d * d, kDefaultCameraCutDistance), "gameplay jump of %.1f units must not be a cut", d);
    CHECK(isCameraCutJump(d * d, 300.0f), "sanity: old 300-unit threshold did cut at %.1f", d);
  }
  for (float d : teleports) {
    CHECK(isCameraCutJump(d * d, kDefaultCameraCutDistance), "teleport of %.1f units must be a cut", d);
  }
  CHECK(!isCameraCutJump(0.0f, kDefaultCameraCutDistance), "standing still is not a cut");
}

static void testTraceClassNotSticky() {
  // Sam during the close-camera fade: blended draw on the player model -> unordered TLAS.
  InstanceTraceInputs fade;
  fade.isPlayerModel = true;
  fade.isFullyOpaque = false;
  fade.isBlendingDisabled = false;
  // Sam back in the opaque G-buffer pass.
  InstanceTraceInputs opaque;
  opaque.isPlayerModel = true;

  const InstanceTraceClass a = classifyInstanceForTracing(fade);
  CHECK(a.isUnordered && a.forceNoOpaque, "blended player-model draw goes to the unordered TLAS");
  const InstanceTraceClass b = classifyInstanceForTracing(opaque);
  CHECK(!b.isUnordered && !b.forceNoOpaque && b.geometry == TraceGeometry::Opaque,
        "the next opaque update must leave the unordered TLAS and be traced as opaque");

  // Same decision for every combination, whatever came before (no hidden state): exhaustively
  // check that "unordered" is exactly the blended/particle/decal/emissive rule.
  for (uint32_t bits = 0; bits < (1u << 7); bits++) {
    InstanceTraceInputs in;
    in.isFullyOpaque = bits & 1;
    in.isParticle = bits & 2;
    in.isDecal = bits & 4;
    in.isBlendingDisabled = bits & 8;
    in.emissiveBlend = bits & 16;
    in.isPlayerModel = bits & 32;
    in.usesRaytracedRenderTarget = bits & 64;
    const bool expected = !in.usesRaytracedRenderTarget &&
      ((!in.isFullyOpaque && in.isParticle) || in.isDecal ||
       (!in.isFullyOpaque && !in.isBlendingDisabled && in.isPlayerModel) || in.emissiveBlend);
    const InstanceTraceClass c = classifyInstanceForTracing(in);
    CHECK(c.isUnordered == expected, "unordered rule mismatch for input bits 0x%x", bits);
    CHECK(classifyInstanceForTracing(opaque).isUnordered == false, "opaque after 0x%x must not be unordered", bits);
  }
}

struct IntHash { size_t operator()(int v) const { return std::hash<int>()(v); } };

static void testCacheRetire() {
  SparseUniqueCache<int, IntHash> cache;
  const uint32_t a = cache.track(10), b = cache.track(20), c = cache.track(30);
  CHECK(cache.getActiveCount() == 3, "three tracked");
  cache.retire(20);
  CHECK(cache.getActiveCount() == 2, "retired entry is not active (got %u)", cache.getActiveCount());
  CHECK(cache.getObjectTable()[b] == 0, "retired slot holds an empty object");
  const uint32_t d = cache.track(40);
  CHECK(d != b && d == 3, "a new object never reuses a retired index (got %u)", d);
  CHECK(cache.track(20) == 4, "re-tracking the retired object gets a fresh index");
  cache.retire(999); // unknown: no-op
  CHECK(cache.getActiveCount() == 4, "active count after re-track (got %u)", cache.getActiveCount());
  cache.free(10);
  CHECK(cache.track(50) == a, "free() still recycles indices as before");
  cache.clear();
  CHECK(cache.getActiveCount() == 0 && cache.getTotalCount() == 0, "clear resets everything");
  (void) c;
}

static std::vector<std::string> readLines(const std::string& path) {
  std::ifstream f(path);
  std::vector<std::string> lines;
  for (std::string line; std::getline(f, line);) lines.push_back(line);
  return lines;
}

static void testTelemetry(const std::string& dir) {
  RtxFrameTelemetry t;
  t.frame = 123456789012ull;
  t.qpcMs = 98765432.125;
  t.frameTimeMs = 11.1111;
  t.gpuTimeMs = 9.87654;
  t.instances = 4000000000u;
  t.skinnedInstances = 12;
  t.staticBlasRebuilds = 3;
  t.cameraCut = true;
  t.textureMemoryMB = 2048.25;
  t.vramUsedMB = 7123.75;
  t.skinnedTriangles = 42000;
  t.inwardSkinnedTriangles = 7;
  t.lumaThumb = "AAAA";
  t.rayTraced = true;
  t.telemetryCpuMs = 0.25;
  t.replacedMaterialDraws = 800;
  t.legacyMaterialDraws = 31;
  t.texturesDemoted = 2;
  t.textureBudgetMB = 3072.0;
  t.vramFreeMB = 1185.4;
  t.thumbNonFinite = 5;
  t.thumbClipped = 9;
  t.skips[size_t(RtxSkip::ZeroPrimitives)] = 4;
  t.skips[size_t(RtxSkip::NoTextureHash)] = 17;
  t.passes.push_back(RtxPassTime { "InjectRTX", 28.5, 3.25, 1 });
  t.passes.push_back(RtxPassTime { "InjectRTX/Gate Telemetry", -1.0, 0.125, 2 });
  t.newLegacyMaterials = { 0x0123456789ABCDEFull, 0x1ull };
  t.rgbThumb = "BBBB";

  // Normal gate summary (worker E's fake cases). Instance 0: every normal zero, so every triangle is
  // zero-normal and none can be judged inward. Instance 1: some inward. Instance 2: normals fully flipped
  // (inward == tris), so it must sort first. Instance 3: static with zero normals, counted apart from skinned.
  {
    std::vector<RtxGateInstance> gated(4);
    gated[0].tris = 300; gated[0].zeroNormal = 300; gated[0].inward = 0;
    gated[1].tris = 500; gated[1].inward = 12; gated[1].geomHash = 0xB1ull;
    gated[2].tris = 200; gated[2].inward = 200; gated[2].geomHash = 0xA2ull; gated[2].albedoHash = 0xC0FFEEull;
    gated[2].cull = 2; gated[2].flip = true; gated[2].enc = true;
    gated[3].skinned = false; gated[3].tris = 900; gated[3].zeroNormal = 40;
    RtxFrameTelemetry s;
    summarizeGateInstances(gated, s);
    CHECK(s.zeroNormalInstances == 1 && s.zeroNormalStaticInstances == 1, "zero-normal instances counted by kind: %u skinned %u static",
          s.zeroNormalInstances, s.zeroNormalStaticInstances);
    CHECK(s.topInwardInstances.size() == 2 && s.topInwardInstances[0].geomHash == 0xA2ull &&
          s.topInwardInstances[1].geomHash == 0xB1ull, "fully flipped instance first, zero-normal and static instances absent");
    std::vector<RtxGateInstance> many(12);
    for (uint32_t i = 0; i < many.size(); ++i) {
      many[i].tris = 100;
      many[i].inward = 1 + (i % 3);
      many[i].geomHash = i;
    }
    summarizeGateInstances(many, s);
    CHECK(s.topInwardInstances.size() == kTopInwardInstances && s.topInwardInstances[0].inward == 3 &&
          s.topInwardInstances[0].geomHash == 2 && s.topInwardInstances.back().inward == 2,
          "list capped at 8, most inward first, ties in dispatch order");
    t.zeroNormalSkinnedTriangles = 300;
    t.zeroNormalStaticTriangles = 40;
    summarizeGateInstances(gated, t);
  }
  char line[2048];
  const size_t n = formatTelemetryLine(line, sizeof(line), t);
  CHECK(n > 0 && line[n - 1] == '\n', "line ends with a newline");
  CHECK(std::strstr(line, "\"frame\":123456789012,\"qpcMs\":98765432.125,") != nullptr, "frame and qpcMs printed: %s", line);
  CHECK(std::strstr(line, "\"cameraCut\":true") != nullptr, "cameraCut printed as JSON bool");
  CHECK(std::strstr(line, "\"instances\":4000000000,") != nullptr, "uint32 printed unsigned");
  CHECK(std::strstr(line, "\"skinnedTriangles\":42000,\"inwardSkinnedTriangles\":7") != nullptr, "skinned triangle fields printed: %s", line);
  CHECK(std::strstr(line, "\"zeroNormalSkinnedTriangles\":300,\"zeroNormalInstances\":1,\"zeroNormalStaticTriangles\":40,"
                          "\"zeroNormalStaticInstances\":1,\"topInwardInstances\":[{\"inward\":200,\"tris\":200,"
                          "\"geomHash\":\"00000000000000A2\",\"albedoHash\":\"0000000000C0FFEE\",\"cull\":2,\"flip\":1,\"blend\":0,"
                          "\"enc\":1},{\"inward\":12,") != nullptr, "normal gate fields printed: %s", line);
  CHECK(std::strstr(line, "\"lumaThumb\":\"AAAA\"}") != nullptr, "lumaThumb printed when non-empty: %s", line);
  CHECK(std::strstr(line, "\"rayTraced\":true,\"telemetryCpuMs\":0.250,\"replacedMaterialDraws\":800,\"legacyMaterialDraws\":31,"
                          "\"texturesDemoted\":2,\"textureBudgetMB\":3072.0,\"vramFreeMB\":1185.4,\"thumbNonFinite\":5,\"thumbClipped\":9,") != nullptr,
        "recorder, material, texture and bad-pixel fields printed: %s", line);
  CHECK(std::strstr(line, "\"skips\":{\"zeroPrimitives\":4,\"noTextureHash\":17},") != nullptr, "only non-zero skip reasons printed: %s", line);
  CHECK(std::strstr(line, "\"passMs\":{\"InjectRTX\":[28.500,3.250,1],\"InjectRTX/Gate Telemetry\":[-1.000,0.125,2]},") != nullptr,
        "step times printed as [gpuMs,cpuMs,count]: %s", line);
  CHECK(std::strstr(line, "\"newLegacyMaterials\":[\"0123456789ABCDEF\",\"0000000000000001\"],\"rgbThumb\":\"BBBB\",") != nullptr,
        "fallback texture hashes printed as 16 hex digits: %s", line);
  CHECK(formatTelemetryLine(line, 16, t) == 0, "too-small buffer reports failure instead of a cut line");
  CHECK(formatTelemetryLine(line, n, t) == 0 && formatTelemetryLine(line, n + 1, t) == n,
        "a buffer one byte short fails and an exact fit succeeds");
  char emptyLine[1024];
  formatTelemetryLine(emptyLine, sizeof(emptyLine), RtxFrameTelemetry());
  CHECK(std::strstr(emptyLine, "\"skips\":{},\"passMs\":{}}\n") != nullptr, "empty maps and absent optional fields: %s", emptyLine);

  const std::string path = dir + "\\telemetry_sample.jsonl";
  {
    std::ofstream stale(path);
    stale << "stale line from a previous launch\n";
  }
  constexpr uint32_t kFrames = 5000;
  std::vector<double> pushUs;
  pushUs.reserve(kFrames);
  {
    RtxTelemetryWriter writer(path);
    CHECK(writer.isOpen(), "writer opens %s", path.c_str());
    for (uint32_t i = 0; i < kFrames; i++) {
      RtxFrameTelemetry f;
      f.frame = i;
      f.qpcMs = (i % 10) == 0 ? 0.0 : 5.0e8 + i * 16.667;   // 0 = frame not stamped
      f.frameTimeMs = 10.0 + (i % 7);
      f.gpuTimeMs = 8.0;
      f.instances = 1500 + i % 13;
      f.cameraCut = (i % 1000) == 0;
      f.rayTraced = (i % 4) != 0;
      f.skips[i % size_t(RtxSkip::Count)] = 1 + i % 3;   // every reason name reaches the contract check
      f.passes.push_back(RtxPassTime { "InjectRTX", (i % 50) == 0 ? -1.0 : 28.5, 3.0, 1 });
      const auto start = std::chrono::steady_clock::now();
      writer.push(f);
      pushUs.push_back(std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - start).count());
    }
  } // destructor flushes and closes
  const auto lines = readLines(path);
  CHECK(lines.size() == kFrames, "one line per frame and the stale file was truncated (got %zu lines)", lines.size());
  for (uint32_t i = 0; i < lines.size() && i < kFrames; i++) {
    const std::string expected = "{\"frame\":" + std::to_string(i) + ",";
    if (lines[i].rfind(expected, 0) != 0) {
      CHECK(false, "line %u out of order: %s", i, lines[i].c_str());
      break;
    }
  }
  double mean = 0.0;
  for (double us : pushUs) mean += us;
  mean /= double(pushUs.size());
  std::sort(pushUs.begin(), pushUs.end());
  const double p999 = pushUs[size_t(pushUs.size() * 0.999)];
  std::printf("telemetry push cost: mean %.2f us, 99.9th percentile %.1f us, max %.1f us (budget 100 us)\n", mean, p999, pushUs.back());
  CHECK(mean < 10.0 && p999 < 100.0, "push must cost well under 0.1 ms (mean %.2f us, p99.9 %.1f us)", mean, p999);
}

int main(int argc, char** argv) {
  const std::string dir = argc > 1 ? argv[1] : ".";
  testCameraCut();
  testTraceClassNotSticky();
  testCacheRetire();
  testTelemetry(dir);
  std::printf(g_failures ? "runtime_logic_test: %d FAILURES\n" : "runtime_logic_test: PASS\n", g_failures);
  return g_failures ? 1 : 0;
}
