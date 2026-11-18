#pragma once
// Query trace and frame grabs (worker B, flare orbs, X3). Both keys are off by default, log or write files only, and
// change no draw and no query answer.
//
// [Proxy] QueryTraceEvery=K (read at load; 0 = off, 1..600). Needs QueryStatsFrames > 0 at load as well, because the
// Issue hook and the "the game reads this query" mark come from X1 (query_probe.h); the load line says whether it is
// active. On every K-th presented frame (frame % K == 0) it follows each occlusion query the game reads from
// Issue(BEGIN) to Issue(END) and keeps the state of the first draw inside: the depth-stencil surface (pointer, size,
// format), ZENABLE, ZFUNC, ZWRITEENABLE, COLORWRITEENABLE, render target 0, the draw kind, whether the proxy sent it,
// and for UP draws whose first declaration element is a FLOAT3 at stream 0 offset 0, the box of their vertex positions.
// The record stays open until the game issues the query again (or 120 frames pass), so it also holds what the game was
// handed and what the proxy read fresh from the bridge after END. Then one line per query:
//   "QueryTrace: frame F q Q draws N sent S kind K ds D WxH fF zen Z zfn Z zwr Z cw C rt R pos M x0 y0 z0 x1 y1 z1
//    handed H first P1 last P2 fresh N2 first P3 end E"
//   (pos 0 = no positions; handed 0 / fresh 0 = never; end = reissue | timeout | table-full)
// and once per traced frame, at Present:
//   "QueryTraceFrame: frame F queries Q scene-ds D dropped-before-first-query N z-write on scene ds M other ds O"
//   scene-ds: the depth surface of the frame's first scene draw. dropped: draws the proxy did not send (PrepareDraw
//   false, or the close-camera fade guard) before the frame's first traced query BEGIN; of those, how many had
//   ZWRITEENABLE on into the scene depth surface or into another one. 0 on the scene surface means no draw the proxy
//   drops can be missing from the depth the queries test against.
//
// [Proxy] PresentGrabFrames=N (read at load; 0 = off, 1..16). On each new GateDump tag, for the next N presented
// frames, writes two 1/8-size pictures to the gate folder: <tag>_composite_f<i>.pfm, render target 0 right after the
// proxy's PresentModernSceneToComposite (Remix's picture as the game composite gets it), and <tag>_present_f<i>.pfm, the
// back buffer just before the real Present (after the HUD). Each is a box average of 8x8 pixels (three LINEAR 2x
// StretchRects), RGB, PFM rows bottom-up: 0..1 for 8-bit targets, the stored values for A16B16G16R16F ones (the
// composite target's format is not known from the code; f17802's hand-off draws go to an A16B16G16R16F target). A frame
// with no composite writes no composite file; a target of another format logs "failed at step 1".
//
// tests\hitch\run_query_trace_test.ps1 checks this header (normal build PASS, each mutant FAIL), the grab writer on a
// real D3D9 device, and the hook sites.
#include <windows.h>
#include <d3d9.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace proxy {

constexpr uint32_t kQueryTraceMaxEvery = 600;
constexpr uint32_t kQueryTraceTimeoutFrames = 120;
constexpr size_t kQueryTraceSlots = 256;
constexpr uint32_t kPresentGrabMaxFrames = 16;

inline uint32_t ParseQueryTraceEvery(int raw) {
    if (raw <= 0) return 0;
    return static_cast<uint32_t>(raw) > kQueryTraceMaxEvery ? kQueryTraceMaxEvery : static_cast<uint32_t>(raw);
}
inline uint32_t ParsePresentGrabFrames(int raw) {
    if (raw <= 0) return 0;
    return static_cast<uint32_t>(raw) > kPresentGrabMaxFrames ? kPresentGrabMaxFrames : static_cast<uint32_t>(raw);
}

// State of one draw, read from the device by the proxy (QueryTraceReadDrawState) only when the trace asks for it.
struct QueryDrawState {
    uintptr_t ds = 0, rt0 = 0;
    uint32_t dsW = 0, dsH = 0, dsFmt = 0;
    int zen = -1, zfn = -1, zwr = -1, cw = -1;
    uint32_t posCount = 0;            // 0 = no positions read
    float mn[3] = {}, mx[3] = {};
};

