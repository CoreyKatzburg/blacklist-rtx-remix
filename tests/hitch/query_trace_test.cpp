// Checks play-setup\unified-build\query_trace.h (worker B, X3: [Proxy] QueryTraceEvery and PresentGrabFrames).
// Built by run_query_trace_test.ps1: the normal build must PASS; each mutant must FAIL.
// Usage: query_trace_test.exe [grab.pfm]  With a path, it also grabs a 64x48 test pattern on a real D3D9 device to
// that file (the script checks the file with numpy); exit 2 when no device can be made.
// Exit code 0 = all cases pass, 1 = a case failed, 2 = no D3D9 device.
#include "query_trace.h"

#include <cstdio>
#include <string>
#include <vector>

namespace {
int g_failed = 0;

void Expect(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) ++g_failed;
}

bool Has(const std::vector<std::string>& lines, const char* text) {
    for (const std::string& l : lines) if (l.find(text) != std::string::npos) return true;
    return false;
}

const void* Q(uintptr_t v) { return reinterpret_cast<const void*>(v); }

int GrabOnDevice(const wchar_t* path, const wchar_t* hdrPath) {
    WNDCLASSW wc = {};
    wc.lpfnWndProc = DefWindowProcW;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = L"query_trace_test";
    RegisterClassW(&wc);
    HWND window = CreateWindowW(wc.lpszClassName, L"", WS_OVERLAPPEDWINDOW, 0, 0, 64, 64, nullptr, nullptr,
                                wc.hInstance, nullptr);
    IDirect3D9* d3d = Direct3DCreate9(D3D_SDK_VERSION);
    D3DPRESENT_PARAMETERS pp = {};
    pp.Windowed = TRUE;
    pp.SwapEffect = D3DSWAPEFFECT_DISCARD;
    pp.BackBufferWidth = pp.BackBufferHeight = 16;
    pp.BackBufferFormat = D3DFMT_UNKNOWN;
    pp.hDeviceWindow = window;
    IDirect3DDevice9* device = nullptr;
    if (!d3d || FAILED(d3d->CreateDevice(D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, window,
                                         D3DCREATE_SOFTWARE_VERTEXPROCESSING, &pp, &device))) {
        std::printf("no D3D9 device\n");
        return 2;
    }
    // 64x48 target of each format the grab reads (X8R8G8B8, and A16B16G16R16F like the game's scene HDR target),
    // black, with three marks in 8x8 cells (cell column, cell row, top row first):
    //   cell (0,0): a 1-pixel white checkerboard (box average 0.5, point sampling 0 or 1)
    //   cell (1,2): solid red (R 1, G 0, B 0; checks channel order and the row order)
    //   cell (3,0): a white 4x4 square in its top-left corner (box average 0.25)
    int result = 0;
    const D3DFORMAT formats[2] = {D3DFMT_X8R8G8B8, D3DFMT_A16B16G16R16F};
    const wchar_t* paths[2] = {path, hdrPath};
    for (int i = 0; i < 2; ++i) {
        IDirect3DSurface9* rt = nullptr;
        if (FAILED(device->CreateRenderTarget(64, 48, formats[i], D3DMULTISAMPLE_NONE, 0, FALSE, &rt, nullptr))) {
            std::printf("FAIL no 64x48 render target of format %d\n", static_cast<int>(formats[i]));
            result = 1;
            continue;
        }
        device->ColorFill(rt, nullptr, D3DCOLOR_XRGB(0, 0, 0));
        for (LONG y = 0; y < 8; ++y)
            for (LONG x = (y & 1); x < 8; x += 2) {
                const RECT r = {x, y, x + 1, y + 1};
                device->ColorFill(rt, &r, D3DCOLOR_XRGB(255, 255, 255));
            }
        const RECT red = {8, 16, 16, 24};
        device->ColorFill(rt, &red, D3DCOLOR_XRGB(255, 0, 0));
        const RECT quarter = {24, 0, 28, 4};
        device->ColorFill(rt, &quarter, D3DCOLOR_XRGB(255, 255, 255));
        const int fail = proxy::WriteGrabPfm(device, rt, paths[i]);
        std::printf("%s grab of format %d written (step %d)\n", fail ? "FAIL" : "ok  ", static_cast<int>(formats[i]),
                    fail);
        if (fail) result = 1;
        rt->Release();
    }
    // A format the grab does not read must be refused, not written.
    IDirect3DSurface9* other = nullptr;
    if (SUCCEEDED(device->CreateRenderTarget(64, 48, D3DFMT_R5G6B5, D3DMULTISAMPLE_NONE, 0, FALSE, &other, nullptr))) {
        const bool refused = proxy::WriteGrabPfm(device, other, L"query_trace_test_r5g6b5.pfm") == 1;
        std::printf("%s an R5G6B5 target is refused\n", refused ? "ok  " : "FAIL");
        if (!refused) result = 1;
        other->Release();
    }
    device->Release();
    d3d->Release();
    DestroyWindow(window);
    return result;
}
}  // namespace

