// Regression test for the 2026-11-12 freeze: the Remix server stopped for 5 minutes, then turned the
// bridge off, the first time its reply data queue (Server2Client) wrapped around.
//
// Built x64 with REMIX_BRIDGE_SERVER and linked with the bridge's libutil, so the real
// Bridge::Command / syncDataQueue server code runs. A reader thread plays the 32-bit game: it reads
// each reply before the next one is written and, like the real client, never publishes its read
// position. The replies wrap the queue many times. Every reply must arrive intact, and no reply may
// take more than 2 s (the bug waited 300 s per wrap). A watchdog fails the run after 30 s.

#include "util_devicecommand.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <thread>
#include <vector>

// Globals the real server defines in server/main.cpp. The GUID keeps the shared-memory names unique.
bool gbBridgeRunning = true;
bridge_util::Guid gUniqueIdentifier;

static int g_failures = 0;
#define CHECK(cond, ...) do { if (!(cond)) { ++g_failures; std::printf("FAIL line %d: ", __LINE__); std::printf(__VA_ARGS__); std::printf("\n"); } } while (0)

static constexpr size_t kMemSize = 64 << 10;  // 64 KB channel, so a few replies wrap it
static constexpr size_t kCmdQueueSize = 64;
static constexpr size_t kDataQueueSize = 25;
static constexpr uint32_t kReplies = 400;
static constexpr uint32_t kBlobBytes = 3000;  // 400 x 3 KB = about 18 wraps of the data queue

static uint8_t pattern(uint32_t reply, uint32_t byte) { return static_cast<uint8_t>(reply * 31u + byte); }

int main() {
  std::thread watchdog([] {
    std::this_thread::sleep_for(std::chrono::seconds(30));
    std::printf("FAIL: watchdog fired (reply queue wait, the 2026-11-12 freeze)\n");
    std::fflush(stdout);
    std::_Exit(2);
  });
  watchdog.detach();

  // As server/main.cpp does. Without it commandTimeout and commandRetries are 0 and no wait can block.
  bridge_util::Config::init(bridge_util::Config::App::Server);
  GlobalOptions::init();
  std::printf("options: semaphoreTimeout %u ms, commandRetries %u\n",
              GlobalOptions::getSemaphoreTimeout(), GlobalOptions::getCommandRetries());
  CHECK(GlobalOptions::getCommandRetries() > 0 && GlobalOptions::getSemaphoreTimeout() > 0,
        "bridge options must load real timeouts for this test to mean anything");

  const std::string base = "BridgeReplyWrapTest" + std::to_string(GetCurrentProcessId());
  DeviceBridge::init(base, kMemSize, kCmdQueueSize, kDataQueueSize, kMemSize, kCmdQueueSize, kDataQueueSize);
  ReaderChannel client(base + "Server2Client", kMemSize, kCmdQueueSize, kDataQueueSize);

  std::atomic<uint32_t> consumed { 0 };
  std::atomic<bool> readerOk { true };
  std::thread reader([&] {
    for (uint32_t i = 0; i < kReplies; i++) {
      bridge_util::Result r = bridge_util::Result::Failure;
      const Header h = client.commands->pull(r, 5000);
      if (r != bridge_util::Result::Success || h.command != Commands::Bridge_Response || h.pHandle != i) {
        std::printf("FAIL reader: reply %u header missing or wrong (handle %u)\n", i, static_cast<uint32_t>(h.pHandle));
        readerOk = false;
        return;
      }
      void* blob = nullptr;
      const uint32_t size = client.data->pull(&blob);
      bool intact = (size == kBlobBytes) && blob;
      for (uint32_t b = 0; intact && b < kBlobBytes; b++) {
        intact = static_cast<const uint8_t*>(blob)[b] == pattern(i, b);
      }
      if (!intact) {
        std::printf("FAIL reader: reply %u data corrupt (size %u)\n", i, size);
        readerOk = false;
        return;
      }
      consumed = i + 1;
    }
  });

  std::vector<uint8_t> blob(kBlobBytes);
  double worstMs = 0;
  for (uint32_t i = 0; i < kReplies && readerOk; i++) {
    for (uint32_t b = 0; b < kBlobBytes; b++) blob[b] = pattern(i, b);
    const auto start = std::chrono::steady_clock::now();
    {
      DeviceBridge::Command reply(Commands::Bridge_Response, i);
      reply.send_data(kBlobBytes, blob.data());
    }
    const double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
    worstMs = ms > worstMs ? ms : worstMs;
    // The game reads each reply before it sends its next request.
    while (consumed.load() <= i && readerOk) std::this_thread::yield();
  }
  reader.join();

  CHECK(readerOk.load(), "the game side did not receive every reply intact");
  CHECK(gbBridgeRunning, "the bridge turned itself off");
  CHECK(worstMs < 2000.0, "a reply took %.0f ms to send (writer waited on a reader that never signals)", worstMs);
  std::printf("bridge_reply_wrap_test: %u replies, %u data-queue slots, worst send %.2f ms\n",
              kReplies, static_cast<uint32_t>(DeviceBridge::getWriterChannel().data->get_total_size()), worstMs);
  std::printf(g_failures ? "bridge_reply_wrap_test: %d FAILURES\n" : "bridge_reply_wrap_test: PASS\n", g_failures);
  return g_failures ? 1 : 0;
}