// Folds float3 positions (stride bytes apart, starting at data) into st's box; at most 64 are read.
inline void AddPositions(QueryDrawState* st, const void* data, uint32_t stride, uint32_t first, uint32_t count) {
    if (!data || stride < 12 || !count) return;
    if (count > 64) count = 64;
    const uint8_t* base = static_cast<const uint8_t*>(data) + static_cast<size_t>(first) * stride;
    for (uint32_t i = 0; i < count; ++i) {
        float p[3];
        std::memcpy(p, base + static_cast<size_t>(i) * stride, sizeof(p));
        if (!std::isfinite(p[0]) || !std::isfinite(p[1]) || !std::isfinite(p[2])) continue;
        for (int a = 0; a < 3; ++a) {
            if (!st->posCount || p[a] < st->mn[a]) st->mn[a] = p[a];
            if (!st->posCount || p[a] > st->mx[a]) st->mx[a] = p[a];
        }
        ++st->posCount;
    }
}

class QueryTrace {
public:
    void SetEvery(uint32_t every) { every_ = every; }
    uint32_t Every() const { return every_; }
    bool Enabled() const { return every_ != 0; }
    bool TracingFrame() const { return tracing_; }

    // Call with the number of the frame about to be drawn (at load and right after each Present).
    void BeginFrame(uint32_t frame) {
        frame_ = frame;
#ifdef QUERY_TRACE_MUTANT_EVERY_FRAME
        tracing_ = every_ != 0;
#else
        tracing_ = every_ != 0 && frame % every_ == 0;
#endif
        open_ = nullptr;
        frameQueries_ = 0;
        firstQuerySeen_ = false;
        sceneDs_ = 0;
        dropped_ = 0;
        droppedDs_.clear();
    }

    // The proxy reads the device state of a draw only when one of these is true.
    bool WantsDrawState() const { return tracing_ && open_ && !open_->hasDraw; }
    bool WantsSceneDs() const { return tracing_ && !sceneDs_; }
    bool WantsDropState() const { return tracing_ && !firstQuerySeen_; }

    // Issue(D3DISSUE_BEGIN). gameQuery: a query the game has read (X1's mark). Closes an older record of this query.
    void OnBegin(const void* q, bool gameQuery, std::vector<std::string>* out) {
        if (!every_) return;
        if (Slot* s = Find(q)) Close(s, "reissue", out);
        if (!tracing_ || !gameQuery) return;
        firstQuerySeen_ = true;
        Slot* s = Free();
        if (!s) {
            char line[160];
            std::snprintf(line, sizeof(line), "QueryTrace: frame %u q %p end table-full", frame_, q);
            out->push_back(line);
            return;
        }
        *s = Slot{};
        s->q = q;
        s->frame = frame_;
        s->open = true;
        open_ = s;
        ++frameQueries_;
    }
    void OnEnd(const void* q) {
        if (!open_ || open_->q != q) return;
        open_->open = false;
        open_->ended = true;
        open_ = nullptr;
    }
    // Every draw call, sent or not. st is the state the proxy read when WantsDrawState() was true, else nullptr.
    void OnDraw(bool sent, const char* kind, const QueryDrawState* st) {
        if (!open_) return;
        ++open_->draws;
        if (sent) ++open_->sent;
#ifdef QUERY_TRACE_MUTANT_LAST_DRAW
        if (st) { open_->state = *st; open_->kind = kind; open_->hasDraw = true; }
#else
        if (st && !open_->hasDraw) { open_->state = *st; open_->kind = kind; open_->hasDraw = true; }
#endif
    }
    void OnSceneDraw(uintptr_t ds) { if (tracing_ && !sceneDs_) sceneDs_ = ds; }
    // A draw the proxy did not send, before the frame's first traced query. zwr: ZWRITEENABLE (with ZENABLE) on.
    void OnDroppedDraw(uintptr_t ds, bool zwr) {
#ifdef QUERY_TRACE_MUTANT_DROPS_AFTER_QUERY
        if (!tracing_) return;
#else
        if (!WantsDropState()) return;
#endif
        ++dropped_;
        if (zwr && ds) droppedDs_.push_back(ds);
    }
    // A pixel count the proxy read from the bridge (S_OK) for query q.
    void OnFresh(const void* q, uint32_t pixels) {
        Slot* s = Find(q);
        if (!s || !s->ended) return;
        if (!s->freshN++) s->freshFirst = pixels;
    }
    // A pixel count handed to the game (S_OK) for query q.
    void OnHanded(const void* q, uint32_t pixels) {
#ifdef QUERY_TRACE_MUTANT_ANY_QUERY
        Slot* s = nullptr;
        for (Slot& x : slots_) if (x.q && x.ended) { s = &x; break; }
        (void)q;
#else
        Slot* s = Find(q);
#endif
        if (!s || !s->ended) return;
        if (!s->handedN++) s->handedFirst = pixels;
        s->handedLast = pixels;
    }
    // Call once per presented frame, before Present, with the frame's number. Writes the frame line on traced frames
    // and closes records older than kQueryTraceTimeoutFrames.
    void EndFrame(std::vector<std::string>* out) {
        if (!every_) return;
        for (Slot& s : slots_)
            if (s.q && frame_ - s.frame >= kQueryTraceTimeoutFrames) Close(&s, "timeout", out);
        if (!tracing_) return;
        uint32_t onScene = 0, other = 0;
        for (uintptr_t ds : droppedDs_) (ds == sceneDs_ ? onScene : other)++;
        char line[256];
        std::snprintf(line, sizeof(line),
                      "QueryTraceFrame: frame %u queries %u scene-ds %08IX dropped-before-first-query %u z-write on "
                      "scene ds %u other ds %u",
                      frame_, frameQueries_, sceneDs_, dropped_, onScene, other);
        out->push_back(line);
    }

private:
    struct Slot {
        const void* q = nullptr;
        uint32_t frame = 0, draws = 0, sent = 0;
        bool open = false, ended = false, hasDraw = false;
        const char* kind = "none";
        QueryDrawState state;
        uint32_t handedN = 0, handedFirst = 0, handedLast = 0, freshN = 0, freshFirst = 0;
    };
    Slot* Find(const void* q) {
        for (Slot& s : slots_) if (s.q == q && q) return &s;
        return nullptr;
    }
    Slot* Free() {
        for (Slot& s : slots_) if (!s.q) return &s;
        return nullptr;
    }
    void Close(Slot* s, const char* why, std::vector<std::string>* out) {
        const QueryDrawState& st = s->state;
        char line[512];
        std::snprintf(line, sizeof(line),
                      "QueryTrace: frame %u q %p draws %u sent %u kind %s ds %08IX %ux%u f%u zen %d zfn %d zwr %d cw %d "
                      "rt %08IX pos %u %.1f %.1f %.1f %.1f %.1f %.1f handed %u first %u last %u fresh %u first %u end %s",
                      s->frame, s->q, s->draws, s->sent, s->kind, st.ds, st.dsW, st.dsH, st.dsFmt, st.zen, st.zfn,
                      st.zwr, st.cw, st.rt0, st.posCount, st.mn[0], st.mn[1], st.mn[2], st.mx[0], st.mx[1], st.mx[2],
                      s->handedN, s->handedFirst, s->handedLast, s->freshN, s->freshFirst, why);
        out->push_back(line);
        if (open_ == s) open_ = nullptr;
        *s = Slot{};
    }

