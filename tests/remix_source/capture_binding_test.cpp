// USD capture material binding (rtx_capture_binding.h, rtx.capture.bindInstanceMaterials).
//
// Replays GameCapturer::newInstance's registration on a fake instance table, then exports through the same
// functions prepExportMeshes / prepExportInstances call. registerInstance below copies newInstance's rules
// (rtx_game_capturer.cpp): the first instance registered for a mesh sets the mesh's matHash; a material is
// captured when the instance's hash is non-zero and not captured yet, and it is stored under the hash of the
// material BlasEntry::getMaterialData returns, which is the BLAS input material when the BLAS does not know the
// asked hash (rtx_types.h getMaterialData).
//
// Case "stale first" is mesh AF006C0D80844A32 in capture_2026-11-16_21-56-00.usd (run 20261116-215353): the frame
// drew it twice with material 0B5610673719A815, which is in Looks, yet its 3 instances bind nothing.
//
// Exit 0 = pass. Built with /DFORCE_OPTION_OFF the "option on" expectations run with the option off and must fail;
// run_remix_source_tests.ps1 checks that.

#include <cstdio>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <vector>

#include "../../src/dxvk-remix/src/dxvk/rtx_render/rtx_capture_binding.h"

using namespace dxvk;

namespace {
  int g_failures = 0;

  void check(bool ok, const std::string& what) {
    std::printf("%s  %s\n", ok ? "ok  " : "FAIL", what.c_str());
    if (!ok) {
      ++g_failures;
    }
  }

  constexpr uint64_t kNone = ~0ull; // "no binding" in expectations

  struct FakeBlas {
    uint64_t inputMat;          // material of the BLAS's first draw this frame
    std::set<uint64_t> cached;  // other materials drawn on it this frame (cacheMaterial)
    uint64_t resolve(uint64_t h) const { return (h == inputMat || cached.count(h)) ? h : inputMat; }
  };

  struct FakeInstance {
    uint64_t mesh;
    uint64_t mat;     // RtInstance::getMaterialDataHash
    const FakeBlas* blas;
  };

  struct Cap {
    std::set<uint64_t> materials;
    std::map<uint64_t, uint64_t> meshMat;   // mesh -> matHash of its first registered instance
    std::map<uint64_t, size_t> meshCount;
    struct Inst { uint64_t mesh, mat; size_t num; };
    std::vector<Inst> instances;
  };

  // Mirrors GameCapturer::newInstance.
  void registerInstance(Cap& cap, const FakeInstance& in) {
    const bool isNewMat = in.mat != 0 && cap.materials.count(in.mat) == 0;
    if (isNewMat) {
      cap.materials.insert(in.blas->resolve(in.mat));
    }
    if (cap.meshMat.count(in.mesh) == 0) {
      cap.meshMat[in.mesh] = in.mat;
      cap.meshCount[in.mesh] = 0;
    }
    cap.instances.push_back({ in.mesh, in.mat, cap.meshCount[in.mesh]++ });
  }

  struct Exported {
    std::map<uint64_t, uint64_t> mesh;     // mesh -> bound material or kNone
    std::vector<uint64_t> inst;            // per instance, in table order
  };

  // Mirrors prepExportMeshes / prepExportInstances.
  Exported exportCap(const Cap& cap, bool option) {
    const auto isCaptured = [&cap](uint64_t h) { return cap.materials.count(h) > 0; };
    std::map<uint64_t, capture_binding::Fallback> fallbacks;
    if (option) {
      for (const auto& i : cap.instances) {
        fallbacks[i.mesh].offer(i.num, i.mat, isCaptured);
      }
    }
    Exported out;
    for (const auto& [mesh, mat] : cap.meshMat) {
      const auto it = fallbacks.find(mesh);
      const auto b = capture_binding::meshMaterial(mat, it != fallbacks.end() ? it->second : capture_binding::Fallback{},
                                                   isCaptured, option);
      out.mesh[mesh] = b.has_value() ? *b : kNone;
    }
    for (const auto& i : cap.instances) {
      const auto b = capture_binding::instanceMaterial(cap.meshMat.at(i.mesh), i.mat, isCaptured, option);
      out.inst.push_back(b.has_value() ? *b : kNone);
    }
    return out;
  }

  Exported run(const std::vector<FakeInstance>& table, bool option) {
    Cap cap;
    for (const auto& in : table) {
      registerInstance(cap, in);
    }
    return exportCap(cap, option);
  }

  bool onFlag() {
#ifdef FORCE_OPTION_OFF
    return false;
#else
    return true;
#endif
  }
}

