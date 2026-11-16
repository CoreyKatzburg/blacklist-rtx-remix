// Replay test for src/d3d9/d3d9_rtx_instanced_capture.h: the vertex capture layout of a
// hardware-instanced draw (con_boiler boiler tank, draws 132-134 at frame 5690 of run
// 20261116-085935: only one copy reached ray tracing and the USD capture).
//
// A fake draw is replayed on the CPU the way the GPU runs it: the capture shader writes every
// (vertex, instance) to vertexCaptureSlot(), instances in order (a later write wins, as on the GPU),
// and the ray-traced triangles are read back through expandInstancedIndices(). Expected triangles
// come from the fake instance transforms, not from the header.
// Not covered here: the SPIR-V that dxso_compiler.cpp emits for the slot; the live run checks it.
// Exit code 0 = pass.

#define _CRT_SECURE_NO_WARNINGS
#include "../../src/dxvk-remix/src/d3d9/d3d9_rtx_instanced_capture.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <vector>

using namespace dxvk;

static int g_failures = 0;
#define CHECK(cond, ...) do { if (!(cond)) { ++g_failures; std::printf("FAIL line %d: ", __LINE__); std::printf(__VA_ARGS__); std::printf("\n"); } } while (0)

struct P { float x, y, z; };
using Tri = std::array<float, 9>;

static Tri tri(P a, P b, P c) { return { a.x, a.y, a.z, b.x, b.y, b.z, c.x, c.y, c.z }; }

// Replays one indexed triangle-list draw; returns the triangles ray tracing would see.
static std::vector<Tri> replay(const std::vector<P>& obj, const std::vector<uint16_t>& indices, uint32_t baseVertex,
                               const std::vector<P>& instanceOffsets, const InstancedCaptureLayout& layout) {
  std::vector<P> captured(layout.capturedVertexCount, P { -1e9f, -1e9f, -1e9f });
  for (uint32_t inst = 0; inst < instanceOffsets.size(); inst++) {
    for (uint32_t v = 0; v < obj.size(); v++) {
      const uint32_t slot = vertexCaptureSlot(baseVertex + v, baseVertex, inst, layout.instanceVertexStride);
      if (slot >= captured.size()) {
        CHECK(false, "instance %u vertex %u writes slot %u past the capture buffer (%zu)", inst, v, slot, captured.size());
        continue;
      }
      const P& o = instanceOffsets[inst];
      captured[slot] = P { obj[v].x + o.x, obj[v].y + o.y, obj[v].z + o.z };
    }
  }
  std::vector<uint32_t> expanded(layout.indexCount);
  expandInstancedIndices(indices.data(), uint32_t(indices.size()), layout, expanded.data());
  std::vector<Tri> tris;
  for (size_t i = 0; i + 2 < expanded.size(); i += 3) {
    if (expanded[i] >= captured.size() || expanded[i + 1] >= captured.size() || expanded[i + 2] >= captured.size()) {
      CHECK(false, "index past the capture buffer at %zu", i);
      continue;
    }
    tris.push_back(tri(captured[expanded[i]], captured[expanded[i + 1]], captured[expanded[i + 2]]));
  }
  return tris;
}

static void testInstancedCopiesKeepTheirOwnTransforms() {
  // A quad (two triangles) whose vertices start at vertex 10 of the buffer.
  const std::vector<P> obj = { { 0, 0, 0 }, { 10, 0, 0 }, { 0, 20, 0 }, { 10, 20, 3 } };
  const std::vector<uint16_t> indices = { 0, 1, 2, 2, 1, 3 };
  const uint32_t baseVertex = 10;
  for (const std::vector<P>& offsets : { std::vector<P> { { 100, 0, 0 }, { 0, 0, 500 } },
                                         std::vector<P> { { 100, 0, 0 }, { 0, 0, 500 }, { 0, -70, 0 } } }) {
    const uint32_t n = uint32_t(offsets.size());
    const InstancedCaptureLayout layout = planInstancedCapture(uint32_t(obj.size()), uint32_t(indices.size()), n, true, true);
    CHECK(layout.copies == n, "%u instances: %u copies captured", n, layout.copies);
    CHECK(layout.capturedVertexCount == obj.size() * n, "%u instances: capture buffer holds %u vertices, want %zu",
          n, layout.capturedVertexCount, obj.size() * n);
    CHECK(!layout.sharedSlots, "%u instances: instances still share slots", n);

    std::vector<Tri> got = replay(obj, indices, baseVertex, offsets, layout);
    std::vector<Tri> want;
    for (const P& o : offsets) {
      auto at = [&](uint16_t i) { return P { obj[i].x + o.x, obj[i].y + o.y, obj[i].z + o.z }; };
      for (size_t i = 0; i < indices.size(); i += 3) {
        want.push_back(tri(at(indices[i]), at(indices[i + 1]), at(indices[i + 2])));
      }
    }
    CHECK(got.size() == want.size(), "%u instances: %zu triangles traced, want %zu", n, got.size(), want.size());
    std::sort(got.begin(), got.end());
    std::sort(want.begin(), want.end());
    CHECK(got == want, "%u instances: traced triangles are not every copy at its own transform", n);
  }
}

static void testLayoutEdges() {
  // One instance: exactly today's layout.
  InstancedCaptureLayout one = planInstancedCapture(4, 6, 1, true, true);
  CHECK(one.copies == 1 && one.capturedVertexCount == 4 && one.indexCount == 6 && !one.sharedSlots && !one.needs32BitIndices,
        "single instance draw changed: copies %u verts %u indices %u", one.copies, one.capturedVertexCount, one.indexCount);

  // Option off: today's behaviour, and the draw is reported as sharing slots.
  InstancedCaptureLayout off = planInstancedCapture(4, 6, 2, true, false);
  CHECK(off.copies == 1 && off.capturedVertexCount == 4 && off.indexCount == 6 && off.sharedSlots,
        "option off must keep one copy and report shared slots");

  // A strip cannot be repeated by appending indices (the copies would be joined): keep one copy, report it.
  InstancedCaptureLayout strip = planInstancedCapture(4, 4, 2, false, true);
  CHECK(strip.copies == 1 && strip.capturedVertexCount == 4 && strip.indexCount == 4 && strip.sharedSlots,
        "instanced strip must fall back to one copy and report shared slots");

  // 16-bit indices that overflow once repeated must switch to 32-bit.
  InstancedCaptureLayout big = planInstancedCapture(40000, 3, 2, true, true);
  CHECK(big.capturedVertexCount == 80000 && big.indexCount == 6 && big.needs32BitIndices,
        "40000 vertices x 2 copies: verts %u indices %u needs32 %d", big.capturedVertexCount, big.indexCount, int(big.needs32BitIndices));
  InstancedCaptureLayout small = planInstancedCapture(30000, 3, 2, true, true);
  CHECK(!small.needs32BitIndices, "60000 slots still fit 16-bit indices");

  // Non-indexed list: the copies sit back to back, no index buffer.
  InstancedCaptureLayout flat = planInstancedCapture(6, 0, 3, true, true);
  CHECK(flat.copies == 3 && flat.capturedVertexCount == 18 && flat.indexCount == 0 && flat.instanceVertexStride == 6,
        "non-indexed list x3: copies %u verts %u stride %u", flat.copies, flat.capturedVertexCount, flat.instanceVertexStride);
}

int main() {
  testInstancedCopiesKeepTheirOwnTransforms();
  testLayoutEdges();
  if (g_failures) {
    std::printf("instanced_capture_test: %d failure(s)\n", g_failures);
    return 1;
  }
  std::printf("instanced_capture_test: all passed\n");
  return 0;
}
