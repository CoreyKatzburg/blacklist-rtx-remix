// Offline checks for the Remix per-draw scene log (rtx_draw_log.h, rtx.drawLog.tag), worker C, Nov 16.
//   1. Every field lands under its own key: each field is set to a value no other field has, and the parsed line
//      must give it back (a swapped or dropped field fails).
//   2. A draw like con_boiler draw 305 (texture shows) reads t0 non-zero, t0empty 0, tc 1, drawtrack 1, instalbedo 1.
//   3. Texcoord source rules: vscapture only for a texcoord on the captured position buffer at its stride.
//   4. Arming: nothing is recorded with the tag empty; a new tag records exactly one frame; the same tag does not
//      record again until the tag has been cleared.
//   5. Cost with the tag empty (one bool test per draw) and with it set (row build + format), printed per frame.
// Exit 0 = all passed.
#include "../../src/dxvk-remix/src/dxvk/rtx_render/rtx_draw_log.h"

#include <chrono>
#include <cstdio>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

using namespace dxvk;

static int g_failed = 0;

static void expect(bool ok, const std::string& what) {
  if (!ok) {
    std::printf("FAIL %s\n", what.c_str());
    ++g_failed;
  }
}

static std::map<std::string, std::string> parse(const std::string& line) {
  std::map<std::string, std::string> kv;
  std::stringstream ss(line.substr(0, line.find('\n')));
  std::string tok;
  bool first = true;
  while (std::getline(ss, tok, '\t')) {
    if (first) {
      kv["#kind"] = tok;
      first = false;
      continue;
    }
    const size_t eq = tok.find('=');
    kv[tok.substr(0, eq)] = eq == std::string::npos ? "" : tok.substr(eq + 1);
  }
  return kv;
}

static std::string hex16(uint64_t v) {
  char b[32];
  std::snprintf(b, sizeof(b), "%016llX", (unsigned long long) v);
  return b;
}

static void testEveryFieldHasItsOwnKey() {
  DrawLogRow r;
  r.order = 0;  // set by DrawLog::add; formatted as given
  r.dcid = 305;
  r.ext = true;
  r.cam = 4;
  r.ignored = true;
  r.verts = 1201;
  r.idx = 2403;  // prims = 801
  r.geom = 0x1111111111111111ull;
  r.mat = 0x2222222222222222ull;
  r.rmat = 0x3333333333333333ull;
  r.rtype = 2;
  r.t0 = 0x4444444444444444ull;
  r.t0empty = false;
  r.t0valid = true;
  r.t1 = 0x5555555555555555ull;
  r.tc = true;
  r.tcsrc = DrawLogTexcoordSource::Texgen;
  r.tcfmt = 103;
  r.tcstride = 36;
  r.tcoff = 12;
  r.ralbedo = 1;
  r.inst = 777;
  r.instmat = 0x6666666666666666ull;
  r.instalbedo = 0;
  r.blasmat = 0x7777777777777777ull;
  r.capmesh = 0x8888888888888888ull;
  r.cap = true;
  r.capmat = 0;
  r.capmeshmat = 1;
  r.capmeshmathash = 0x9999999999999999ull;
  const auto kv = parse(formatDrawLogRow(r));
  const std::vector<std::pair<std::string, std::string>> want = {
    {"#kind", "remixdraw"}, {"order", "0"}, {"dcid", "305"}, {"ext", "1"}, {"cam", "4"}, {"ignored", "1"},
    {"verts", "1201"}, {"idx", "2403"}, {"prims", "801"},
    {"geom", hex16(r.geom)}, {"mat", hex16(r.mat)}, {"rmat", hex16(r.rmat)}, {"rtype", "2"},
    {"t0", hex16(r.t0)}, {"t0empty", "0"}, {"t0valid", "1"}, {"t1", hex16(r.t1)},
    {"tc", "1"}, {"tcsrc", "texgen"}, {"tcfmt", "103"}, {"tcstride", "36"}, {"tcoff", "12"},
    {"ralbedo", "1"}, {"drawtrack", "1"}, {"inst", "777"}, {"instmat", hex16(r.instmat)}, {"instalbedo", "0"},
    {"blasmat", hex16(r.blasmat)}, {"capmesh", hex16(r.capmesh)}, {"cap", "1"}, {"capmat", "0"},
    {"capmeshmat", "1"}, {"capmeshmathash", hex16(r.capmeshmathash)},
  };
  for (const auto& [k, v] : want) {
    const auto it = kv.find(k);
    expect(it != kv.end() && it->second == v,
           "field " + k + ": want " + v + ", got " + (it == kv.end() ? std::string("<missing>") : it->second));
  }
  expect(kv.size() == want.size(), "line has " + std::to_string(kv.size()) + " keys, want " + std::to_string(want.size()));
  // A non-trivial bool may not read the same as its neighbours: flip each one alone.
  DrawLogRow z;
  z.t0empty = true;
  z.t0valid = false;
  z.tc = false;
  z.ralbedo = 0;
  const auto kz = parse(formatDrawLogRow(z));
  expect(kz.at("t0empty") == "1" && kz.at("t0valid") == "0" && kz.at("tc") == "0" && kz.at("drawtrack") == "0" &&
         kz.at("tcsrc") == "none" && kz.at("instalbedo") == "-1" && kz.at("capmat") == "-1" && kz.at("cap") == "0",
         "default row reads empty / no texcoords / not tracked / no capture");
  DrawLogRow noTc = r;
  noTc.tc = false;  // albedo valid but no texcoords: trackTexture returns without tracking
  expect(parse(formatDrawLogRow(noTc)).at("drawtrack") == "0", "albedo without texcoords must read drawtrack 0");
  DrawLogRow noAlbedo = r;
  noAlbedo.ralbedo = 0;
  expect(parse(formatDrawLogRow(noAlbedo)).at("drawtrack") == "0", "texcoords without albedo must read drawtrack 0");
}