int main() {
  const uint64_t kMeshA = 0xAF006C0D80844A32ull, kDrawn = 0x0B5610673719A815ull, kStale = 0x5A1E000000000001ull;
  const uint64_t kMeshB = 0x1111000000000002ull, kMatB = 0x2222000000000003ull;
  const bool on = onFlag();

  // 1. Stale first: instance left from last frame registers first with a hash its BLAS no longer knows.
  {
    const FakeBlas blas{ kDrawn, {} };
    const std::vector<FakeInstance> table{ { kMeshA, kStale, &blas }, { kMeshA, kDrawn, &blas }, { kMeshA, kDrawn, &blas } };
    Cap cap;
    for (const auto& in : table) {
      registerInstance(cap, in);
    }
    check(cap.materials.count(kDrawn) == 1 && cap.materials.count(kStale) == 0,
          "stale first: drawn material captured (in Looks), stale hash not captured");
    const Exported off = run(table, false);
    check(off.mesh.at(kMeshA) == kNone && off.inst == std::vector<uint64_t>{ kNone, kNone, kNone },
          "stale first, option off: mesh and all 3 instances unbound (the bug as in the USD)");
    const Exported fix = run(table, on);
    check(fix.mesh.at(kMeshA) == kDrawn, "stale first, option on: mesh binds the drawn material");
    check(fix.inst == std::vector<uint64_t>{ kNone, kDrawn, kDrawn },
          "stale first, option on: the 2 drawn instances bind it, the stale one stays unbound (no dangling binding)");
  }

  // 2. Bound mesh: drawn instance first, stale one second. Upstream binds both, the stale one to an uncaptured
  //    hash. The option must not change a single binding.
  {
    const FakeBlas blas{ kDrawn, {} };
    const std::vector<FakeInstance> table{ { kMeshA, kDrawn, &blas }, { kMeshA, kStale, &blas } };
    const Exported off = run(table, false), fix = run(table, on);
    check(off.mesh.at(kMeshA) == kDrawn && off.inst == std::vector<uint64_t>{ kDrawn, kStale },
          "bound mesh, option off: upstream bindings");
    check(fix.mesh == off.mesh && fix.inst == off.inst, "bound mesh, option on: identical to option off");
  }

  // 3. First instance carries hash 0 (no material): nothing captured for it.
  {
    const FakeBlas blas{ kDrawn, {} };
    const std::vector<FakeInstance> table{ { kMeshA, 0, &blas }, { kMeshA, kDrawn, &blas } };
    check(run(table, false).mesh.at(kMeshA) == kNone, "hash 0 first, option off: mesh unbound");
    const Exported fix = run(table, on);
    check(fix.mesh.at(kMeshA) == kDrawn && fix.inst == std::vector<uint64_t>{ kNone, kDrawn },
          "hash 0 first, option on: mesh and drawn instance bind the drawn material");
  }

  // 4. Two captured materials on one mesh: the mesh takes the lowest-numbered instance's, whatever the table order
  //    of the export loop (prepExportMeshes walks an unordered_map).
  {
    capture_binding::Fallback fb;
    const auto all = [](uint64_t) { return true; };
    fb.offer(2, kMatB, all);
    fb.offer(1, kDrawn, all);
    fb.offer(3, 0x9999ull, all);
    check(fb.hash == kDrawn, "fallback: lowest instance number wins regardless of offer order");
    capture_binding::Fallback none;
    none.offer(0, kStale, [](uint64_t) { return false; });
    check(!none.hash.has_value(), "fallback: an uncaptured hash is never offered");
  }

  // 5. Mesh with no captured material on any instance stays unbound with the option on.
  {
    const FakeBlas blas{ kMatB, {} };
    Cap cap;
    registerInstance(cap, { kMeshB, 0, &blas });
    registerInstance(cap, { kMeshB, 0, &blas });
    const Exported fix = exportCap(cap, on);
    check(fix.mesh.at(kMeshB) == kNone && fix.inst == std::vector<uint64_t>{ kNone, kNone },
          "no captured material: option on adds nothing");
  }

  // 6. Two meshes sharing a BLAS-unknown stale hash do not borrow each other's material.
  {
    const FakeBlas blasA{ kDrawn, {} }, blasB{ kMatB, {} };
    const std::vector<FakeInstance> table{ { kMeshA, kStale, &blasA }, { kMeshB, kStale, &blasB },
                                           { kMeshA, kDrawn, &blasA }, { kMeshB, kMatB, &blasB } };
    const Exported fix = run(table, on);
    check(fix.mesh.at(kMeshA) == kDrawn && fix.mesh.at(kMeshB) == kMatB, "two meshes: each binds its own drawn material");
  }

  std::printf(g_failures == 0 ? "capture_binding_test: PASS\n" : "capture_binding_test: FAIL (%d)\n", g_failures);
  return g_failures == 0 ? 0 : 1;
}