    uint32_t every_ = 0, frame_ = 0, frameQueries_ = 0, dropped_ = 0;
    bool tracing_ = false, firstQuerySeen_ = false;
    uintptr_t sceneDs_ = 0;
    Slot* open_ = nullptr;
    std::vector<uintptr_t> droppedDs_;
    Slot slots_[kQueryTraceSlots];
};

// Frame grabs: which frames of a GateDump tag get files. Not thread safe; called on the render thread.
class PresentGrab {
public:
    void SetFrames(uint32_t frames) { frames_ = frames; }
    uint32_t Frames() const { return frames_; }
    // A new GateDump tag: the next Frames() frames are grabbed under it.
    void Start(const std::string& tag) {
        if (!frames_ || tag.empty()) return;
        tag_ = tag;
        index_ = 0;
        left_ = frames_;
    }
    bool Active() const { return left_ != 0; }
    // File stem for this frame's grab of `what` ("composite" or "present"), or empty when this frame is not grabbed.
    std::string Name(const char* what) const {
        if (!left_) return std::string();
        char stem[200];
        std::snprintf(stem, sizeof(stem), "%s_%s_f%u", tag_.c_str(), what, index_);
        return stem;
    }
    // Call once per presented frame after the present grab.
    void EndFrame() {
        if (!left_) return;
#ifndef QUERY_TRACE_MUTANT_GRAB_NO_STOP
        --left_;
#endif
        ++index_;
    }

private:
    uint32_t frames_ = 0, index_ = 0, left_ = 0;
    std::string tag_;
};

inline float GrabHalfToFloat(uint16_t h) {
    const uint32_t sign = (h >> 15) & 1u, exp = (h >> 10) & 0x1Fu, mant = h & 0x3FFu;
    float v;
    if (exp == 0) v = std::ldexp(static_cast<float>(mant), -24);
    else if (exp == 31) v = mant ? 0.0f : 65504.0f;   // NaN reads 0, infinity the largest half
    else v = std::ldexp(static_cast<float>(mant | 0x400u), static_cast<int>(exp) - 25);
    return sign ? -v : v;
}

