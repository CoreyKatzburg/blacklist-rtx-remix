#include "replay_side.h"

#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "camera_solver.h"
#include "proxy_log.h"

namespace proxy {
namespace {

struct CallTag {
    uint32_t frame, index, value;
};
struct FrameTotals {
    uint32_t draws, uploads;
};

bool g_recording = false;
uintptr_t g_testCameraRva = 0;
wchar_t g_callsPath[MAX_PATH] = {};
wchar_t g_checkPath[MAX_PATH] = {};
std::string* g_pending = nullptr;
std::vector<CallTag>* g_cameraTags = nullptr;
std::vector<CallTag>* g_decalTags = nullptr;
std::vector<FrameTotals>* g_totals = nullptr;
size_t g_cameraCursor = 0, g_decalCursor = 0;
uint32_t g_frame = 0, g_draws = 0, g_uploads = 0, g_cameraUsed = 0, g_decalUsed = 0;
uintptr_t g_loggedCallers[8] = {};
int g_loggedCallerCount = 0;

void AddLine(const char* format, ...) {
    char line[96];
    va_list args;
    va_start(args, format);
    const int length = _vsnprintf_s(line, _TRUNCATE, format, args);
    va_end(args);
    if (length > 0) g_pending->append(line, static_cast<size_t>(length));
}

void FlushPending(const wchar_t* path) {
    size_t length = g_pending->size();
    AppendToFile(path, g_pending->data(), &length);
    g_pending->clear();
}

const CallTag* FindTag(const std::vector<CallTag>& tags, size_t cursor, uint32_t index) {
    for (size_t i = cursor; i < tags.size() && tags[i].frame == g_frame; ++i)
        if (tags[i].index == index) return &tags[i];
    return nullptr;
}

void LoadCalls() {
    HANDLE file = CreateFileW(g_callsPath, GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        WriteLog("replay side: no call recording at %ls; no camera upload and no decal draw is marked", g_callsPath);
        return;
    }
    const DWORD size = GetFileSize(file, nullptr);
    std::string text(size, '\0');
    DWORD read = 0;
    if (!ReadFile(file, &text[0], size, &read, nullptr)) read = 0;
    CloseHandle(file);
    text.resize(read);
    g_cameraTags = new std::vector<CallTag>();
    g_decalTags = new std::vector<CallTag>();
    g_totals = new std::vector<FrameTotals>();
    for (const char* line = text.c_str(); *line;) {
        char* cursor = nullptr;
        const char kind = line[0];
        if (line[1] == ',') {
            const uint32_t frame = strtoul(line + 2, &cursor, 10);
            const uint32_t second = *cursor == ',' ? strtoul(cursor + 1, &cursor, 10) : 0;
            if (kind == 'C' && *cursor == ',') {
                g_cameraTags->push_back({frame, second, static_cast<uint32_t>(strtoul(cursor + 1, &cursor, 16))});
            } else if (kind == 'D') {
                g_decalTags->push_back({frame, second, 0});
            } else if (kind == 'F' && *cursor == ',') {
                if (g_totals->size() <= frame) g_totals->resize(frame + 1, {0xFFFFFFFFu, 0xFFFFFFFFu});
                (*g_totals)[frame] = {second, static_cast<uint32_t>(strtoul(cursor + 1, &cursor, 10))};
            }
        }
        const char* end = strchr(line, '\n');
        if (!end) break;
        line = end + 1;
    }
    WriteLog("replay side: %u camera uploads, %u decal draws and %u frames loaded from %ls",
             static_cast<unsigned>(g_cameraTags->size()), static_cast<unsigned>(g_decalTags->size()),
             static_cast<unsigned>(g_totals->size()), g_callsPath);
}

}  // namespace

void ReplaySideInit(const wchar_t* iniPath) {
    if (!CombinePath(ExecutableDirectory(), L"d3d9_proxy_replay_calls.csv", g_callsPath) ||
        !CombinePath(ExecutableDirectory(), L"d3d9_proxy_replay_check.csv", g_checkPath)) return;
    if (ReplayMode()) {
        g_pending = new std::string();
        DeleteFileW(g_checkPath);
        LoadCalls();
        return;
    }
    if (GetPrivateProfileIntW(L"Replay", L"Record", 0, iniPath) == 0) return;
    wchar_t rva[32] = {};
    GetPrivateProfileStringW(L"Replay", L"TestCameraRva", L"", rva, ARRAYSIZE(rva), iniPath);
    g_testCameraRva = wcstoul(rva, nullptr, 16);
    // apitrace, chained under the proxy, writes its trace to the Desktop unless TRACE_FILE names another place.
    wchar_t trace[MAX_PATH] = {};
    GetPrivateProfileStringW(L"Replay", L"TraceFile", L"", trace, ARRAYSIZE(trace), iniPath);
    if (trace[0]) SetEnvironmentVariableW(L"TRACE_FILE", trace);
    g_pending = new std::string();
    DeleteFileW(g_callsPath);
    g_recording = true;
    WriteLog("replay record: writing %ls (test camera RVA %08IX), apitrace TRACE_FILE %ls", g_callsPath,
             g_testCameraRva, trace[0] ? trace : L"not set");
}

bool ReplayRecording() { return g_recording; }
uint32_t ReplaySideFrame() { return g_frame; }

uintptr_t ReplaySideCallerRva(uintptr_t liveCallerRva, UINT startRegister, UINT vector4Count) {
    const uint32_t index = g_uploads++;
    if (ReplayMode()) {
        const CallTag* tag = g_cameraTags ? FindTag(*g_cameraTags, g_cameraCursor, index) : nullptr;
        if (!tag) return static_cast<uintptr_t>(-1);
        ++g_cameraUsed;
        return tag->value;
    }
    if (!g_recording) return liveCallerRva;
    const uintptr_t callerRva =
        g_testCameraRva && liveCallerRva == g_testCameraRva ? kGameplayCameraUploadReturnRva : liveCallerRva;
    if (IsDirectCameraUploadReturn(callerRva)) {
        AddLine("C,%u,%u,%08X\n", g_frame, index, static_cast<unsigned>(callerRva));
    } else if (startRegister == 8 && vector4Count == 4 && g_loggedCallerCount < ARRAYSIZE(g_loggedCallers)) {
        bool known = false;
        for (int i = 0; i < g_loggedCallerCount; ++i) known = known || g_loggedCallers[i] == callerRva;
        if (!known) {
            g_loggedCallers[g_loggedCallerCount++] = callerRva;
            WriteLog("replay record: c8 x4 upload from caller RVA %08IX is not a camera upload", callerRva);
        }
    }
    return callerRva;
}

bool ReplaySideDecalDraw(bool liveIsDecal) {
    const uint32_t index = g_draws++;
    if (ReplayMode()) {
        const bool decal = g_decalTags && FindTag(*g_decalTags, g_decalCursor, index) != nullptr;
        g_decalUsed += decal;
        return decal;
    }
    if (g_recording && liveIsDecal) AddLine("D,%u,%u\n", g_frame, index);
    return liveIsDecal;
}

void ReplaySideEndFrame() {
    if (g_recording) {
        AddLine("F,%u,%u,%u\n", g_frame, g_draws, g_uploads);
        FlushPending(g_callsPath);
    } else if (ReplayMode() && g_pending) {
        const bool known = g_totals && g_frame < g_totals->size();
        AddLine("%u,%u,%u,%d,%d,%u,%u\n", g_frame, g_draws, g_uploads,
                known ? static_cast<int>((*g_totals)[g_frame].draws) : -1,
                known ? static_cast<int>((*g_totals)[g_frame].uploads) : -1, g_cameraUsed, g_decalUsed);
        FlushPending(g_checkPath);
        if (g_cameraTags)
            while (g_cameraCursor < g_cameraTags->size() && (*g_cameraTags)[g_cameraCursor].frame <= g_frame) ++g_cameraCursor;
        if (g_decalTags)
            while (g_decalCursor < g_decalTags->size() && (*g_decalTags)[g_decalCursor].frame <= g_frame) ++g_decalCursor;
    }
    ++g_frame;
    g_draws = g_uploads = g_cameraUsed = g_decalUsed = 0;
}

}  // namespace proxy