static void testDraw305Shape() {
  // con_boiler draw 305 shows texture 2E0101B8 correctly: a vertex-shader draw whose texcoords come from capture.
  DrawLogRow r;
  r.t0 = 0x2E0101B8AABBCCDDull;
  r.mat = r.t0;
  r.t0empty = false;
  r.t0valid = true;
  r.tc = true;
  r.tcsrc = classifyDrawLogTexcoords(true, false, true, true, true, true);
  r.ralbedo = 1;
  r.instalbedo = 1;
  const auto kv = parse(formatDrawLogRow(r));
  expect(kv.at("t0") != hex16(0) && kv.at("t0empty") == "0" && kv.at("tc") == "1" && kv.at("tcsrc") == "vscapture" &&
         kv.at("drawtrack") == "1" && kv.at("instalbedo") == "1",
         "draw-305 shape: non-empty t0, texcoords from capture, albedo tracked");
}

static void testTexcoordSource() {
  // (defined, texgen, usesVs, sameBuffer, sameStride, posCaptured)
  expect(classifyDrawLogTexcoords(true, false, true, true, true, true) == DrawLogTexcoordSource::VsCapture, "capture");
  expect(classifyDrawLogTexcoords(true, false, false, true, true, true) == DrawLogTexcoordSource::Decl, "fixed function");
  expect(classifyDrawLogTexcoords(true, false, true, false, true, true) == DrawLogTexcoordSource::Decl, "other buffer");
  expect(classifyDrawLogTexcoords(true, false, true, true, false, true) == DrawLogTexcoordSource::Decl, "other stride");
  // An interleaved game vertex buffer: texcoord shares the position buffer and stride, but positions are not captured.
  expect(classifyDrawLogTexcoords(true, false, true, true, true, false) == DrawLogTexcoordSource::Decl, "interleaved decl");
  expect(classifyDrawLogTexcoords(false, true, false, false, false, false) == DrawLogTexcoordSource::Texgen, "texgen");
  expect(classifyDrawLogTexcoords(false, false, true, false, false, true) == DrawLogTexcoordSource::None, "none");
}

static size_t countLines(const std::string& path, std::string* head) {
  std::ifstream f(path);
  std::string line;
  size_t n = 0;
  while (std::getline(f, line)) {
    if (n == 0 && head) {
      *head = line;
    }
    ++n;
  }
  return n;
}

