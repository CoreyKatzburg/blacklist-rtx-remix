// HitchSampler cost benchmark (x86, like the proxy). Not a pass/fail test: it measures what the real sampler from
// proxy_diagnostics.cpp costs a game-like render thread per frame, so turning it on in gate launches is a number.
//   hitch_cost_bench.exe on|off [frames] [workIterations]
// A frame is: Present entry (HitchSamplerFrame when on), a 1 ms fake Present through proxy::CallPresentTagged,
// then a fixed amount of arithmetic. Around it run 40 idle threads and 3 threads at about 50% duty, so the
// sampler has a game-like thread list to walk. Prints the median, p95 and mean frame time in ms, and the
// process CPU time per second. Run on and off alternately (bench_hitch_cost.ps1) and compare the medians.
#include <windows.h>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#include "present_tag.h"
#include "proxy_diagnostics.h"
#include "proxy_log.h"

namespace {

volatile double g_sink = 0;
volatile LONG g_stop = 0;

double Seconds(LONGLONG ticks) {
    static LARGE_INTEGER freq = [] { LARGE_INTEGER f; QueryPerformanceFrequency(&f); return f; }();
    return double(ticks) / double(freq.QuadPart);
}

void Work(unsigned iterations) {
    double x = 1.0001;
    for (unsigned i = 0; i < iterations; ++i) x = x * 1.0000001 + 0.0000001;
    g_sink = x;
}

void SpinMs(double ms) {
    LARGE_INTEGER a, b;
    QueryPerformanceCounter(&a);
    do { Work(200); QueryPerformanceCounter(&b); } while (Seconds(b.QuadPart - a.QuadPart) * 1000.0 < ms);
}

DWORD WINAPI IdleThread(void* e) { WaitForSingleObject(static_cast<HANDLE>(e), INFINITE); return 0; }
DWORD WINAPI HalfBusyThread(void*) {
    while (!g_stop) { SpinMs(2.0); Sleep(2); }
    return 0;
}

double ProcessCpuSeconds() {
    FILETIME c, e, k, u;
    GetProcessTimes(GetCurrentProcess(), &c, &e, &k, &u);
    auto v = [](FILETIME f) { return (ULONGLONG(f.dwHighDateTime) << 32 | f.dwLowDateTime) / 1e7; };
    return v(k) + v(u);
}

}  // namespace

int main(int argc, char** argv) {
    const bool on = argc > 1 && strcmp(argv[1], "on") == 0;
    const int frames = argc > 2 ? atoi(argv[2]) : 600;
    const unsigned work = argc > 3 ? static_cast<unsigned>(strtoul(argv[3], nullptr, 10)) : 20000000u;

    proxy::GetExecutableDirectory();
    proxy::InitDiagnosticsQpc();
    proxy::DiagnosticsConfig cfg;
    cfg.hitchSampler = on;
    cfg.sampleMs = 5;
    cfg.hitchMs = 50.0;
    proxy::ConfigureDiagnostics(&cfg);

    HANDLE idle = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    std::vector<HANDLE> threads;
    for (int i = 0; i < 40; ++i) threads.push_back(CreateThread(nullptr, 0, IdleThread, idle, 0, nullptr));
    for (int i = 0; i < 3; ++i) threads.push_back(CreateThread(nullptr, 0, HalfBusyThread, nullptr, 0, nullptr));

    std::vector<double> ms;
    ms.reserve(frames);
    const double cpu0 = ProcessCpuSeconds();
    LARGE_INTEGER t0, t1;
    QueryPerformanceCounter(&t0);
    for (int f = 0; f < frames + 30; ++f) {  // the first 30 frames warm up and are not counted
        LARGE_INTEGER entry, done;
        QueryPerformanceCounter(&entry);
        if (on) proxy::HitchSamplerFrame(entry.QuadPart);
        proxy::CallPresentTagged([] { SpinMs(1.0); return S_OK; });
        Work(work);
        QueryPerformanceCounter(&done);
        if (f >= 30) ms.push_back(Seconds(done.QuadPart - entry.QuadPart) * 1000.0);
    }
    QueryPerformanceCounter(&t1);
    const double wall = Seconds(t1.QuadPart - t0.QuadPart);
    const double cpu = ProcessCpuSeconds() - cpu0;

    std::vector<double> sorted = ms;
    std::sort(sorted.begin(), sorted.end());
    double mean = 0;
    for (double v : ms) mean += v;
    mean /= ms.size();
    printf("sampler %s: frames %zu median %.3f ms p95 %.3f ms mean %.3f ms, process cpu %.3f s per s\n",
           on ? "on " : "off", ms.size(), sorted[sorted.size() / 2], sorted[sorted.size() * 95 / 100], mean,
           cpu / wall);
    fflush(stdout);
    InterlockedExchange(&g_stop, 1);
    // A hitch file may exist if a frame went over 50 ms; it is not part of the measurement.
    wchar_t path[MAX_PATH];
    GetModuleFileNameW(nullptr, path, MAX_PATH);
    wchar_t* slash = wcsrchr(path, L'\\');
    if (slash) { wcscpy_s(slash + 1, MAX_PATH - (slash + 1 - path), L"d3d9_proxy_hitches.txt"); DeleteFileW(path); }
    ExitProcess(0);  // the sampler thread never returns
}
