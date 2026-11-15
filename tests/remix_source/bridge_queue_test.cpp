// Tests for bridge command batching in bridge/src/util/util_atomiccircularqueue.h.
// Built x64 against the real header and linked with the bridge's libutil (GlobalOptions, logging).
//
// Checks:
//   1. Batched (staged) commands stay invisible to the server until a non-batched push publishes them.
//   2. Staging publishes by itself after kMaxStagedElements.
//   3. Order and content survive a tiny queue under concurrent use (producer and consumer threads),
//      including long runs of batched-only pushes that fill the queue (no deadlock).
//   4. pull() returns the element even when the producer immediately reuses the freed slot.
// A watchdog fails the run if anything blocks for more than 30 s.

#include "util_bridgecommand.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <thread>
#include <vector>

using Writer = bridge_util::AtomicCircularQueue<Header, bridge_util::Accessor::Writer>;
using Reader = bridge_util::AtomicCircularQueue<Header, bridge_util::Accessor::Reader>;

static int g_failures = 0;
#define CHECK(cond, ...) do { if (!(cond)) { ++g_failures; std::printf("FAIL line %d: ", __LINE__); std::printf(__VA_ARGS__); std::printf("\n"); } } while (0)

struct SharedQueue {
  explicit SharedQueue(size_t queueSize)
    : size(Writer::getExtraMemoryRequirements() + queueSize * sizeof(Header) + 256)
    , memory(static_cast<uint8_t*>(_aligned_malloc(size, 256)))
    , writer(std::make_unique<Writer>("test", memory, size, queueSize))
    , reader(std::make_unique<Reader>("test", memory, size, queueSize)) { }
  ~SharedQueue() { writer.reset(); reader.reset(); _aligned_free(memory); }
  size_t size;
  uint8_t* memory;
  std::unique_ptr<Writer> writer;
  std::unique_ptr<Reader> reader;
};

static Header make(uint32_t seq) {
  Header h;
  h.command = Commands::IDirect3DDevice9Ex_SetRenderState;
  h.dataOffset = seq;
  h.pHandle = seq * 7u + 1u;
  return h;
}

static void testStagedInvisibleUntilPublished() {
  SharedQueue q(1024);
  for (uint32_t i = 0; i < 10; i++) {
    CHECK(q.writer->push(make(i), true) == bridge_util::Result::Success, "batched push %u failed", i);
  }
  CHECK(q.reader->isEmpty(), "staged commands must not be visible before a non-batched push");
  CHECK(q.writer->push(make(10), false) == bridge_util::Result::Success, "non-batched push failed");
  for (uint32_t i = 0; i <= 10; i++) {
    bridge_util::Result r = bridge_util::Result::Failure;
    const Header h = q.reader->pull(r, 100);
    CHECK(r == bridge_util::Result::Success && h.dataOffset == i, "expected element %u in order (got %u)", i, h.dataOffset);
  }
  CHECK(q.reader->isEmpty(), "queue should be empty");
}

static void testAutoPublishAfterLimit() {
  SharedQueue q(1024);
  const uint32_t limit = Writer::kMaxStagedElements;
  for (uint32_t i = 0; i + 1 < limit; i++) q.writer->push(make(i), true);
  CHECK(q.reader->isEmpty(), "fewer than %u staged commands stay staged", limit);
  q.writer->push(make(limit - 1), true);
  CHECK(!q.reader->isEmpty(), "the %u-th staged command publishes the batch", limit);
}

static void testConcurrentTinyQueue(uint32_t total, uint32_t batchedRun) {
  SharedQueue q(8);
  std::atomic<bool> readerOk { true };
  std::thread consumer([&] {
    for (uint32_t i = 0; i < total; i++) {
      bridge_util::Result r = bridge_util::Result::Failure;
      const Header h = q.reader->pull(r, 0);
      if (r != bridge_util::Result::Success || h.dataOffset != i || h.pHandle != i * 7u + 1u) {
        std::printf("FAIL consumer: element %u wrong (seq %u handle %u)\n", i, h.dataOffset, h.pHandle);
        readerOk = false;
        return;
      }
    }
  });
  for (uint32_t i = 0; i < total; i++) {
    // Mostly batched, with a non-batched push every `batchedRun` elements, and always at the end.
    const bool batched = (i + 1 < total) && ((i + 1) % batchedRun) != 0;
    if (q.writer->push(make(i), batched) != bridge_util::Result::Success) {
      CHECK(false, "producer push %u failed (timeout)", i);
      break;
    }
  }
  consumer.join();
  CHECK(readerOk.load(), "consumer saw wrong order or content (batchedRun %u)", batchedRun);
}

int main() {
  std::thread watchdog([] {
    std::this_thread::sleep_for(std::chrono::seconds(30));
    std::printf("FAIL: watchdog fired (deadlock)\n");
    std::fflush(stdout);
    std::_Exit(2);
  });
  watchdog.detach();

  testStagedInvisibleUntilPublished();
  testAutoPublishAfterLimit();
  testConcurrentTinyQueue(200000, 3);      // short batches
  testConcurrentTinyQueue(200000, 1000);   // batched runs far longer than the 8-slot queue
  testConcurrentTinyQueue(200000, 1);      // no batching at all

  std::printf(g_failures ? "bridge_queue_test: %d FAILURES\n" : "bridge_queue_test: PASS\n", g_failures);
  return g_failures ? 1 : 0;
}
