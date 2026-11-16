// HitchSampler Present tag test (x86, like the proxy). Runs the real sampler from proxy_diagnostics.cpp with a fake
// Present through proxy::CallPresentTagged, the wrapper HookPresent uses, then reads d3d9_proxy_hitches.txt:
//   window 1: a 150 ms stall inside the fake Present  -> the render thread's samples must be >= 80% "present"
//   window 2: a 150 ms stall after Present returned   -> the render thread's samples must be <= 20% "present"
// Built with /DHITCH_TAG_MUTANT the fake Present is called without the tag; the runner requires that build to FAIL.
#include <windows.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "present_tag.h"
#include "proxy_diagnostics.h"
#include "proxy_log.h"

namespace {

struct RenderWindow {
    double dt = 0;
    unsigned samples = 0;
    unsigned present = 0;
};

HRESULT FakePresent(DWORD sleepMs) {
    Sleep(sleepMs);
    return S_OK;
}

HRESULT Present(DWORD sleepMs) {
#ifdef HITCH_TAG_MUTANT
    return FakePresent(sleepMs);
#else
    return proxy::CallPresentTagged([&] { return FakePresent(sleepMs); });
#endif
}

std::wstring HitchPath() {
    wchar_t path[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, path, MAX_PATH);
    std::wstring p(path);
    return p.substr(0, p.find_last_of(L"\\/") + 1) + L"d3d9_proxy_hitches.txt";
}

// The render thread (role R) line of each HITCH record, in file order.
std::vector<RenderWindow> ReadWindows(const std::wstring& path) {
    std::vector<RenderWindow> out;
    FILE* f = nullptr;
    if (_wfopen_s(&f, path.c_str(), L"r") != 0 || !f) return out;
    char line[1024];
    double dt = 0;
    while (fgets(line, sizeof(line), f)) {
        unsigned frame = 0;
        double start = 0;
        if (sscanf_s(line, "HITCH frame %u start %lf ms dt %lf ms", &frame, &start, &dt) == 3) continue;
        unsigned long id = 0;
        char role = 0;
        unsigned samples = 0;
        double cpu = 0;
        if (sscanf_s(line, "  thread %lu %c samples %u cpu %lf", &id, &role, 1, &samples, &cpu) == 4 && role == 'R') {
            RenderWindow w;
            w.dt = dt;
            w.samples = samples;
            const char* p = strstr(line, "present=");
            if (p) w.present = static_cast<unsigned>(atoi(p + 8));
            out.push_back(w);
        }
    }
    fclose(f);
    return out;
}

}  // namespace

int main() {
    const std::wstring path = HitchPath();
    DeleteFileW(path.c_str());

    proxy::GetExecutableDirectory();  // WriteHitch puts the file next to the exe, as in the game
    proxy::InitDiagnosticsQpc();
    proxy::DiagnosticsConfig cfg;
    cfg.hitchSampler = true;
    cfg.sampleMs = 5;
    cfg.hitchMs = 50.0;
    proxy::ConfigureDiagnostics(&cfg);

    // Frame k: Present entry (HitchSamplerFrame), Present, then work after it. Gaps over 50 ms become windows.
    // Short steps are 0 ms, not a few ms: Sleep rounds up to the 15.6 ms timer tick, which would put about 10% of
    // each window on the wrong side of Present and leave the 80% / 20% limits with no margin.
    const DWORD insidePresent[] = {0, 0, 200, 0, 0, 0, 0};
    const DWORD afterPresent[] = {0, 0, 0, 0, 200, 0, 0};
    for (int k = 0; k < 7; ++k) {
        LARGE_INTEGER entry;
        QueryPerformanceCounter(&entry);
        proxy::HitchSamplerFrame(entry.QuadPart);
        Present(insidePresent[k]);
        Sleep(afterPresent[k]);
    }
    Sleep(500);  // the sampler thread writes the records

    const std::vector<RenderWindow> w = ReadWindows(path);
    DeleteFileW(path.c_str());
    if (w.size() != 2) {
        printf("FAIL: expected 2 hitch windows with a render-thread line, found %zu\n", w.size());
        return 1;
    }
    const double inShare = w[0].samples ? double(w[0].present) / w[0].samples : 0;
    const double outShare = w[1].samples ? double(w[1].present) / w[1].samples : 1;
    printf("stall inside Present: dt %.1f ms, render thread %u/%u samples tagged present (%.2f)\n", w[0].dt,
           w[0].present, w[0].samples, inShare);
    printf("stall after Present:  dt %.1f ms, render thread %u/%u samples tagged present (%.2f)\n", w[1].dt,
           w[1].present, w[1].samples, outShare);
    bool ok = true;
    if (w[0].samples < 3 || inShare < 0.8) { printf("FAIL: the stall inside Present is not tagged\n"); ok = false; }
    if (w[1].samples < 3 || outShare > 0.2) { printf("FAIL: the stall after Present is tagged as Present\n"); ok = false; }
    printf(ok ? "PASS\n" : "FAIL\n");
    fflush(stdout);
    ExitProcess(ok ? 0 : 1);  // the sampler thread never returns
}