int main(int argc, char** argv) {
    using namespace proxy;

    // Keys: off unless set.
    Expect(ParseQueryTraceEvery(0) == 0 && ParseQueryTraceEvery(-3) == 0, "QueryTraceEvery 0 or negative is off");
    Expect(ParseQueryTraceEvery(15) == 15, "QueryTraceEvery 15 kept");
    Expect(ParseQueryTraceEvery(5000) == kQueryTraceMaxEvery, "QueryTraceEvery clamps to 600");
    Expect(ParsePresentGrabFrames(0) == 0 && ParsePresentGrabFrames(-1) == 0, "PresentGrabFrames 0 or negative is off");
    Expect(ParsePresentGrabFrames(8) == 8 && ParsePresentGrabFrames(99) == kPresentGrabMaxFrames,
           "PresentGrabFrames 8 kept, 99 clamps to 16");

    std::vector<std::string> lines;
    QueryDrawState stA;
    stA.ds = 0x1A2011B0; stA.dsW = 2560; stA.dsH = 1440; stA.dsFmt = 75; stA.zen = 1; stA.zfn = 4; stA.zwr = 0;
    stA.cw = 0; stA.rt0 = 0x1A222ED0;
    QueryDrawState stB = stA;
    stB.ds = 0x55555550; stB.zfn = 8;

    // Off path: every 0 traces nothing and writes nothing, whatever the calls.
    {
        QueryTrace* off = new QueryTrace();
        off->BeginFrame(0);
        off->OnDroppedDraw(0x1A2011B0, true);
        off->OnBegin(Q(0x100), true, &lines);
        off->OnDraw(true, "indexed-up", off->WantsDrawState() ? &stA : nullptr);
        off->OnEnd(Q(0x100));
        off->OnHanded(Q(0x100), 9);
        off->OnBegin(Q(0x100), true, &lines);
        off->EndFrame(&lines);
        Expect(!off->Enabled() && !off->TracingFrame() && lines.empty(), "every 0: no traced frame, no line");
        delete off;
    }

    QueryTrace* t = new QueryTrace();
    t->SetEvery(15);
    t->BeginFrame(14);
    Expect(!t->TracingFrame(), "every 15: frame 14 not traced");
    t->BeginFrame(1);
    Expect(!t->TracingFrame(), "every 15: frame 1 not traced");

    // Frame 15, the real call order: drops and a scene draw before the first query, then a game query with two draws,
    // then a drop after it and a query the game never read.
    t->BeginFrame(15);
    Expect(t->TracingFrame(), "every 15: frame 15 traced");
    Expect(t->WantsDropState(), "before the first query the trace wants dropped draws");
    t->OnDroppedDraw(0x1A2011B0, true);    // depth writer into the scene surface
    t->OnDroppedDraw(0x30000000, true);    // depth writer into another surface
    t->OnDroppedDraw(0x1A2011B0, false);   // dropped, no depth write
    t->OnSceneDraw(0x1A2011B0);
    t->OnSceneDraw(0x30000000);            // only the first scene draw names the scene surface
    Expect(!t->WantsDrawState(), "no draw state wanted outside a query");
    t->OnBegin(Q(0x100), true, &lines);
    Expect(t->WantsDrawState() && !t->WantsDropState(), "inside the query: draw state wanted, drops no longer");
    t->OnDraw(true, "indexed-up", t->WantsDrawState() ? &stA : nullptr);
    Expect(!t->WantsDrawState(), "after the first draw no more state is read");
    t->OnDraw(false, "draw", &stB);         // a second draw (even with state passed) must not replace the first
    t->OnEnd(Q(0x100));
    t->OnDroppedDraw(0x1A2011B0, true);     // after the first query: not counted
    t->OnBegin(Q(0x200), false, &lines);    // never read by the game: not traced
    t->OnDraw(true, "indexed-up", t->WantsDrawState() ? &stA : nullptr);
    t->OnEnd(Q(0x200));
    t->EndFrame(&lines);
    Expect(lines.size() == 1 && Has(lines, "QueryTraceFrame: frame 15 queries 1 scene-ds 1A2011B0 "
                                           "dropped-before-first-query 3 z-write on scene ds 1 other ds 1"),
           "frame line: 1 query, scene ds, 3 drops before it, 1 depth writer on the scene ds, 1 on another");
    lines.clear();

    // Frame 16 (not traced): answers, then the game reissues the query.
    t->BeginFrame(16);
    t->OnHanded(Q(0x200), 99);             // another query's answer
    t->OnFresh(Q(0x100), 0);
    t->OnHanded(Q(0x100), 5);
    t->OnHanded(Q(0x100), 0);
    t->OnFresh(Q(0x100), 7);               // counted, but only the first fresh value is kept
    t->OnBegin(Q(0x100), true, &lines);
    Expect(lines.size() == 1, "reissue closes the record with one line");
    char want[512];
    std::snprintf(want, sizeof(want),
                  "QueryTrace: frame 15 q %p draws 2 sent 1 kind indexed-up ds 1A2011B0 2560x1440 f75 zen 1 zfn 4 zwr 0 "
                  "cw 0 rt 1A222ED0 pos 0 0.0 0.0 0.0 0.0 0.0 0.0 handed 2 first 5 last 0 fresh 2 first 0 end reissue",
                  Q(0x100));
    Expect(Has(lines, want), "the line holds the first draw's state and the answers of this query only");
    if (!Has(lines, want)) std::printf("     got: %s\n     want: %s\n", lines.empty() ? "" : lines[0].c_str(), want);
    lines.clear();
    t->EndFrame(&lines);
    Expect(lines.empty(), "an untraced frame writes no frame line");

    // A traced query that is never issued again closes after 120 frames.
    t->BeginFrame(30);
    t->OnBegin(Q(0x300), true, &lines);
    t->OnEnd(Q(0x300));
    lines.clear();
    for (uint32_t f = 31; f < 150; ++f) { t->BeginFrame(f); t->EndFrame(&lines); }
    Expect(!Has(lines, "end timeout"), "still open 119 frames later");
    t->BeginFrame(150);
    t->EndFrame(&lines);
    Expect(Has(lines, "QueryTrace: frame 30") && Has(lines, "handed 0 first 0 last 0 fresh 0 first 0 end timeout"),
           "closed with no answer at 120 frames");
    delete t;

    // Positions: box of float3 at offset 0, stride 32; a NaN vertex is left out.
    {
        float verts[4][8] = {{1, 2, 3}, {-4, 5, 6}, {7, -8, 9}, {0, 0, 0}};
        uint32_t nan = 0x7FC00000u;
        std::memcpy(&verts[3][0], &nan, 4);
        QueryDrawState st;
        AddPositions(&st, verts, 32, 0, 4);
        Expect(st.posCount == 3 && st.mn[0] == -4 && st.mn[1] == -8 && st.mn[2] == 3 && st.mx[0] == 7 &&
                   st.mx[1] == 5 && st.mx[2] == 9,
               "positions: box (-4,-8,3)..(7,5,9) from 3 of 4 vertices");
        QueryDrawState from1;
        AddPositions(&from1, verts, 32, 1, 2);
        Expect(from1.posCount == 2 && from1.mn[0] == -4 && from1.mx[0] == 7, "positions start at the first vertex");
    }

    // Grabs: N frames per new tag, then none.
    {
        PresentGrab g;
        g.Start("x");
        Expect(!g.Active(), "PresentGrabFrames 0: a tag starts nothing");
        g.SetFrames(3);
        g.Start("y0_p0");
        std::string names;
        for (int f = 0; f < 5; ++f) {
            names += g.Name("composite") + "|";
            g.EndFrame();
        }
        Expect(names == "y0_p0_composite_f0|y0_p0_composite_f1|y0_p0_composite_f2|||", "3 frames named f0..f2, then none");
        Expect(!g.Active(), "inactive after 3 frames");
    }

    int device = 0;
    if (argc > 2) {   // argv[1]: the X8R8G8B8 grab, argv[2]: the A16B16G16R16F grab
        wchar_t path[MAX_PATH] = {}, hdrPath[MAX_PATH] = {};
        MultiByteToWideChar(CP_ACP, 0, argv[1], -1, path, MAX_PATH);
        MultiByteToWideChar(CP_ACP, 0, argv[2], -1, hdrPath, MAX_PATH);
        device = GrabOnDevice(path, hdrPath);
        if (device == 1) ++g_failed;
    }
    std::printf("%s\n", g_failed ? "query trace test: FAIL" : "query trace test: PASS");
    if (g_failed) return 1;
    return device == 2 ? 2 : 0;
}