static void testArming(const std::filesystem::path& dir) {
  DrawLog log;
  log.armIfNewTag("");
  expect(!log.active(), "empty tag arms nothing");
  expect(log.write(dir, 1, 0.0).empty(), "write with nothing armed writes nothing");
  log.armIfNewTag("t1");
  expect(log.active(), "a new tag arms the next frame");
  for (uint32_t i = 0; i < 3; ++i) {
    DrawLogRow r;
    r.dcid = 10 + i;
    log.add(r);
  }
  const std::string path = log.write(dir, 42, 1234.5);
  expect(!path.empty() && !log.active(), "the armed frame is written and the log disarms");
  std::string head;
  expect(countLines(path, &head) == 4, "header plus 3 rows");
  const auto kv = parse(head);
  expect(kv.at("#kind") == "remixframe" && kv.at("frame") == "42" && kv.at("draws") == "3" && kv.at("tag") == "t1" &&
         kv.at("qpcMs") == "1234.500", "header: " + head);
  std::ifstream f(path);
  std::string line;
  std::getline(f, line);
  for (int i = 0; i < 3; ++i) {
    std::getline(f, line);
    const auto row = parse(line);
    expect(row.at("order") == std::to_string(i) && row.at("dcid") == std::to_string(10 + i), "row order " + line);
  }
  f.close();
  log.armIfNewTag("t1");
  expect(!log.active(), "the same tag does not record again");
  log.armIfNewTag("");
  log.armIfNewTag("t1");
  expect(log.active(), "the same tag after a clear records again");
  log.write(dir, 43, 0.0);
  std::filesystem::remove(path);
  expect(!std::filesystem::exists(dir / "t1_remix_draws.tsv.tmp"), "no temporary file left behind");
}

static void costs() {
  // Key off: processDrawCallState pays one bool test per draw; onFrameEnd one empty-string test.
  DrawLog off;
  volatile bool sink = false;
  const int kDraws = 1600;  // con_boiler f5690 has 1580 draws in the proxy dump
  const int kFrames = 200000;
  auto t0 = std::chrono::steady_clock::now();
  for (int f = 0; f < kFrames; ++f) {
    for (int d = 0; d < kDraws; ++d) {
      if (off.active()) {
        off.add(DrawLogRow());
      }
      sink = sink ^ off.active();
    }
    off.armIfNewTag(std::string());
  }
  const double offMsPerFrame =
    std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count() / kFrames;
  // Key on, one frame: add every row, then format and write the file.
  DrawLog on;
  const std::filesystem::path dir = std::filesystem::temp_directory_path() / "draw_log_cost";
  const int kOnFrames = 20;
  double addMs = 0.0, writeMs = 0.0;
  for (int f = 0; f < kOnFrames; ++f) {
    on.armIfNewTag("cost" + std::to_string(f));
    auto a = std::chrono::steady_clock::now();
    for (int d = 0; d < kDraws; ++d) {
      DrawLogRow r;
      r.dcid = d;
      r.t0 = 0x1234567812345678ull * (d + 1);
      on.add(r);
    }
    auto b = std::chrono::steady_clock::now();
    on.write(dir, f, 0.0);
    auto c = std::chrono::steady_clock::now();
    addMs += std::chrono::duration<double, std::milli>(b - a).count();
    writeMs += std::chrono::duration<double, std::milli>(c - b).count();
  }
  std::error_code ec;
  std::filesystem::remove_all(dir, ec);
  std::printf("cost key off: %.5f ms per frame of %d draws (%.2f ns per draw)\n", offMsPerFrame, kDraws,
              offMsPerFrame * 1e6 / kDraws);
  std::printf("cost key on, the one logged frame: rows %.3f ms + format and write %.3f ms (%d draws)\n",
              addMs / kOnFrames, writeMs / kOnFrames, kDraws);
}

int main(int argc, char** argv) {
  const std::filesystem::path dir = std::filesystem::temp_directory_path() / "draw_log_test";
  std::filesystem::create_directories(dir);
  testEveryFieldHasItsOwnKey();
  testDraw305Shape();
  testTexcoordSource();
  testArming(dir);
  std::error_code ec;
  std::filesystem::remove_all(dir, ec);
  if (!(argc > 1 && std::string(argv[1]) == "--no-cost")) {
    costs();
  }
  std::printf("draw_log_test: %s\n", g_failed ? "FAIL" : "PASS");
  return g_failed ? 1 : 0;
}