// Writes `source` (a render target or the back buffer: A8R8G8B8, X8R8G8B8, or A16B16G16R16F like the game's scene HDR
// targets) at 1/8 size as an RGB PFM: three LINEAR 2x StretchRects (an exact 2x2 box average each, so an 8x8 box in
// all), then GetRenderTargetData. Float targets keep their values (they can exceed 1).
// Returns 0 on success, otherwise the step that failed (1 desc/format, 2 render targets, 3 stretch, 4 readback, 5 file).
inline int WriteGrabPfm(IDirect3DDevice9* device, IDirect3DSurface9* source, const wchar_t* path) {
    D3DSURFACE_DESC desc = {};
    if (!source || FAILED(source->GetDesc(&desc)) || desc.Width < 8 || desc.Height < 8) return 1;
    const bool half = desc.Format == D3DFMT_A16B16G16R16F;
    if (desc.Format != D3DFMT_A8R8G8B8 && desc.Format != D3DFMT_X8R8G8B8 && !half) return 1;
    IDirect3DSurface9* level[3] = {};
    UINT w = desc.Width, h = desc.Height;
    int fail = 0;
    IDirect3DSurface9* from = source;
    for (int i = 0; i < 3 && !fail; ++i) {
        w /= 2;
        h /= 2;
        if (FAILED(device->CreateRenderTarget(w, h, desc.Format, D3DMULTISAMPLE_NONE, 0, FALSE, &level[i], nullptr)) ||
            !level[i]) {
            fail = 2;
            break;
        }
        // Source rect trimmed to an exact 2x of the target, so every output pixel averages one 2x2 block.
        const RECT src = {0, 0, static_cast<LONG>(w * 2), static_cast<LONG>(h * 2)};
#ifdef QUERY_TRACE_MUTANT_GRAB_POINT
        if (FAILED(device->StretchRect(from, &src, level[i], nullptr, D3DTEXF_POINT))) fail = 3;
#else
        if (FAILED(device->StretchRect(from, &src, level[i], nullptr, D3DTEXF_LINEAR))) fail = 3;
#endif
        from = level[i];
    }
    IDirect3DSurface9* sys = nullptr;
    if (!fail && (FAILED(device->CreateOffscreenPlainSurface(w, h, desc.Format, D3DPOOL_SYSTEMMEM, &sys, nullptr)) ||
                  !sys || FAILED(device->GetRenderTargetData(level[2], sys)))) {
        fail = 4;
    }
    D3DLOCKED_RECT locked = {};
    if (!fail && (FAILED(sys->LockRect(&locked, nullptr, D3DLOCK_READONLY)) || !locked.pBits)) fail = 4;
    if (!fail) {
        std::vector<float> rgb(static_cast<size_t>(w) * h * 3);
        for (UINT y = 0; y < h; ++y) {
            const uint8_t* row = static_cast<const uint8_t*>(locked.pBits) + static_cast<size_t>(y) * locked.Pitch;
            float* dst = rgb.data() + static_cast<size_t>(h - 1u - y) * w * 3;   // PFM rows run bottom-up
            for (UINT x = 0; x < w; ++x) {
                if (half) {   // memory order R, G, B, A, 16 bits each
                    uint16_t px[4];
                    std::memcpy(px, row + static_cast<size_t>(x) * 8, sizeof(px));
#ifdef QUERY_TRACE_MUTANT_HALF_BGR
                    for (int c = 0; c < 3; ++c) dst[x * 3 + c] = GrabHalfToFloat(px[2 - c]);
#else
                    for (int c = 0; c < 3; ++c) dst[x * 3 + c] = GrabHalfToFloat(px[c]);
#endif
                } else {
                    dst[x * 3 + 0] = row[x * 4 + 2] / 255.0f;
                    dst[x * 3 + 1] = row[x * 4 + 1] / 255.0f;
                    dst[x * 3 + 2] = row[x * 4 + 0] / 255.0f;
                }
            }
        }
        sys->UnlockRect();
        wchar_t tmp[MAX_PATH] = {};
        swprintf_s(tmp, L"%s.tmp", path);
        FILE* f = nullptr;
        if (_wfopen_s(&f, tmp, L"wb") == 0 && f) {
            std::fprintf(f, "PF\n%u %u\n-1.0\n", w, h);
            std::fwrite(rgb.data(), sizeof(float), rgb.size(), f);
            std::fclose(f);
            if (!MoveFileExW(tmp, path, MOVEFILE_REPLACE_EXISTING)) fail = 5;
        } else {
            fail = 5;
        }
    }
    if (sys) sys->Release();
    for (IDirect3DSurface9* s : level) if (s) s->Release();
    return fail;
}

inline QueryTrace g_queryTrace;
inline PresentGrab g_presentGrab;
inline SRWLOCK g_queryTraceLock = SRWLOCK_INIT;

}  // namespace proxy
