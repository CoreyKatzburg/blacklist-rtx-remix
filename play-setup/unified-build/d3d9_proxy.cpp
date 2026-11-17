// D3D9 proxy entry point, INI configuration & live hot-reload, COM factory wrapper,
// device/resource vtable hooks, FocusGuard, FormatGuard, and occlusion QueryMode.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d9.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <intrin.h>
#include <map>
#include <new>
#include <tuple>

#include "camera_solver.h"
#include "decal_tag.h"
#include "draw_filter.h"
#include "handoff_renderer.h"
#include "lightpub.h"
#include "present_tag.h"
#include "proxy_diagnostics.h"
#include "proxy_log.h"
#include "proxy_types.h"
#include "stage_texture_dump.h"
#include "texture_fill.h"

#pragma intrinsic(_ReturnAddress)

// Splinter Cell Blacklist DX9 shader constants (confirmed from CTAB metadata):
//   c4      gDrawScale       float4
//   c5-c7   gLocalToWorld    column-major float4x3
//   c8-c11  gWorldToProj     column-major float4x4
// The game uploads the last matrix after D3DXMatrixTranspose(world * view * projection).

namespace {

using proxy::CallTimer;
using proxy::DeviceHook;
using proxy::UploadInfo;
using proxy::WriteLog;

HMODULE g_realD3D9 = nullptr;
HMODULE g_systemD3D9 = nullptr;
HINSTANCE g_proxyInstance = nullptr;
uintptr_t g_executableBase = 0;
INIT_ONCE g_initializationOnce = INIT_ONCE_STATIC_INIT;
wchar_t g_proxyPath[MAX_PATH] = {};

bool g_publishCamera = true;
bool g_diagnoseWorldWvp = false;
UINT g_cameraSources = 0;  // 0 = both direct camera sites, 1 = gameplay site only, 2 = pure site only
bool g_cameraOncePerFrame = true;
bool g_presentHook = false;
bool g_lightsEnabled = true;
bool g_lightPublisherTried = false;
bool g_debuggingTelemetry = false;

UINT g_queryMode = 1;
LONGLONG g_queryPollTicks = 0;
bool g_formatGuard = true;
bool g_prevGogglesLogged = false;

proxy::FilterConfig g_filterCfg = {};
volatile LONG g_presentCount = 0;

// Export forwarding targets resolved from chained or system d3d9.dll.
FARPROC g_Ordinal16 = nullptr, g_Ordinal17 = nullptr, g_Ordinal18 = nullptr, g_Ordinal19 = nullptr;
FARPROC g_Direct3DCreate9On12 = nullptr, g_Direct3DCreate9On12Ex = nullptr;
FARPROC g_Ordinal22 = nullptr, g_Ordinal23 = nullptr;
FARPROC g_Direct3DShaderValidatorCreate9 = nullptr, g_PSGPError = nullptr, g_PSGPSampleTexture = nullptr;
FARPROC g_D3DPERF_BeginEvent = nullptr, g_D3DPERF_EndEvent = nullptr, g_D3DPERF_GetStatus = nullptr;
FARPROC g_D3DPERF_QueryRepeatFrame = nullptr, g_D3DPERF_SetMarker = nullptr;
FARPROC g_D3DPERF_SetOptions = nullptr, g_D3DPERF_SetRegion = nullptr;
FARPROC g_DebugSetLevel = nullptr, g_DebugSetMute = nullptr;
FARPROC g_Direct3D9EnableMaximizedWindowedModeShim = nullptr;

using Direct3DCreate9Fn = IDirect3D9*(WINAPI*)(UINT);
using Direct3DCreate9ExFn = HRESULT(WINAPI*)(UINT, IDirect3D9Ex**);
Direct3DCreate9Fn g_Direct3DCreate9 = nullptr;
Direct3DCreate9ExFn g_Direct3DCreate9Ex = nullptr;

// Original pointers for hooked device, resource, and query vtable slots.
HRESULT(WINAPI* g_createTexture)(IDirect3DDevice9*, UINT, UINT, UINT, DWORD, D3DFORMAT, D3DPOOL, IDirect3DTexture9**, HANDLE*) = nullptr;
HRESULT(WINAPI* g_createVertexBuffer)(IDirect3DDevice9*, UINT, DWORD, DWORD, D3DPOOL, IDirect3DVertexBuffer9**, HANDLE*) = nullptr;
HRESULT(WINAPI* g_createIndexBuffer)(IDirect3DDevice9*, UINT, DWORD, D3DFORMAT, D3DPOOL, IDirect3DIndexBuffer9**, HANDLE*) = nullptr;
HRESULT(WINAPI* g_updateSurface)(IDirect3DDevice9*, IDirect3DSurface9*, const RECT*, IDirect3DSurface9*, const POINT*) = nullptr;
HRESULT(WINAPI* g_updateTexture)(IDirect3DDevice9*, IDirect3DBaseTexture9*, IDirect3DBaseTexture9*) = nullptr;
HRESULT(WINAPI* g_getRenderTargetData)(IDirect3DDevice9*, IDirect3DSurface9*, IDirect3DSurface9*) = nullptr;
HRESULT(WINAPI* g_stretchRect)(IDirect3DDevice9*, IDirect3DSurface9*, const RECT*, IDirect3DSurface9*, const RECT*, D3DTEXTUREFILTERTYPE) = nullptr;
HRESULT(WINAPI* g_clear)(IDirect3DDevice9*, DWORD, const D3DRECT*, DWORD, D3DCOLOR, float, DWORD) = nullptr;
HRESULT(WINAPI* g_createVolumeTexture)(IDirect3DDevice9*, UINT, UINT, UINT, UINT, DWORD, D3DFORMAT, D3DPOOL, IDirect3DVolumeTexture9**, HANDLE*) = nullptr;
HRESULT(WINAPI* g_createCubeTexture)(IDirect3DDevice9*, UINT, UINT, DWORD, D3DFORMAT, D3DPOOL, IDirect3DCubeTexture9**, HANDLE*) = nullptr;
HRESULT(WINAPI* g_createRenderTarget)(IDirect3DDevice9*, UINT, UINT, D3DFORMAT, D3DMULTISAMPLE_TYPE, DWORD, BOOL, IDirect3DSurface9**, HANDLE*) = nullptr;
HRESULT(WINAPI* g_createDepthStencilSurface)(IDirect3DDevice9*, UINT, UINT, D3DFORMAT, D3DMULTISAMPLE_TYPE, DWORD, BOOL, IDirect3DSurface9**, HANDLE*) = nullptr;
HRESULT(WINAPI* g_createOffscreenPlainSurface)(IDirect3DDevice9*, UINT, UINT, D3DFORMAT, D3DPOOL, IDirect3DSurface9**, HANDLE*) = nullptr;
HRESULT(WINAPI* g_createQuery)(IDirect3DDevice9*, D3DQUERYTYPE, IDirect3DQuery9**) = nullptr;
HRESULT(WINAPI* g_queryGetData)(IDirect3DQuery9*, void*, DWORD, DWORD) = nullptr;
HRESULT(WINAPI* g_lockRect)(IDirect3DTexture9*, UINT, D3DLOCKED_RECT*, const RECT*, DWORD) = nullptr;
HRESULT(WINAPI* g_unlockRect)(IDirect3DTexture9*, UINT) = nullptr;
HRESULT(WINAPI* g_vertexBufferLock)(IUnknown*, UINT, UINT, void**, DWORD) = nullptr;
HRESULT(WINAPI* g_vertexBufferUnlock)(IUnknown*) = nullptr;
HRESULT(WINAPI* g_indexBufferLock)(IUnknown*, UINT, UINT, void**, DWORD) = nullptr;
HRESULT(WINAPI* g_indexBufferUnlock)(IUnknown*) = nullptr;
HRESULT(WINAPI* g_setRenderTarget)(IDirect3DDevice9*, DWORD, IDirect3DSurface9*) = nullptr;
HRESULT(WINAPI* g_setTexture)(IDirect3DDevice9*, DWORD, IDirect3DBaseTexture9*) = nullptr;
HRESULT(WINAPI* g_setVertexShader)(IDirect3DDevice9*, IDirect3DVertexShader9*) = nullptr;
HRESULT(WINAPI* g_setPixelShader)(IDirect3DDevice9*, IDirect3DPixelShader9*) = nullptr;
HRESULT(WINAPI* g_setPixelShaderConstantF)(IDirect3DDevice9*, UINT, const float*, UINT) = nullptr;
HRESULT(WINAPI* g_setRenderState)(IDirect3DDevice9*, D3DRENDERSTATETYPE, DWORD) = nullptr;
HRESULT(WINAPI* g_setStreamSource)(IDirect3DDevice9*, UINT, IDirect3DVertexBuffer9*, UINT, UINT) = nullptr;
HRESULT(WINAPI* g_setIndices)(IDirect3DDevice9*, IDirect3DIndexBuffer9*) = nullptr;
HRESULT(WINAPI* g_reset)(IDirect3DDevice9*, D3DPRESENT_PARAMETERS*) = nullptr;
ULONG(WINAPI* g_textureRelease)(IUnknown*) = nullptr;
ULONG(WINAPI* g_vertexShaderRelease)(IUnknown*) = nullptr;
ULONG(WINAPI* g_pixelShaderRelease)(IUnknown*) = nullptr;
UINT(WINAPI* g_getAvailableTextureMem)(IDirect3DDevice9*) = nullptr;

constexpr uint32_t kWorldRegisterMask = (1u << 5) | (1u << 6) | (1u << 7);
constexpr uint32_t kWvpRegisterMask = (1u << 8) | (1u << 9) | (1u << 10) | (1u << 11);
constexpr uint32_t kRequiredRegisterMask = kWorldRegisterMask | kWvpRegisterMask;

proxy::CameraFrameTracker g_cameraTracker;
SRWLOCK g_hookListLock = SRWLOCK_INIT;
DeviceHook* g_deviceHooks = nullptr;

// FocusGuard: drops focus-loss, minimize, and restore messages posted by d3d9_remix.dll to Remix's
// message thread so Remix 1.5.2 cannot race an extra end-of-frame against its command-stream flush.
volatile LONG g_focusMessagesDropped = 0;
BOOL(WINAPI* g_postThreadMessageA)(DWORD, UINT, WPARAM, LPARAM) = nullptr;

BOOL WINAPI GuardedPostThreadMessageA(DWORD thread, UINT message, WPARAM wParam, LPARAM lParam) {
    if ((message == WM_ACTIVATE && LOWORD(wParam) == WA_INACTIVE) ||
        (message == WM_SIZE && (wParam == SIZE_MINIMIZED || wParam == SIZE_RESTORED))) {
        InterlockedIncrement(&g_focusMessagesDropped);
        return TRUE;
    }
    return g_postThreadMessageA(thread, message, wParam, lParam);
}

void InstallFocusGuard(HMODULE remixClient) {
    BYTE* const base = reinterpret_cast<BYTE*>(remixClient);
    const auto* const dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
    const auto* const nt = reinterpret_cast<const IMAGE_NT_HEADERS*>(base + dos->e_lfanew);
    const IMAGE_DATA_DIRECTORY& directory = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    g_postThreadMessageA = reinterpret_cast<decltype(g_postThreadMessageA)>(
        GetProcAddress(GetModuleHandleW(L"user32.dll"), "PostThreadMessageA"));
    const void* const wanted = reinterpret_cast<const void*>(g_postThreadMessageA);
    if (!directory.VirtualAddress || !wanted) {
        WriteLog("FocusGuard: no import table; not installed");
        return;
    }
    for (auto* imports = reinterpret_cast<const IMAGE_IMPORT_DESCRIPTOR*>(base + directory.VirtualAddress); imports->Name;
         ++imports) {
        if (_stricmp(reinterpret_cast<const char*>(base + imports->Name), "user32.dll") != 0) continue;
        for (auto* thunk = reinterpret_cast<IMAGE_THUNK_DATA*>(base + imports->FirstThunk); thunk->u1.Function; ++thunk) {
            if (reinterpret_cast<const void*>(thunk->u1.Function) != wanted) continue;
            DWORD oldProtection = 0;
            if (!VirtualProtect(&thunk->u1.Function, sizeof(void*), PAGE_READWRITE, &oldProtection)) break;
            thunk->u1.Function = reinterpret_cast<ULONG_PTR>(&GuardedPostThreadMessageA);
            VirtualProtect(&thunk->u1.Function, sizeof(void*), oldProtection, &oldProtection);
            WriteLog("FocusGuard: installed (focus-loss, minimise and restore messages are not forwarded to Remix)");
            return;
        }
    }
    WriteLog("FocusGuard: PostThreadMessageA import not found in the chained DLL; not installed");
}

// WindowLog: every quit with ray tracing on ends in VK_ERROR_DEVICE_LOST 2.5 to 4.9 s after the game window
// loses focus, and the logs do not say what the window did. Log the game window's focus, size, show and close
// messages (sent and posted) with timestamps, capped so alt-tabbing cannot flood the log.
// The proxy does not link user32 (see FocusGuard); these are resolved when the log is installed.
HWND g_loggedWindow = nullptr;
volatile LONG g_windowLogLines = 0;
constexpr LONG kWindowLogMaxLines = 300;
BOOL(WINAPI* g_isWindowVisible)(HWND) = nullptr;
BOOL(WINAPI* g_isIconic)(HWND) = nullptr;
LRESULT(WINAPI* g_callNextHookEx)(HHOOK, int, WPARAM, LPARAM) = nullptr;

void LogWindowMessage(HWND window, UINT message, WPARAM wParam, LPARAM lParam, const char* how) {
    if (window != g_loggedWindow) return;
    const char* name = nullptr;
    switch (message) {
    case WM_ACTIVATEAPP: name = "WM_ACTIVATEAPP"; break;
    case WM_ACTIVATE: name = "WM_ACTIVATE"; break;
    case WM_KILLFOCUS: name = "WM_KILLFOCUS"; break;
    case WM_SETFOCUS: name = "WM_SETFOCUS"; break;
    case WM_SIZE: name = "WM_SIZE"; break;
    case WM_SHOWWINDOW: name = "WM_SHOWWINDOW"; break;
    case WM_CLOSE: name = "WM_CLOSE"; break;
    case WM_DESTROY: name = "WM_DESTROY"; break;
    case WM_QUIT: name = "WM_QUIT"; break;
    case WM_SYSCOMMAND:
        if ((wParam & 0xFFF0) != SC_CLOSE && (wParam & 0xFFF0) != SC_MINIMIZE) return;
        name = "WM_SYSCOMMAND";
        break;
    default: return;
    }
    if (InterlockedIncrement(&g_windowLogLines) > kWindowLogMaxLines) return;
    WriteLog("WindowLog: %s %s wParam=0x%08lX lParam=0x%08lX visible=%d iconic=%d", how, name,
             static_cast<unsigned long>(wParam), static_cast<unsigned long>(lParam),
             g_isWindowVisible(window) ? 1 : 0, g_isIconic(window) ? 1 : 0);
}

LRESULT CALLBACK WindowLogCallProc(int code, WPARAM wParam, LPARAM lParam) {
    if (code == HC_ACTION) {
        const auto* msg = reinterpret_cast<const CWPSTRUCT*>(lParam);
        LogWindowMessage(msg->hwnd, msg->message, msg->wParam, msg->lParam, "sent");
    }
    return g_callNextHookEx(nullptr, code, wParam, lParam);
}

LRESULT CALLBACK WindowLogGetMessageProc(int code, WPARAM wParam, LPARAM lParam) {
    if (code == HC_ACTION && wParam == PM_REMOVE) {
        const auto* msg = reinterpret_cast<const MSG*>(lParam);
        // WM_QUIT is posted to the thread, not a window.
        LogWindowMessage(msg->message == WM_QUIT ? g_loggedWindow : msg->hwnd, msg->message, msg->wParam,
                         msg->lParam, "posted");
    }
    return g_callNextHookEx(nullptr, code, wParam, lParam);
}

void InstallWindowLog(HWND window) {
    if (!window || g_loggedWindow) return;
    const HMODULE user32 = GetModuleHandleW(L"user32.dll");
    const auto threadOf = reinterpret_cast<DWORD(WINAPI*)(HWND, DWORD*)>(
        user32 ? GetProcAddress(user32, "GetWindowThreadProcessId") : nullptr);
    const auto setHook = reinterpret_cast<HHOOK(WINAPI*)(int, HOOKPROC, HINSTANCE, DWORD)>(
        user32 ? GetProcAddress(user32, "SetWindowsHookExW") : nullptr);
    g_isWindowVisible = reinterpret_cast<decltype(g_isWindowVisible)>(
        user32 ? GetProcAddress(user32, "IsWindowVisible") : nullptr);
    g_isIconic = reinterpret_cast<decltype(g_isIconic)>(user32 ? GetProcAddress(user32, "IsIconic") : nullptr);
    g_callNextHookEx = reinterpret_cast<decltype(g_callNextHookEx)>(
        user32 ? GetProcAddress(user32, "CallNextHookEx") : nullptr);
    if (!threadOf || !setHook || !g_isWindowVisible || !g_isIconic || !g_callNextHookEx) {
        WriteLog("WindowLog: user32 functions not found; not installed");
        return;
    }
    const DWORD thread = threadOf(window, nullptr);
    g_loggedWindow = window;
    const bool sent = setHook(WH_CALLWNDPROC, WindowLogCallProc, nullptr, thread) != nullptr;
    const bool posted = setHook(WH_GETMESSAGE, WindowLogGetMessageProc, nullptr, thread) != nullptr;
    WriteLog("WindowLog: window %p thread %lu, sent-message hook %d, posted-message hook %d", window, thread,
             sent ? 1 : 0, posted ? 1 : 0);
}

HMODULE TryChainCandidate(const wchar_t* path, const char* source) {
    if (_wcsicmp(path, g_proxyPath) == 0) {
        WriteLog("ignored recursive %s chain candidate: %ls", source, path);
        return nullptr;
    }
    HMODULE module = LoadLibraryW(path);
    if (!module) {
        WriteLog("failed %s chain candidate: %ls (Win32 error %lu)", source, path, GetLastError());
        return nullptr;
    }
    if (!GetProcAddress(module, "Direct3DCreate9")) {
        WriteLog("ignored %s chain candidate without Direct3DCreate9: %ls", source, path);
        FreeLibrary(module);
        return nullptr;
    }
    WriteLog("chained %s DLL: %ls", source, path);
    return module;
}

FARPROC ResolveName(const char* name) {
    FARPROC result = GetProcAddress(g_realD3D9, name);
    if (!result && g_systemD3D9 && g_systemD3D9 != g_realD3D9) {
        result = GetProcAddress(g_systemD3D9, name);
    }
    return result;
}

FARPROC ResolveOrdinal(WORD ordinal) {
    FARPROC result = GetProcAddress(g_realD3D9, MAKEINTRESOURCEA(ordinal));
    if (!result && g_systemD3D9 && g_systemD3D9 != g_realD3D9) {
        result = GetProcAddress(g_systemD3D9, MAKEINTRESOURCEA(ordinal));
    }
    return result;
}

template <typename T>
T Resolve(const char* name) {
    return reinterpret_cast<T>(ResolveName(name));
}

// [Proxy] DumpStageTexturesPs / DumpStageTexturesStages (stage_texture_dump.h). Off unless both are set.
void ReadStageTextureDumpKeys(const wchar_t* iniPath, bool log) {
    wchar_t psList[256] = {}, stageList[64] = {};
    GetPrivateProfileStringW(L"Proxy", L"DumpStageTexturesPs", L"", psList, ARRAYSIZE(psList), iniPath);
    GetPrivateProfileStringW(L"Proxy", L"DumpStageTexturesStages", L"", stageList, ARRAYSIZE(stageList), iniPath);
    const uint32_t before = proxy::g_stageDumpStages;
    const size_t psBefore = proxy::g_stageDumpPs.size();
    proxy::SetStageTextureDump(psList, stageList);
    if (log || before != proxy::g_stageDumpStages || psBefore != proxy::g_stageDumpPs.size()) {
        WriteLog("Proxy keys: DumpStageTexturesPs %u ids, DumpStageTexturesStages mask %04X",
                 static_cast<unsigned>(proxy::g_stageDumpPs.size()), proxy::g_stageDumpStages);
    }
}

bool LoadRealD3D9(HINSTANCE proxyInstance) {
    const DWORD proxyLength = GetModuleFileNameW(proxyInstance, g_proxyPath, ARRAYSIZE(g_proxyPath));
    if (proxyLength == 0 || proxyLength >= ARRAYSIZE(g_proxyPath) || !proxy::GetExecutableDirectory()) {
        return false;
    }
    g_executableBase = reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr));
    const wchar_t* iniPath = proxy::IniPath();

    proxy::StartBackgroundLogAndIniServices();
    g_lightsEnabled = GetPrivateProfileIntW(L"Lights", L"Enable", 1, iniPath) != 0;
    proxy::SetLoggingEnabled(GetPrivateProfileIntW(L"Proxy", L"Log", 1, iniPath) != 0);
    g_publishCamera = GetPrivateProfileIntW(L"Proxy", L"PublishCamera", 1, iniPath) != 0;
    g_diagnoseWorldWvp = GetPrivateProfileIntW(L"Proxy", L"DiagnoseWorldWvp", 0, iniPath) != 0;
    g_cameraSources = GetPrivateProfileIntW(L"Proxy", L"CameraSources", 0, iniPath);
    g_cameraOncePerFrame = GetPrivateProfileIntW(L"Proxy", L"CameraOncePerFrame", 1, iniPath) != 0;
    const int debuggingTelemetry = GetPrivateProfileIntW(L"Proxy", L"EnableDebuggingTelemetry", 0, iniPath) != 0 ? 1 : 0;
    g_debuggingTelemetry = debuggingTelemetry != 0;
    proxy::DiagnosticsConfig diag = {};
    diag.frameTrace = GetPrivateProfileIntW(L"Proxy", L"FrameTrace", debuggingTelemetry, iniPath) != 0;
    diag.frameTraceMax = GetPrivateProfileIntW(L"Proxy", L"FrameTraceMax", 500000, iniPath);
    diag.callTiming = diag.frameTrace && GetPrivateProfileIntW(L"Proxy", L"CallTiming", debuggingTelemetry, iniPath) != 0;
    diag.frameTiming = GetPrivateProfileIntW(L"Proxy", L"FrameTiming", 0, iniPath);
    diag.hitchSampler = GetPrivateProfileIntW(L"Proxy", L"HitchSampler", debuggingTelemetry, iniPath) != 0;
    diag.sampleMs = std::clamp(GetPrivateProfileIntW(L"Proxy", L"SampleMs", 5, iniPath), 1u, 1000u);
    diag.hitchMs = GetPrivateProfileIntW(L"Proxy", L"HitchMs", 50, iniPath);
    diag.texStats = diag.callTiming && GetPrivateProfileIntW(L"Proxy", L"TexStats", 0, iniPath) != 0;

    g_presentHook = diag.frameTiming != 0 || diag.hitchSampler ||
                    GetPrivateProfileIntW(L"Proxy", L"PresentHook", 0, iniPath) != 0;
    g_queryMode = GetPrivateProfileIntW(L"Proxy", L"QueryMode", 1, iniPath);
    g_formatGuard = GetPrivateProfileIntW(L"Proxy", L"FormatGuard", 1, iniPath) != 0;
    proxy::SetTextureFillCounts(GetPrivateProfileIntW(L"Proxy", L"TextureFillCounts", debuggingTelemetry, iniPath) != 0);
    WriteLog("Proxy keys: EnableDebuggingTelemetry %d, TextureFillCounts %d", debuggingTelemetry,
             proxy::TextureFillCountsEnabled() ? 1 : 0);

    // Decal hashes are pre-populated offline in rtx.conf (build_materials.py); runtime LockRect tagging stays off unless requested.
    const bool decalTag = GetPrivateProfileIntW(L"Proxy", L"DecalTag", 0, iniPath) != 0;
    const bool hideDecals = GetPrivateProfileIntW(L"Proxy", L"HideDecals", 0, iniPath) != 0;
    const bool texHashLog = GetPrivateProfileIntW(L"Proxy", L"TexHashLog", 0, iniPath) != 0;
    proxy::SetDecalConfig(decalTag, hideDecals, false);
    proxy::SetTexHashLogEnabled(texHashLog);
    proxy::SetStreamBudgetMB(GetPrivateProfileIntW(L"Proxy", L"TextureStreamBudgetMB", 160, iniPath));

    const bool handoffBeforePost = GetPrivateProfileIntW(L"Proxy", L"HandoffBeforePost", 1, iniPath) != 0;
    const int headroomStops =
        std::clamp(static_cast<int>(GetPrivateProfileIntW(L"Proxy", L"HandoffHeadroomStops", 4, iniPath)), 0, 10);
    const bool modernFilter = GetPrivateProfileIntW(L"Proxy", L"ModernFilter", 1, iniPath) != 0;
    const bool raytraceParticles = GetPrivateProfileIntW(L"Proxy", L"RaytraceParticles", 0, iniPath) != 0;
    proxy::SetHandoffConfig(handoffBeforePost, headroomStops, modernFilter, false);
    proxy::SetParticleTagEnabled(raytraceParticles, false);

    char gateDump[128] = {};
    wchar_t wGateDump[128] = {};
    GetPrivateProfileStringW(L"Proxy", L"GateDump", L"", wGateDump, ARRAYSIZE(wGateDump), iniPath);
    WideCharToMultiByte(CP_UTF8, 0, wGateDump, -1, gateDump, sizeof(gateDump), nullptr, nullptr);
    proxy::SetGateDumpTag(gateDump);
    proxy::SetCaptureDrawsConfig(
        GetPrivateProfileIntW(L"Proxy", L"CaptureDraws", 0, iniPath) != 0,
        static_cast<int>(GetPrivateProfileIntW(L"Proxy", L"CaptureBurst", 0, iniPath)));
    ReadStageTextureDumpKeys(iniPath, true);

    g_filterCfg.passFilter = GetPrivateProfileIntW(L"Proxy", L"PassFilter", 1, iniPath) != 0;
    g_filterCfg.forwardScene = GetPrivateProfileIntW(L"Proxy", L"ForwardScene", 1, iniPath) != 0;
    g_filterCfg.skipShadowMapDraws = GetPrivateProfileIntW(L"Proxy", L"SkipShadowMapDraws", 1, iniPath) != 0;
    g_filterCfg.skipGameLighting = GetPrivateProfileIntW(L"Proxy", L"SkipGameLighting", 1, iniPath) != 0;
    g_filterCfg.skipGamePost = GetPrivateProfileIntW(L"Proxy", L"SkipGamePost", 1, iniPath) != 0;
    g_filterCfg.gogglesGameView = GetPrivateProfileIntW(L"Proxy", L"GogglesGameView", 1, iniPath) != 0;
    g_filterCfg.handoffBeforePost = handoffBeforePost;
    g_filterCfg.handoffBeforeEffects = GetPrivateProfileIntW(L"Proxy", L"HandoffBeforeEffects", 1, iniPath) != 0;
    g_filterCfg.packedTexcoordFix = GetPrivateProfileIntW(L"Proxy", L"PackedTexcoordFix", 0, iniPath) != 0;
    g_filterCfg.modernFilter = modernFilter;
    g_filterCfg.raytraceParticles = raytraceParticles;
    g_filterCfg.alphaTestFromTexkill = GetPrivateProfileIntW(L"Proxy", L"AlphaTestFromTexkill", 1, iniPath) != 0;
    g_filterCfg.alphaTestRef = GetPrivateProfileIntW(L"Proxy", L"AlphaTestRef", 128, iniPath);
    proxy::SetFilterConfig(g_filterCfg);
    {
        LARGE_INTEGER frequency;
        QueryPerformanceFrequency(&frequency);
        g_queryPollTicks = frequency.QuadPart * GetPrivateProfileIntW(L"Proxy", L"QueryPollMs", 2, iniPath) / 1000;
    }
    proxy::ConfigureDiagnostics(&diag);

    WriteLog("camera publication %s; preserves shader projection depth convention",
             g_publishCamera ? "enabled" : "disabled for diagnostic control");
    WriteLog("diagnostics: EnableDebuggingTelemetry=%d FrameTrace=%d FrameTraceMax=%u CameraSources=%u "
             "CameraOncePerFrame=%d CallTiming=%d",
             g_debuggingTelemetry ? 1 : 0, diag.frameTrace ? 1 : 0, diag.frameTraceMax, g_cameraSources,
             g_cameraOncePerFrame ? 1 : 0, diag.callTiming ? 1 : 0);
    WriteLog("features: FrameTiming=%u QueryMode=%u PresentHook=%d FormatGuard=%d TexStats=%d TexHashLog=%d "
             "HitchSampler=%d", diag.frameTiming, g_queryMode, g_presentHook ? 1 : 0, g_formatGuard ? 1 : 0,
             diag.texStats ? 1 : 0, texHashLog ? 1 : 0, diag.hitchSampler ? 1 : 0);
    proxy::InstallLoopTimer(g_executableBase);

    wchar_t configured[MAX_PATH] = {};
    GetPrivateProfileStringW(L"Proxy", L"ChainDll", L"", configured, ARRAYSIZE(configured), iniPath);
    wchar_t configuredPath[MAX_PATH] = {};
    if (configured[0] != L'\0') {
        if (proxy::ResolveConfiguredPath(configured, configuredPath)) {
            g_realD3D9 = TryChainCandidate(configuredPath, "INI");
        } else {
            WriteLog("invalid INI ChainDll path: %ls", configured);
        }
    }

    wchar_t remixPath[MAX_PATH];
    if (!g_realD3D9 && proxy::CombinePath(proxy::ExecutableDirectory(), L"d3d9_remix.dll", remixPath)) {
        if (configuredPath[0] == L'\0' || _wcsicmp(configuredPath, remixPath) != 0) {
            g_realD3D9 = TryChainCandidate(remixPath, "RTX Remix");
        }
    }

    wchar_t systemPath[MAX_PATH];
    if (!proxy::GetSystemD3D9Path(systemPath)) {
        WriteLog("could not form the system d3d9.dll path");
        return false;
    }
    if (!g_realD3D9) g_realD3D9 = TryChainCandidate(systemPath, "system fallback");
    if (!g_realD3D9) return false;
    if (GetPrivateProfileIntW(L"Proxy", L"FocusGuard", 1, iniPath) != 0) InstallFocusGuard(g_realD3D9);

    if (_wcsicmp(systemPath, g_proxyPath) != 0) g_systemD3D9 = LoadLibraryW(systemPath);
    if (!g_systemD3D9 && _wcsicmp(systemPath, g_proxyPath) == 0) g_systemD3D9 = g_realD3D9;

    g_Ordinal16 = ResolveOrdinal(16); g_Ordinal17 = ResolveOrdinal(17);
    g_Ordinal18 = ResolveOrdinal(18); g_Ordinal19 = ResolveOrdinal(19);
    g_Direct3DCreate9On12 = Resolve<FARPROC>("Direct3DCreate9On12");
    g_Direct3DCreate9On12Ex = Resolve<FARPROC>("Direct3DCreate9On12Ex");
    g_Ordinal22 = ResolveOrdinal(22); g_Ordinal23 = ResolveOrdinal(23);
    g_Direct3DShaderValidatorCreate9 = Resolve<FARPROC>("Direct3DShaderValidatorCreate9");
    g_PSGPError = Resolve<FARPROC>("PSGPError");
    g_PSGPSampleTexture = Resolve<FARPROC>("PSGPSampleTexture");
    g_D3DPERF_BeginEvent = Resolve<FARPROC>("D3DPERF_BeginEvent");
    g_D3DPERF_EndEvent = Resolve<FARPROC>("D3DPERF_EndEvent");
    g_D3DPERF_GetStatus = Resolve<FARPROC>("D3DPERF_GetStatus");
    g_D3DPERF_QueryRepeatFrame = Resolve<FARPROC>("D3DPERF_QueryRepeatFrame");
    g_D3DPERF_SetMarker = Resolve<FARPROC>("D3DPERF_SetMarker");
    g_D3DPERF_SetOptions = Resolve<FARPROC>("D3DPERF_SetOptions");
    g_D3DPERF_SetRegion = Resolve<FARPROC>("D3DPERF_SetRegion");
    g_DebugSetLevel = Resolve<FARPROC>("DebugSetLevel");
    g_DebugSetMute = Resolve<FARPROC>("DebugSetMute");
    g_Direct3D9EnableMaximizedWindowedModeShim = Resolve<FARPROC>("Direct3D9EnableMaximizedWindowedModeShim");
    g_Direct3DCreate9 = Resolve<Direct3DCreate9Fn>("Direct3DCreate9");
    g_Direct3DCreate9Ex = Resolve<Direct3DCreate9ExFn>("Direct3DCreate9Ex");

    struct RequiredExport { const char* name; FARPROC address; };
    const RequiredExport required[] = {
        {"ordinal 16", g_Ordinal16}, {"ordinal 17", g_Ordinal17}, {"ordinal 18", g_Ordinal18}, {"ordinal 19", g_Ordinal19},
        {"Direct3DCreate9On12", g_Direct3DCreate9On12}, {"Direct3DCreate9On12Ex", g_Direct3DCreate9On12Ex},
        {"ordinal 22", g_Ordinal22}, {"ordinal 23", g_Ordinal23},
        {"Direct3DShaderValidatorCreate9", g_Direct3DShaderValidatorCreate9},
        {"PSGPError", g_PSGPError}, {"PSGPSampleTexture", g_PSGPSampleTexture},
        {"D3DPERF_BeginEvent", g_D3DPERF_BeginEvent}, {"D3DPERF_EndEvent", g_D3DPERF_EndEvent},
        {"D3DPERF_GetStatus", g_D3DPERF_GetStatus}, {"D3DPERF_QueryRepeatFrame", g_D3DPERF_QueryRepeatFrame},
        {"D3DPERF_SetMarker", g_D3DPERF_SetMarker}, {"D3DPERF_SetOptions", g_D3DPERF_SetOptions},
        {"D3DPERF_SetRegion", g_D3DPERF_SetRegion}, {"DebugSetLevel", g_DebugSetLevel}, {"DebugSetMute", g_DebugSetMute},
        {"Direct3D9EnableMaximizedWindowedModeShim", g_Direct3D9EnableMaximizedWindowedModeShim},
        {"Direct3DCreate9", reinterpret_cast<FARPROC>(g_Direct3DCreate9)},
        {"Direct3DCreate9Ex", reinterpret_cast<FARPROC>(g_Direct3DCreate9Ex)}};
    bool complete = true;
    for (const RequiredExport& item : required) {
        if (!item.address) {
            WriteLog("FATAL unresolved forwarded export: %s", item.name);
            complete = false;
        }
    }
    if (complete) {
        proxy::InstallCrashHandler();
        WriteLog("proxy initialized outside DllMain; camera decomposition logging is "
                 "rate-limited to one accepted and one rejected entry per device every 5 seconds");
    }
    return complete;
}

BOOL CALLBACK InitializeProxyOnce(PINIT_ONCE, PVOID, PVOID*) {
    return LoadRealD3D9(g_proxyInstance) ? TRUE : FALSE;
}

bool EnsureInitialized() {
    return InitOnceExecuteOnce(&g_initializationOnce, InitializeProxyOnce, nullptr, nullptr) != FALSE;
}

__declspec(noreturn) void __cdecl FailMissingForwardTarget(unsigned exportIndex) {
    static const char message[] = "d3d9 proxy initialization failed before a forwarded export could run";
    if (proxy::LogPath()[0]) WriteLog("FATAL %s (export index %u)", message, exportIndex);
    OutputDebugStringA(message);
    TerminateProcess(GetCurrentProcess(), ERROR_PROC_NOT_FOUND);
    __assume(0);
}

void __cdecl EnsureInitializedForForwarder(unsigned exportIndex) {
    if (!EnsureInitialized()) FailMissingForwardTarget(exportIndex);
}

DeviceHook* FindDeviceHook(IDirect3DDevice9* object) {
    if (!object) return nullptr;
    void** vtable = *reinterpret_cast<void***>(object);
    static DeviceHook* volatile lastHit = nullptr;
    DeviceHook* const cached = lastHit;
    if (cached && cached->vtable == vtable) return cached;
    AcquireSRWLockShared(&g_hookListLock);
    DeviceHook* result = g_deviceHooks;
    while (result && result->vtable != vtable) result = result->next;
    ReleaseSRWLockShared(&g_hookListLock);
    if (result) lastHit = result;
    return result;
}

// --- Setter & state hooks ---
HRESULT WINAPI HookSetRenderTarget(IDirect3DDevice9* self, DWORD index, IDirect3DSurface9* surface) {
    proxy::OnSetRenderTarget(self, index, surface);
    return g_setRenderTarget(self, index, surface);
}

HRESULT WINAPI HookSetTexture(IDirect3DDevice9* self, DWORD stage, IDirect3DBaseTexture9* texture) {
    if (proxy::TrySuppressSetTexture(self, stage, texture)) return D3D_OK;
    return g_setTexture(self, stage, texture);
}

HRESULT WINAPI HookSetRenderState(IDirect3DDevice9* self, D3DRENDERSTATETYPE state, DWORD value) {
    if (proxy::TrySuppressSetRenderState(self, state, value)) return D3D_OK;
    return g_setRenderState(self, state, value);
}

HRESULT WINAPI HookSetStreamSource(IDirect3DDevice9* self, UINT stream, IDirect3DVertexBuffer9* vb, UINT offset, UINT stride) {
    if (proxy::TrySuppressSetStreamSource(self, stream, vb, offset, stride)) return D3D_OK;
    return g_setStreamSource(self, stream, vb, offset, stride);
}

HRESULT WINAPI HookSetIndices(IDirect3DDevice9* self, IDirect3DIndexBuffer9* ib) {
    if (proxy::TrySuppressSetIndices(self, ib)) return D3D_OK;
    return g_setIndices(self, ib);
}

HRESULT WINAPI HookSetPixelShaderConstantF(IDirect3DDevice9* self, UINT startReg, const float* data, UINT vec4Count) {
    if (proxy::TrySuppressSetPixelShaderConstantF(self, startReg, data, vec4Count)) return D3D_OK;
    return g_setPixelShaderConstantF(self, startReg, data, vec4Count);
}

HRESULT WINAPI HookSetVertexShader(IDirect3DDevice9* self, IDirect3DVertexShader9* shader) {
    const HRESULT hr = g_setVertexShader(self, shader);
    if (SUCCEEDED(hr)) proxy::OnVertexShaderSet(self, shader);
    return hr;
}

HRESULT WINAPI HookSetPixelShader(IDirect3DDevice9* self, IDirect3DPixelShader9* shader) {
    const HRESULT hr = g_setPixelShader(self, shader);
    if (SUCCEEDED(hr)) proxy::OnPixelShaderSet(self, shader);
    return hr;
}

HRESULT WINAPI HookReset(IDirect3DDevice9* self, D3DPRESENT_PARAMETERS* parameters) {
    proxy::HandoffOnDeviceReset();
    proxy::FilterOnDeviceReset();
    return g_reset(self, parameters);
}

void ReloadLiveIniSwitches() {
    const wchar_t* iniPath = proxy::IniPath();
    proxy::ReloadSkipVsLists();

    const bool passFilter = GetPrivateProfileIntW(L"Proxy", L"PassFilter", 1, iniPath) != 0;
    const bool forwardScene = GetPrivateProfileIntW(L"Proxy", L"ForwardScene", 1, iniPath) != 0;
    if (passFilter != g_filterCfg.passFilter || forwardScene != g_filterCfg.forwardScene)
        WriteLog("PassFilter: now %d, ForwardScene %d", passFilter ? 1 : 0, forwardScene ? 1 : 0);
    g_filterCfg.passFilter = passFilter;
    g_filterCfg.forwardScene = forwardScene;

    const bool decalTag = GetPrivateProfileIntW(L"Proxy", L"DecalTag", 0, iniPath) != 0;
    const bool hideDecals = GetPrivateProfileIntW(L"Proxy", L"HideDecals", 0, iniPath) != 0;
    proxy::SetDecalConfig(decalTag, hideDecals, true);

    const bool gogglesGameView = GetPrivateProfileIntW(L"Proxy", L"GogglesGameView", 1, iniPath) != 0;
    if (gogglesGameView != g_filterCfg.gogglesGameView) WriteLog("Goggles: GogglesGameView now %d", gogglesGameView ? 1 : 0);
    g_filterCfg.gogglesGameView = gogglesGameView;

    proxy::SetStreamBudgetMB(
        std::clamp(static_cast<int>(GetPrivateProfileIntW(L"Proxy", L"TextureStreamBudgetMB", 160, iniPath)), 0, 1024));

    const bool handoffBeforePost = GetPrivateProfileIntW(L"Proxy", L"HandoffBeforePost", 1, iniPath) != 0;
    const int headroomStops =
        std::clamp(static_cast<int>(GetPrivateProfileIntW(L"Proxy", L"HandoffHeadroomStops", 4, iniPath)), 0, 10);
    const bool modernFilter = GetPrivateProfileIntW(L"Proxy", L"ModernFilter", 1, iniPath) != 0;
    proxy::SetHandoffConfig(handoffBeforePost, headroomStops, modernFilter, true);
    g_filterCfg.handoffBeforePost = handoffBeforePost;
    g_filterCfg.modernFilter = modernFilter;

    const bool raytraceParticles = GetPrivateProfileIntW(L"Proxy", L"RaytraceParticles", 0, iniPath) != 0;
    proxy::SetParticleTagEnabled(raytraceParticles, true);
    g_filterCfg.raytraceParticles = raytraceParticles;

    char gateDump[128] = {};
    wchar_t wGateDump[128] = {};
    GetPrivateProfileStringW(L"Proxy", L"GateDump", L"", wGateDump, ARRAYSIZE(wGateDump), iniPath);
    WideCharToMultiByte(CP_UTF8, 0, wGateDump, -1, gateDump, sizeof(gateDump), nullptr, nullptr);
    proxy::SetGateDumpTag(gateDump);
    proxy::SetCaptureDrawsConfig(
        GetPrivateProfileIntW(L"Proxy", L"CaptureDraws", 0, iniPath) != 0,
        static_cast<int>(GetPrivateProfileIntW(L"Proxy", L"CaptureBurst", 0, iniPath)));
    ReadStageTextureDumpKeys(iniPath, false);

    const bool handoffBeforeEffects = GetPrivateProfileIntW(L"Proxy", L"HandoffBeforeEffects", 1, iniPath) != 0;
    if (handoffBeforeEffects != g_filterCfg.handoffBeforeEffects)
        WriteLog("HandoffBeforePost: HandoffBeforeEffects now %d", handoffBeforeEffects ? 1 : 0);
    g_filterCfg.handoffBeforeEffects = handoffBeforeEffects;
    const bool skipGameLighting = GetPrivateProfileIntW(L"Proxy", L"SkipGameLighting", 1, iniPath) != 0;
    if (skipGameLighting != g_filterCfg.skipGameLighting) WriteLog("SkipGameLighting: now %d", skipGameLighting ? 1 : 0);
    g_filterCfg.skipGameLighting = skipGameLighting;

    const bool skipGamePost = GetPrivateProfileIntW(L"Proxy", L"SkipGamePost", 1, iniPath) != 0;
    if (skipGamePost != g_filterCfg.skipGamePost) WriteLog("SkipGameLighting: SkipGamePost now %d", skipGamePost ? 1 : 0);
    g_filterCfg.skipGamePost = skipGamePost;

    const bool alphaTestFromTexkill = GetPrivateProfileIntW(L"Proxy", L"AlphaTestFromTexkill", 1, iniPath) != 0;
    if (alphaTestFromTexkill != g_filterCfg.alphaTestFromTexkill)
        WriteLog("AlphaTestFromTexkill: now %d", alphaTestFromTexkill ? 1 : 0);
    g_filterCfg.alphaTestFromTexkill = alphaTestFromTexkill;

    proxy::SetSkinTraceEnabled(GetPrivateProfileIntW(L"Proxy", L"SkinTrace", 0, iniPath) != 0);
    proxy::SetFilterConfig(g_filterCfg);
}

// --- Present, Draw, and Camera constant hooks ---
HRESULT WINAPI HookPresent(IDirect3DDevice9* self, const RECT* sourceRect,
                           const RECT* destRect, HWND destWindow, const RGNDATA* dirtyRegion) {
    DeviceHook* hook = FindDeviceHook(self);
    if (!hook || !hook->present) return D3DERR_INVALIDCALL;

    if (g_lightsEnabled && !g_lightPublisherTried) {
        g_lightPublisherTried = true;
        lightpub::Initialize(g_realD3D9, proxy::IniPath(), &WriteLog);
        lightpub::SetRemixConfig("rtx.writeFrameTelemetry", g_debuggingTelemetry ? "True" : "False");
        if (proxy::IsHandoffBeforePostEnabled() || proxy::IsModernFilterEnabled()) proxy::ApplyHandoffOverrides();
    }
    lightpub::SetShowGamePicture(proxy::ShouldShowGamePicture());
    if (g_lightsEnabled) lightpub::OnPresent();
    proxy::FilterEndFrameBeforePresent(self, hook, g_cameraTracker, g_presentCount);
    proxy::DecalTagEndFrame();
    proxy::ParticleTagEndFrame();
    proxy::TexHashLogEndFrame(g_presentCount);
    if (!(g_presentCount % 60)) proxy::ApplyStreamBudget(g_presentCount);

    LARGE_INTEGER entry;
    QueryPerformanceCounter(&entry);
    if (proxy::IsHitchSamplerEnabled()) proxy::HitchSamplerFrame(entry.QuadPart);
    const HRESULT result = proxy::CallPresentTagged(
        [&] { return hook->present(self, sourceRect, destRect, destWindow, dirtyRegion); });
    LARGE_INTEGER exit;
    QueryPerformanceCounter(&exit);
    if (proxy::IsFrameTraceEnabled()) {
        proxy::FinishTracedFrame(hook, entry.QuadPart, exit.QuadPart, proxy::SnapshotGateFrameMetrics());
    }

    const LONG presentCount = InterlockedIncrement(&g_presentCount);
    proxy::MaybeDumpTexStats(exit.QuadPart, presentCount);

    hook->publishedThisFrame = false;
    const bool gogglesNow = proxy::IsGogglesActive();
    if (gogglesNow != g_prevGogglesLogged) {
        WriteLog("Goggles: %s (game view %s)", gogglesNow ? "on" : "off",
                 gogglesNow ? "while on, Remix scene kept" : "ends, ray tracing returns next frame");
        g_prevGogglesLogged = gogglesNow;
    }
    proxy::FilterResetAfterPresent(self);
    if (proxy::ConsumeIniChanged()) ReloadLiveIniSwitches();
    proxy::RefreshBackbuffer(self);
    g_cameraTracker.BeginFrame(proxy::BackbufferWidth(), proxy::BackbufferHeight());
    proxy::LogFilterStatsPeriodic(presentCount, &g_focusMessagesDropped);
    proxy::UpdateCaptureDrawsEndFrame(presentCount);
    return result;
}

HRESULT WINAPI HookDrawPrimitive(IDirect3DDevice9* self, D3DPRIMITIVETYPE type,
                                 UINT startVertex, UINT primitiveCount) {
    DeviceHook* hook = FindDeviceHook(self);
    if (!hook || !hook->drawPrimitive) return D3DERR_INVALIDCALL;
    InterlockedIncrement(&hook->frame.draws);
    const proxy::DrawGeometryKey geometry = proxy::MakeDrawGeometry(false, 0, 0, startVertex, primitiveCount, type);
    if (!proxy::PrepareDraw(self, hook, g_cameraTracker, &geometry, "draw", 0, primitiveCount,
                            _ReturnAddress(), g_presentCount)) {
        return D3D_OK;
    }
    proxy::AlphaTestHint alphaTestHint(self);
    CallTimer timer(proxy::kDraw);
    return hook->drawPrimitive(self, type, startVertex, primitiveCount);
}

HRESULT WINAPI HookDrawIndexedPrimitive(IDirect3DDevice9* self, D3DPRIMITIVETYPE type,
                                        INT baseVertex, UINT minIndex, UINT vertexCount,
                                        UINT startIndex, UINT primitiveCount) {
    DeviceHook* hook = FindDeviceHook(self);
    if (!hook || !hook->drawIndexedPrimitive) return D3DERR_INVALIDCALL;
    InterlockedIncrement(&hook->frame.draws);
    const proxy::DrawGeometryKey geometry =
        proxy::MakeDrawGeometry(true, baseVertex, minIndex, startIndex, primitiveCount, type);
    if (!proxy::PrepareDraw(self, hook, g_cameraTracker, &geometry, "indexed", vertexCount, primitiveCount,
                            _ReturnAddress(), g_presentCount)) {
        return D3D_OK;
    }
    proxy::AlphaTestHint alphaTestHint(self);
    proxy::CloseCameraFadeGuard fadeGuard(self);
    if (fadeGuard.dropDraw) return D3D_OK;
    CallTimer timer(proxy::kDraw);
    return hook->drawIndexedPrimitive(self, type, baseVertex, minIndex, vertexCount, startIndex, primitiveCount);
}

HRESULT WINAPI HookDrawPrimitiveUP(IDirect3DDevice9* self, D3DPRIMITIVETYPE type,
                                   UINT primitiveCount, const void* vertexData, UINT vertexStride) {
    DeviceHook* hook = FindDeviceHook(self);
    if (!hook || !hook->drawPrimitiveUP) return D3DERR_INVALIDCALL;
    InterlockedIncrement(&hook->frame.draws);
    if (!proxy::PrepareDraw(self, hook, g_cameraTracker, nullptr, "up", 0, primitiveCount,
                            _ReturnAddress(), g_presentCount)) {
        return D3D_OK;
    }
    proxy::AlphaTestHint alphaTestHint(self);
    CallTimer timer(proxy::kDraw);
    const HRESULT result = hook->drawPrimitiveUP(self, type, primitiveCount, vertexData, vertexStride);
    proxy::OnAfterUpDraw(false);
    return result;
}

HRESULT WINAPI HookDrawIndexedPrimitiveUP(IDirect3DDevice9* self, D3DPRIMITIVETYPE type,
                                          UINT minIndex, UINT vertexCount, UINT primitiveCount,
                                          const void* indexData, D3DFORMAT indexFormat,
                                          const void* vertexData, UINT vertexStride) {
    DeviceHook* hook = FindDeviceHook(self);
    if (!hook || !hook->drawIndexedPrimitiveUP) return D3DERR_INVALIDCALL;
    InterlockedIncrement(&hook->frame.draws);
    if (!proxy::PrepareDraw(self, hook, g_cameraTracker, nullptr, "indexed-up", vertexCount, primitiveCount,
                            _ReturnAddress(), g_presentCount)) {
        return D3D_OK;
    }
    proxy::AlphaTestHint alphaTestHint(self);
    CallTimer timer(proxy::kDraw);
    const HRESULT result = hook->drawIndexedPrimitiveUP(self, type, minIndex, vertexCount, primitiveCount,
                                                        indexData, indexFormat, vertexData, vertexStride);
    proxy::OnAfterUpDraw(true);
    return result;
}

HRESULT WINAPI HookSetVertexShaderConstantF(IDirect3DDevice9* self, UINT startRegister,
                                            const float* constantData, UINT vector4Count) {
    DeviceHook* hook = FindDeviceHook(self);
    if (!hook) return D3DERR_INVALIDCALL;
    const HRESULT result = hook->setVertexShaderConstantF(self, startRegister, constantData, vector4Count);
    if (FAILED(result) || !constantData || vector4Count == 0 || !g_publishCamera) return result;

    const uintptr_t returnAddress = reinterpret_cast<uintptr_t>(_ReturnAddress());
    const uintptr_t callerRva = g_executableBase != 0 && returnAddress >= g_executableBase
        ? returnAddress - g_executableBase
        : static_cast<uintptr_t>(-1);
    const bool sourceAllowed =
        g_cameraSources == 0 ||
        (g_cameraSources == 1 && callerRva == proxy::kGameplayCameraUploadReturnRva) ||
        (g_cameraSources == 2 && callerRva == proxy::kPureCameraUploadReturnRva);
    const bool pureCameraUpload =
        startRegister == 8 && vector4Count == 4 &&
        proxy::IsDirectCameraUploadReturn(callerRva) && sourceAllowed;
    if (!pureCameraUpload && !g_diagnoseWorldWvp) return result;

    uint32_t touchedRegisters = 0;
    for (UINT i = 0; i < vector4Count; ++i) {
        const uint64_t reg64 = static_cast<uint64_t>(startRegister) + i;
        if (reg64 >= 5 && reg64 < 12) touchedRegisters |= 1u << static_cast<UINT>(reg64);
    }
    bool ready = false;
    float constants[12][4] = {};
    UploadInfo upload = {};
    AcquireSRWLockExclusive(&hook->constantsLock);
    if ((touchedRegisters & (1u << 5)) != 0) {
        hook->pendingRegisters &= ~kRequiredRegisterMask;
        hook->candidateCallCount = 0;
        hook->capturedUploadCount = 0;
    } else if ((touchedRegisters & (1u << 8)) != 0) {
        hook->pendingRegisters &= ~kWvpRegisterMask;
    }
    if (touchedRegisters != 0) {
        ++hook->candidateCallCount;
        if (hook->capturedUploadCount < ARRAYSIZE(hook->candidateUploadStart)) {
            const UINT index = hook->capturedUploadCount++;
            hook->candidateUploadStart[index] = startRegister;
            hook->candidateUploadVectorCount[index] = vector4Count;
        }
        if ((touchedRegisters & kWorldRegisterMask) != 0) {
            hook->lastWorldUploadStart = startRegister;
            hook->lastWorldUploadCount = vector4Count;
        }
        if ((touchedRegisters & kWvpRegisterMask) != 0) {
            hook->lastWvpUploadStart = startRegister;
            hook->lastWvpUploadCount = vector4Count;
        }
    }
    for (UINT i = 0; i < vector4Count; ++i) {
        const uint64_t reg64 = static_cast<uint64_t>(startRegister) + i;
        if (reg64 >= 5 && reg64 < 12) {
            const UINT reg = static_cast<UINT>(reg64);
            CopyMemory(hook->constants[reg], constantData + i * 4, sizeof(float) * 4);
            hook->validRegisters |= 1u << reg;
            hook->pendingRegisters |= 1u << reg;
        }
    }
    ready = pureCameraUpload || (hook->pendingRegisters & kRequiredRegisterMask) == kRequiredRegisterMask;
    if (ready) {
        CopyMemory(constants, hook->constants, sizeof(constants));
        upload.validRegisters = hook->validRegisters;
        upload.pendingRegisters = hook->pendingRegisters;
        upload.candidateCallCount = hook->candidateCallCount;
        upload.capturedUploadCount = hook->capturedUploadCount;
        CopyMemory(upload.candidateUploadStart, hook->candidateUploadStart, sizeof(upload.candidateUploadStart));
        CopyMemory(upload.candidateUploadVectorCount, hook->candidateUploadVectorCount,
                   sizeof(upload.candidateUploadVectorCount));
        upload.lastWorldUploadStart = hook->lastWorldUploadStart;
        upload.lastWorldUploadCount = hook->lastWorldUploadCount;
        upload.lastWvpUploadStart = hook->lastWvpUploadStart;
        upload.lastWvpUploadCount = hook->lastWvpUploadCount;
        upload.callerRva = callerRva;
        upload.pureCameraUpload = pureCameraUpload;
        upload.dumpRawConstants = !hook->dumpedRejectedFrame;
        hook->pendingRegisters &= ~kRequiredRegisterMask;
        hook->candidateCallCount = 0;
        hook->capturedUploadCount = 0;
    }
    ReleaseSRWLockExclusive(&hook->constantsLock);

    if (ready) {
        D3DMATRIX world = {}, view = {}, projection = {};
        proxy::CameraRejection rejection = {};
        bool recovered = false;
        bool shouldPublish = false;
        if (pureCameraUpload) {
            if (!proxy::BackbufferWidth()) {
                proxy::CurrentBackbuffer(self);
                g_cameraTracker.BeginFrame(proxy::BackbufferWidth(), proxy::BackbufferHeight());
            }
            recovered = g_cameraTracker.EvaluateDirectUpload(
                &constants[8], g_cameraOncePerFrame, &view, &projection, &shouldPublish, &rejection);
            world.m[0][0] = world.m[1][1] = world.m[2][2] = world.m[3][3] = 1.0f;
        } else {
            recovered = proxy::RecoverTransformsFromConstants(constants, &world, &view, &projection, &rejection);
            if (recovered) {
                rejection = {"direct camera caller RVA (must match allowlist)",
                             static_cast<double>(callerRva),
                             static_cast<double>(proxy::kGameplayCameraUploadReturnRva)};
                recovered = false;
            }
        }
        const bool skipPublish = recovered && pureCameraUpload && !shouldPublish;
        if (recovered && pureCameraUpload) {
            hook->publishedThisFrame = true;
            if (proxy::IsFrameTraceEnabled()) proxy::RecordTracedPublish(hook, callerRva, view, constants, skipPublish);
            if (proxy::IsCaptureDrawsActive()) proxy::CaptureCameraPublish(callerRva, view, projection, skipPublish);
        }
        if (recovered && skipPublish) {
            // Redundant intra-frame gameplay camera re-upload suppressed.
        } else if (recovered) {
            const HRESULT worldResult = hook->setTransform(self, D3DTS_WORLD, &world);
            const HRESULT viewResult = hook->setTransform(self, D3DTS_VIEW, &view);
            const HRESULT projectionResult = hook->setTransform(self, D3DTS_PROJECTION, &projection);
            proxy::LogCameraResult(hook, self, true, &world, &projection,
                                   worldResult, viewResult, projectionResult, nullptr, &upload, constants);
        } else {
            if (upload.dumpRawConstants) {
                AcquireSRWLockExclusive(&hook->constantsLock);
                if (hook->dumpedRejectedFrame) upload.dumpRawConstants = false;
                else hook->dumpedRejectedFrame = true;
                ReleaseSRWLockExclusive(&hook->constantsLock);
            }
            proxy::LogCameraResult(hook, self, false, nullptr, nullptr,
                                   S_OK, S_OK, S_OK, &rejection, &upload, constants);
        }
    }
    return result;
}

// --- Vtable patching & resource/query hooks ---
bool PatchVtableSlot(void** vtable, size_t slot, void* replacement, void** previous) {
    if (!vtable || !replacement || !previous) return false;
    DWORD oldProtection;
    if (!VirtualProtect(&vtable[slot], sizeof(void*), PAGE_EXECUTE_READWRITE, &oldProtection)) {
        WriteLog("VirtualProtect failed for vtable %p slot %u (error %lu)",
                 vtable, static_cast<unsigned>(slot), GetLastError());
        return false;
    }
    *previous = InterlockedExchangePointer(reinterpret_cast<PVOID volatile*>(&vtable[slot]), replacement);
    FlushInstructionCache(GetCurrentProcess(), &vtable[slot], sizeof(void*));
    DWORD ignored;
    if (!VirtualProtect(&vtable[slot], sizeof(void*), oldProtection, &ignored)) {
        WriteLog("restoring protection failed for vtable %p slot %u (error %lu)",
                 vtable, static_cast<unsigned>(slot), GetLastError());
    }
    return true;
}

struct MethodSlot { size_t slot; void* replacement; void** saved; };
SRWLOCK g_objectHookLock = SRWLOCK_INIT;

void HookObjectMethods(void* object, const MethodSlot* slots, size_t count) {
    void** vtable = *reinterpret_cast<void***>(object);
    AcquireSRWLockExclusive(&g_objectHookLock);
    for (size_t i = 0; i < count; ++i) {
        void* current = vtable[slots[i].slot];
        if (current == slots[i].replacement) continue;
        if (*slots[i].saved && *slots[i].saved != current) {
            WriteLog("CallTiming: second implementation %p for slot %u on vtable %p left untimed",
                     current, static_cast<unsigned>(slots[i].slot), vtable);
            continue;
        }
        void* saved = nullptr;
        if (PatchVtableSlot(vtable, slots[i].slot, slots[i].replacement, &saved) && saved) {
            *slots[i].saved = saved;
            WriteLog("CallTiming: hooked vtable %p slot %u original=%p",
                     vtable, static_cast<unsigned>(slots[i].slot), saved);
        }
    }
    ReleaseSRWLockExclusive(&g_objectHookLock);
}

ULONG WINAPI HookVertexShaderRelease(IUnknown* self) {
    const ULONG remaining = g_vertexShaderRelease(self);
    if (!remaining) proxy::ForgetShader(self);
    return remaining;
}

ULONG WINAPI HookPixelShaderRelease(IUnknown* self) {
    const ULONG remaining = g_pixelShaderRelease(self);
    if (!remaining) proxy::ForgetShader(self);
    return remaining;
}

void HookShaderRelease(void* shader, bool vertex) {
    const MethodSlot slots[] = {
        {2, vertex ? reinterpret_cast<void*>(&HookVertexShaderRelease) : reinterpret_cast<void*>(&HookPixelShaderRelease),
         vertex ? reinterpret_cast<void**>(&g_vertexShaderRelease) : reinterpret_cast<void**>(&g_pixelShaderRelease)},
    };
    HookObjectMethods(shader, slots, ARRAYSIZE(slots));
}

HRESULT WINAPI HookLockRect(IDirect3DTexture9* self, UINT level, D3DLOCKED_RECT* locked,
                            const RECT* rect, DWORD flags) {
    CallTimer timer(proxy::kTextureLock);
    const auto lock = [&] { return g_lockRect(self, level, locked, rect, flags); };
    if (!proxy::IsTexStatsEnabled()) return proxy::LockRectCounted(self, lock);
    LARGE_INTEGER start, end;
    QueryPerformanceCounter(&start);
    const HRESULT result = proxy::LockRectCounted(self, lock);
    QueryPerformanceCounter(&end);
    proxy::RecordTextureLock(_ReturnAddress(), self, level, rect, flags,
                             proxy::QpcToMilliseconds(end.QuadPart - start.QuadPart), g_presentCount);
    return result;
}

HRESULT WINAPI HookUnlockRect(IDirect3DTexture9* self, UINT level) {
    CallTimer timer(proxy::kTextureUnlock);
    const HRESULT result = g_unlockRect(self, level);
    proxy::OnTextureUnlockRect(self, level, result);
    return result;
}

HRESULT WINAPI HookVertexBufferLock(IUnknown* self, UINT offset, UINT size, void** data, DWORD flags) {
    CallTimer timer(proxy::kBufferLock);
    return g_vertexBufferLock(self, offset, size, data, flags);
}

HRESULT WINAPI HookVertexBufferUnlock(IUnknown* self) {
    CallTimer timer(proxy::kBufferUnlock);
    return g_vertexBufferUnlock(self);
}

HRESULT WINAPI HookIndexBufferLock(IUnknown* self, UINT offset, UINT size, void** data, DWORD flags) {
    CallTimer timer(proxy::kBufferLock);
    return g_indexBufferLock(self, offset, size, data, flags);
}

HRESULT WINAPI HookIndexBufferUnlock(IUnknown* self) {
    CallTimer timer(proxy::kBufferUnlock);
    return g_indexBufferUnlock(self);
}

ULONG WINAPI HookTextureRelease(IUnknown* self) {
    const ULONG remaining = g_textureRelease(self);
    if (remaining == 0) {
        proxy::ForgetDecalTexture(self);
        proxy::ForgetParticleTexture(self);
        proxy::OnTextureReleasedForStats(self, g_presentCount);
    }
    return remaining;
}

UINT WINAPI HookGetAvailableTextureMem(IDirect3DDevice9* self) {
    const UINT available = g_getAvailableTextureMem(self);
    proxy::NoteAvailableTextureMem(available, _ReturnAddress());
    return available;
}

// QueryMode=1: polls the bridge at most once per QueryPollMs per occlusion query and returns its
// last real pixel count in between, preventing tight spin-polls from stalling behind bridge flushes.
struct OcclusionQuery { IDirect3DQuery9* query; DWORD lastPixels; LONGLONG lastPoll; bool hasResult; };
constexpr size_t kOcclusionQueryTableSize = 4096;
OcclusionQuery g_occlusionQueries[kOcclusionQueryTableSize] = {};
SRWLOCK g_occlusionLock = SRWLOCK_INIT;

OcclusionQuery* FindOcclusionQuery(IDirect3DQuery9* query, bool insert) {
    const size_t index = (reinterpret_cast<uintptr_t>(query) >> 4) & (kOcclusionQueryTableSize - 1);
    for (size_t probe = 0; probe < kOcclusionQueryTableSize; ++probe) {
        OcclusionQuery& entry = g_occlusionQueries[(index + probe) & (kOcclusionQueryTableSize - 1)];
        if (entry.query == query) return &entry;
        if (!entry.query) {
            if (!insert) return nullptr;
            entry = {query, 0, 0, false};
            return &entry;
        }
    }
    return nullptr;
}

HRESULT WINAPI HookQueryGetData(IDirect3DQuery9* self, void* data, DWORD size, DWORD flags) {
    CallTimer timer(proxy::kQueryGetData);
    if (!g_queryMode || !data || size != sizeof(DWORD)) return g_queryGetData(self, data, size, flags);
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    AcquireSRWLockExclusive(&g_occlusionLock);
    OcclusionQuery* entry = FindOcclusionQuery(self, false);
    if (!entry) {
        ReleaseSRWLockExclusive(&g_occlusionLock);
        return g_queryGetData(self, data, size, flags & ~D3DGETDATA_FLUSH);
    }
    const bool poll = !entry->hasResult || (now.QuadPart - entry->lastPoll >= g_queryPollTicks);
    DWORD pixels = entry->lastPixels;
    ReleaseSRWLockExclusive(&g_occlusionLock);
    if (poll) {
        DWORD fresh = 0;
        const HRESULT result = g_queryGetData(self, &fresh, sizeof(fresh), flags & ~D3DGETDATA_FLUSH);
        if (FAILED(result)) return result;
        AcquireSRWLockExclusive(&g_occlusionLock);
        if (OcclusionQuery* current = FindOcclusionQuery(self, false)) {
            current->lastPoll = now.QuadPart;
            current->hasResult = true;
            if (result == S_OK) current->lastPixels = fresh;
            pixels = current->lastPixels;
        }
        ReleaseSRWLockExclusive(&g_occlusionLock);
    }
    *static_cast<DWORD*>(data) = pixels;
    return S_OK;
}

// FormatGuard: caches CheckDeviceFormat per (format, usage, resourceType) and refuses unsupported
// or zero-sized resource creates before an async bridge create can leave a null server object behind.
IDirect3D9* g_guardFactory = nullptr;
using FormatCheckKey = std::tuple<DWORD, DWORD, DWORD>;
std::map<FormatCheckKey, HRESULT>* g_formatChecks = nullptr;
SRWLOCK g_formatCheckLock = SRWLOCK_INIT;

void FormatText(DWORD code, char (&text)[12]) {
    if (code > 0xFF) {
        for (int i = 0; i < 4; ++i) {
            const char c = static_cast<char>((code >> (8 * i)) & 0xFF);
            text[i] = c >= 0x20 && c < 0x7F ? c : '?';
        }
        text[4] = 0;
    } else {
        _snprintf_s(text, _TRUNCATE, "%lu", static_cast<unsigned long>(code));
    }
}

void NoteCheckedFormat(D3DFORMAT format, DWORD usage, D3DRESOURCETYPE type, HRESULT result) {
    if (!g_formatGuard) return;
    const FormatCheckKey key(static_cast<DWORD>(format), usage | 0x80000000u, static_cast<DWORD>(type));
    AcquireSRWLockExclusive(&g_formatCheckLock);
    if (!g_formatChecks) g_formatChecks = new std::map<FormatCheckKey, HRESULT>();
    const bool first = g_formatChecks->emplace(key, result).second;
    ReleaseSRWLockExclusive(&g_formatCheckLock);
    if (!first) return;
    char text[12];
    FormatText(static_cast<DWORD>(format), text);
    WriteLog("FormatGuard: game CheckDeviceFormat '%s' (0x%08lX) usage=0x%08lX type=%d -> 0x%08lX",
             text, static_cast<unsigned long>(format), static_cast<unsigned long>(usage),
             static_cast<int>(type), static_cast<unsigned long>(result));
}

HRESULT FormatGuardCheck(D3DFORMAT format, DWORD usage, D3DRESOURCETYPE type, UINT width, UINT height, const char* call) {
    if (!g_formatGuard) return S_OK;
    if (width == 0 || height == 0) {
        WriteLog("FormatGuard: %s with zero size %ux%u refused", call, width, height);
        return D3DERR_INVALIDCALL;
    }
    if (!g_guardFactory) return S_OK;
    const DWORD checkUsage = usage & (D3DUSAGE_RENDERTARGET | D3DUSAGE_DEPTHSTENCIL | D3DUSAGE_AUTOGENMIPMAP);
    const FormatCheckKey key(static_cast<DWORD>(format), checkUsage, static_cast<DWORD>(type));
    AcquireSRWLockShared(&g_formatCheckLock);
    HRESULT result = E_PENDING;
    if (g_formatChecks) {
        const auto found = g_formatChecks->find(key);
        if (found != g_formatChecks->end()) result = found->second;
    }
    ReleaseSRWLockShared(&g_formatCheckLock);
    if (result == E_PENDING) {
        result = g_guardFactory->CheckDeviceFormat(D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL,
                                                   D3DFMT_X8R8G8B8, checkUsage, type, format);
        AcquireSRWLockExclusive(&g_formatCheckLock);
        if (!g_formatChecks) g_formatChecks = new std::map<FormatCheckKey, HRESULT>();
        (*g_formatChecks)[key] = result;
        ReleaseSRWLockExclusive(&g_formatCheckLock);
        char text[12];
        FormatText(static_cast<DWORD>(format), text);
        WriteLog("FormatGuard: %s '%s' (0x%08lX) usage=0x%08lX type=%d -> 0x%08lX%s", call, text,
                 static_cast<unsigned long>(format), static_cast<unsigned long>(checkUsage),
                 static_cast<int>(type), static_cast<unsigned long>(result),
                 FAILED(result) ? " REFUSED (logged once per combination)" : "");
    }
    return FAILED(result) ? D3DERR_NOTAVAILABLE : S_OK;
}

HRESULT WINAPI HookCreateVolumeTexture(IDirect3DDevice9* self, UINT width, UINT height, UINT depth, UINT levels,
                                       DWORD usage, D3DFORMAT format, D3DPOOL pool,
                                       IDirect3DVolumeTexture9** texture, HANDLE* shared) {
    const HRESULT refused = FormatGuardCheck(format, usage, D3DRTYPE_VOLUMETEXTURE, width, height, "CreateVolumeTexture");
    if (refused != S_OK) return refused;
    return g_createVolumeTexture(self, width, height, depth, levels, usage, format, pool, texture, shared);
}

HRESULT WINAPI HookCreateCubeTexture(IDirect3DDevice9* self, UINT edge, UINT levels, DWORD usage, D3DFORMAT format,
                                     D3DPOOL pool, IDirect3DCubeTexture9** texture, HANDLE* shared) {
    const HRESULT refused = FormatGuardCheck(format, usage, D3DRTYPE_CUBETEXTURE, edge, edge, "CreateCubeTexture");
    if (refused != S_OK) return refused;
    return g_createCubeTexture(self, edge, levels, usage, format, pool, texture, shared);
}

HRESULT WINAPI HookCreateRenderTarget(IDirect3DDevice9* self, UINT width, UINT height, D3DFORMAT format,
                                      D3DMULTISAMPLE_TYPE multiSample, DWORD quality, BOOL lockable,
                                      IDirect3DSurface9** surface, HANDLE* shared) {
    const HRESULT refused = FormatGuardCheck(format, D3DUSAGE_RENDERTARGET, D3DRTYPE_SURFACE, width, height, "CreateRenderTarget");
    if (refused != S_OK) return refused;
    return g_createRenderTarget(self, width, height, format, multiSample, quality, lockable, surface, shared);
}

HRESULT WINAPI HookCreateDepthStencilSurface(IDirect3DDevice9* self, UINT width, UINT height, D3DFORMAT format,
                                             D3DMULTISAMPLE_TYPE multiSample, DWORD quality, BOOL discard,
                                             IDirect3DSurface9** surface, HANDLE* shared) {
    const HRESULT refused = FormatGuardCheck(format, D3DUSAGE_DEPTHSTENCIL, D3DRTYPE_SURFACE, width, height, "CreateDepthStencilSurface");
    if (refused != S_OK) return refused;
    return g_createDepthStencilSurface(self, width, height, format, multiSample, quality, discard, surface, shared);
}

HRESULT WINAPI HookCreateOffscreenPlainSurface(IDirect3DDevice9* self, UINT width, UINT height, D3DFORMAT format,
                                               D3DPOOL pool, IDirect3DSurface9** surface, HANDLE* shared) {
    const HRESULT refused = FormatGuardCheck(format, 0, D3DRTYPE_SURFACE, width, height, "CreateOffscreenPlainSurface");
    if (refused != S_OK) return refused;
    return g_createOffscreenPlainSurface(self, width, height, format, pool, surface, shared);
}

HRESULT WINAPI HookCreateTexture(IDirect3DDevice9* self, UINT width, UINT height, UINT levels, DWORD usage,
                                 D3DFORMAT format, D3DPOOL pool, IDirect3DTexture9** texture, HANDLE* shared) {
    const HRESULT refused = FormatGuardCheck(format, usage, D3DRTYPE_TEXTURE, width, height, "CreateTexture");
    if (refused != S_OK) return refused;
    HRESULT result;
    LARGE_INTEGER start, end;
    QueryPerformanceCounter(&start);
    {
        CallTimer timer(proxy::kCreateTexture);
        result = proxy::CreateTextureCounted(pool, texture, [&] {
            return g_createTexture(self, width, height, levels, usage, format, pool, texture, shared);
        });
    }
    QueryPerformanceCounter(&end);
    if (SUCCEEDED(result) && texture && *texture) {
        proxy::RecordTextureUsage(*texture, usage);
        const MethodSlot slots[] = {
            {19, reinterpret_cast<void*>(&HookLockRect), reinterpret_cast<void**>(&g_lockRect)},
            {20, reinterpret_cast<void*>(&HookUnlockRect), reinterpret_cast<void**>(&g_unlockRect)},
            {2, reinterpret_cast<void*>(&HookTextureRelease), reinterpret_cast<void**>(&g_textureRelease)},
        };
        const bool hookRelease = proxy::IsTexStatsEnabled() || proxy::IsDecalTagEnabled();
        HookObjectMethods(*texture, slots, hookRelease ? ARRAYSIZE(slots) : 2);
        if (hookRelease) proxy::NoteTextureReleaseHooked();
        if (proxy::IsTexStatsEnabled()) {
            proxy::RecordTextureCreate(_ReturnAddress(), *texture, width, height, levels, usage, format, pool,
                                       proxy::QpcToMilliseconds(end.QuadPart - start.QuadPart), g_presentCount);
        }
    }
    return result;
}

HRESULT WINAPI HookCreateVertexBuffer(IDirect3DDevice9* self, UINT length, DWORD usage, DWORD fvf, D3DPOOL pool,
                                      IDirect3DVertexBuffer9** buffer, HANDLE* shared) {
    HRESULT result;
    {
        CallTimer timer(proxy::kCreateVertexBuffer);
        result = g_createVertexBuffer(self, length, usage, fvf, pool, buffer, shared);
    }
    if (SUCCEEDED(result) && buffer && *buffer) {
        const MethodSlot slots[] = {
            {11, reinterpret_cast<void*>(&HookVertexBufferLock), reinterpret_cast<void**>(&g_vertexBufferLock)},
            {12, reinterpret_cast<void*>(&HookVertexBufferUnlock), reinterpret_cast<void**>(&g_vertexBufferUnlock)},
        };
        HookObjectMethods(*buffer, slots, ARRAYSIZE(slots));
    }
    return result;
}

HRESULT WINAPI HookCreateIndexBuffer(IDirect3DDevice9* self, UINT length, DWORD usage, D3DFORMAT format, D3DPOOL pool,
                                     IDirect3DIndexBuffer9** buffer, HANDLE* shared) {
    HRESULT result;
    {
        CallTimer timer(proxy::kCreateIndexBuffer);
        result = g_createIndexBuffer(self, length, usage, format, pool, buffer, shared);
    }
    if (SUCCEEDED(result) && buffer && *buffer) {
        const MethodSlot slots[] = {
            {11, reinterpret_cast<void*>(&HookIndexBufferLock), reinterpret_cast<void**>(&g_indexBufferLock)},
            {12, reinterpret_cast<void*>(&HookIndexBufferUnlock), reinterpret_cast<void**>(&g_indexBufferUnlock)},
        };
        HookObjectMethods(*buffer, slots, ARRAYSIZE(slots));
    }
    return result;
}

HRESULT WINAPI HookCreateQuery(IDirect3DDevice9* self, D3DQUERYTYPE type, IDirect3DQuery9** query) {
    const HRESULT result = g_createQuery(self, type, query);
    if (SUCCEEDED(result) && query && *query) {
        if (g_queryMode) {
            AcquireSRWLockExclusive(&g_occlusionLock);
            if (type == D3DQUERYTYPE_OCCLUSION) {
                if (OcclusionQuery* entry = FindOcclusionQuery(*query, true)) {
                    entry->lastPixels = 0;
                    entry->lastPoll = 0;
                    entry->hasResult = false;
                }
            } else if (OcclusionQuery* stale = FindOcclusionQuery(*query, false)) {
                stale->query = reinterpret_cast<IDirect3DQuery9*>(1);
            }
            ReleaseSRWLockExclusive(&g_occlusionLock);
        }
        const MethodSlot slots[] = {
            {7, reinterpret_cast<void*>(&HookQueryGetData), reinterpret_cast<void**>(&g_queryGetData)},
        };
        HookObjectMethods(*query, slots, ARRAYSIZE(slots));
    }
    return result;
}

HRESULT WINAPI HookUpdateSurface(IDirect3DDevice9* self, IDirect3DSurface9* source, const RECT* sourceRect,
                                 IDirect3DSurface9* destination, const POINT* destinationPoint) {
    CallTimer timer(proxy::kUpdateSurface);
    return g_updateSurface(self, source, sourceRect, destination, destinationPoint);
}

HRESULT WINAPI HookUpdateTexture(IDirect3DDevice9* self, IDirect3DBaseTexture9* source, IDirect3DBaseTexture9* destination) {
    CallTimer timer(proxy::kUpdateTexture);
    return proxy::UpdateTextureCounted(destination, [&] { return g_updateTexture(self, source, destination); });
}

HRESULT WINAPI HookGetRenderTargetData(IDirect3DDevice9* self, IDirect3DSurface9* target, IDirect3DSurface9* destination) {
    CallTimer timer(proxy::kGetRenderTargetData);
    return g_getRenderTargetData(self, target, destination);
}

HRESULT WINAPI HookStretchRect(IDirect3DDevice9* self, IDirect3DSurface9* source, const RECT* sourceRect,
                               IDirect3DSurface9* destination, const RECT* destinationRect, D3DTEXTUREFILTERTYPE filter) {
    if (lightpub::HidePostFx() && destination && destination == proxy::CurrentBackbuffer(self)) {
        proxy::NotePostFxSkippedCopy();
        return D3D_OK;
    }
    CallTimer timer(proxy::kStretchRect);
    return g_stretchRect(self, source, sourceRect, destination, destinationRect, filter);
}

HRESULT WINAPI HookClear(IDirect3DDevice9* self, DWORD count, const D3DRECT* rects,
                         DWORD flags, D3DCOLOR color, float z, DWORD stencil) {
    CallTimer timer(proxy::kClear);
    return g_clear(self, count, rects, flags, color, z, stencil);
}

bool PatchDevice(IDirect3DDevice9* device) {
    WriteLog("PatchDevice enter device=%p", device);
    if (!device) { WriteLog("PatchDevice rejected null device"); return false; }
    void** vtable = *reinterpret_cast<void***>(device);
    if (!vtable) { WriteLog("PatchDevice rejected device=%p with null vtable", device); return false; }

    AcquireSRWLockExclusive(&g_hookListLock);
    for (DeviceHook* existing = g_deviceHooks; existing; existing = existing->next) {
        if (existing->vtable == vtable) {
            if (vtable[94] != reinterpret_cast<void*>(&HookSetVertexShaderConstantF)) {
                WriteLog("PatchDevice found later external hook vtable=%p slot94=%p; leaving its chain intact",
                         vtable, vtable[94]);
            } else {
                WriteLog("PatchDevice already active device=%p vtable=%p", device, vtable);
            }
            ReleaseSRWLockExclusive(&g_hookListLock);
            return true;
        }
    }

    DeviceHook* hook = static_cast<DeviceHook*>(HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(DeviceHook)));
    if (!hook) {
        WriteLog("PatchDevice allocation failed for device=%p", device);
        ReleaseSRWLockExclusive(&g_hookListLock);
        return false;
    }

    void* previous = nullptr;
    if (vtable[94] == reinterpret_cast<void*>(&HookSetVertexShaderConstantF) ||
        !PatchVtableSlot(vtable, 94, reinterpret_cast<void*>(&HookSetVertexShaderConstantF), &previous) ||
        !previous || previous == reinterpret_cast<void*>(&HookSetVertexShaderConstantF)) {
        WriteLog("PatchDevice refused unsafe slot94 patch device=%p vtable=%p previous=%p",
                 device, vtable, previous);
        HeapFree(GetProcessHeap(), 0, hook);
        ReleaseSRWLockExclusive(&g_hookListLock);
        return false;
    }

    hook->vtable = vtable;
    hook->setTransform = reinterpret_cast<proxy::SetTransformFn>(vtable[44]);
    hook->setVertexShaderConstantF = reinterpret_cast<proxy::SetVertexShaderConstantFFn>(previous);

    const bool frameTrace = proxy::IsFrameTraceEnabled();
    const bool callTiming = proxy::IsCallTimingEnabled();
    const bool needDrawHooks = g_lightsEnabled || g_filterCfg.passFilter || g_filterCfg.skipShadowMapDraws ||
                               g_filterCfg.packedTexcoordFix || g_filterCfg.alphaTestFromTexkill ||
                               proxy::IsDecalTagEnabled() || proxy::IsHideDecalsEnabled();
    if (frameTrace || g_cameraOncePerFrame || g_presentHook || needDrawHooks) {
        proxy::InitDiagnosticsQpc();
        struct DiagnosticSlot { size_t slot; void* replacement; void** saved; };
        const DiagnosticSlot slots[] = {
            {17, reinterpret_cast<void*>(&HookPresent), reinterpret_cast<void**>(&hook->present)},
            {81, reinterpret_cast<void*>(&HookDrawPrimitive), reinterpret_cast<void**>(&hook->drawPrimitive)},
            {82, reinterpret_cast<void*>(&HookDrawIndexedPrimitive), reinterpret_cast<void**>(&hook->drawIndexedPrimitive)},
            {83, reinterpret_cast<void*>(&HookDrawPrimitiveUP), reinterpret_cast<void**>(&hook->drawPrimitiveUP)},
            {84, reinterpret_cast<void*>(&HookDrawIndexedPrimitiveUP), reinterpret_cast<void**>(&hook->drawIndexedPrimitiveUP)},
        };
        const size_t slotCount = (frameTrace || needDrawHooks) ? ARRAYSIZE(slots) : 1;
        for (size_t i = 0; i < slotCount; ++i) {
            void* saved = nullptr;
            if (vtable[slots[i].slot] == slots[i].replacement ||
                !PatchVtableSlot(vtable, slots[i].slot, slots[i].replacement, &saved) || !saved) {
                WriteLog("PatchDevice diagnostic slot %u not hooked", static_cast<unsigned>(slots[i].slot));
                continue;
            }
            *slots[i].saved = saved;
            WriteLog("PatchDevice diagnostic slot %u original=%p", static_cast<unsigned>(slots[i].slot), saved);
        }
        if (callTiming) {
            const MethodSlot timedSlots[] = {
                {23, reinterpret_cast<void*>(&HookCreateTexture), reinterpret_cast<void**>(&g_createTexture)},
                {26, reinterpret_cast<void*>(&HookCreateVertexBuffer), reinterpret_cast<void**>(&g_createVertexBuffer)},
                {27, reinterpret_cast<void*>(&HookCreateIndexBuffer), reinterpret_cast<void**>(&g_createIndexBuffer)},
                {30, reinterpret_cast<void*>(&HookUpdateSurface), reinterpret_cast<void**>(&g_updateSurface)},
                {31, reinterpret_cast<void*>(&HookUpdateTexture), reinterpret_cast<void**>(&g_updateTexture)},
                {32, reinterpret_cast<void*>(&HookGetRenderTargetData), reinterpret_cast<void**>(&g_getRenderTargetData)},
                {34, reinterpret_cast<void*>(&HookStretchRect), reinterpret_cast<void**>(&g_stretchRect)},
                {43, reinterpret_cast<void*>(&HookClear), reinterpret_cast<void**>(&g_clear)},
                {118, reinterpret_cast<void*>(&HookCreateQuery), reinterpret_cast<void**>(&g_createQuery)},
            };
            HookObjectMethods(device, timedSlots, ARRAYSIZE(timedSlots));
        }
    }
    if (g_queryMode && !callTiming) {
        const MethodSlot querySlots[] = {
            {118, reinterpret_cast<void*>(&HookCreateQuery), reinterpret_cast<void**>(&g_createQuery)},
        };
        HookObjectMethods(device, querySlots, ARRAYSIZE(querySlots));
    }
    if (needDrawHooks) {
        const MethodSlot postFxSlots[] = {
            {34, reinterpret_cast<void*>(&HookStretchRect), reinterpret_cast<void**>(&g_stretchRect)},
            {37, reinterpret_cast<void*>(&HookSetRenderTarget), reinterpret_cast<void**>(&g_setRenderTarget)},
            {65, reinterpret_cast<void*>(&HookSetTexture), reinterpret_cast<void**>(&g_setTexture)},
        };
        HookObjectMethods(device, postFxSlots, ARRAYSIZE(postFxSlots));
        const MethodSlot passSlots[] = {
            {57, reinterpret_cast<void*>(&HookSetRenderState), reinterpret_cast<void**>(&g_setRenderState)},
            {16, reinterpret_cast<void*>(&HookReset), reinterpret_cast<void**>(&g_reset)},
            {100, reinterpret_cast<void*>(&HookSetStreamSource), reinterpret_cast<void**>(&g_setStreamSource)},
            {104, reinterpret_cast<void*>(&HookSetIndices), reinterpret_cast<void**>(&g_setIndices)},
            {92, reinterpret_cast<void*>(&HookSetVertexShader), reinterpret_cast<void**>(&g_setVertexShader)},
            {107, reinterpret_cast<void*>(&HookSetPixelShader), reinterpret_cast<void**>(&g_setPixelShader)},
            {109, reinterpret_cast<void*>(&HookSetPixelShaderConstantF), reinterpret_cast<void**>(&g_setPixelShaderConstantF)},
        };
        HookObjectMethods(device, passSlots, ARRAYSIZE(passSlots));
        proxy::FilterOriginals filterOrig = {};
        filterOrig.setTexture = g_setTexture;
        filterOrig.setRenderState = g_setRenderState;
        filterOrig.setStreamSource = g_setStreamSource;
        filterOrig.setIndices = g_setIndices;
        filterOrig.setPixelShaderConstantF = g_setPixelShaderConstantF;
        filterOrig.shaderHooksInstalled = g_setVertexShader && g_setPixelShader;
        filterOrig.hookShaderRelease = &HookShaderRelease;
        proxy::SetFilterOriginals(filterOrig);
    }
    if (g_formatGuard) {
        const MethodSlot guardSlots[] = {
            {23, reinterpret_cast<void*>(&HookCreateTexture), reinterpret_cast<void**>(&g_createTexture)},
            {24, reinterpret_cast<void*>(&HookCreateVolumeTexture), reinterpret_cast<void**>(&g_createVolumeTexture)},
            {25, reinterpret_cast<void*>(&HookCreateCubeTexture), reinterpret_cast<void**>(&g_createCubeTexture)},
            {28, reinterpret_cast<void*>(&HookCreateRenderTarget), reinterpret_cast<void**>(&g_createRenderTarget)},
            {29, reinterpret_cast<void*>(&HookCreateDepthStencilSurface), reinterpret_cast<void**>(&g_createDepthStencilSurface)},
            {36, reinterpret_cast<void*>(&HookCreateOffscreenPlainSurface), reinterpret_cast<void**>(&g_createOffscreenPlainSurface)},
        };
        HookObjectMethods(device, guardSlots, ARRAYSIZE(guardSlots));
    }
    if (proxy::TextureFillCountsEnabled()) {
        const MethodSlot fillSlots[] = {
            {23, reinterpret_cast<void*>(&HookCreateTexture), reinterpret_cast<void**>(&g_createTexture)},
            {31, reinterpret_cast<void*>(&HookUpdateTexture), reinterpret_cast<void**>(&g_updateTexture)},
        };
        HookObjectMethods(device, fillSlots, ARRAYSIZE(fillSlots));
    }
    if (proxy::IsTexStatsEnabled()) {
        const MethodSlot statSlots[] = {
            {4, reinterpret_cast<void*>(&HookGetAvailableTextureMem), reinterpret_cast<void**>(&g_getAvailableTextureMem)},
        };
        HookObjectMethods(device, statSlots, ARRAYSIZE(statSlots));
    }
    hook->next = g_deviceHooks;
    g_deviceHooks = hook;
    WriteLog("PatchDevice success device=%p vtable=%p SetTransform=%p "
             "SetVertexShaderConstantF original=%p hook=%p",
             device, vtable, hook->setTransform, hook->setVertexShaderConstantF, &HookSetVertexShaderConstantF);
    ReleaseSRWLockExclusive(&g_hookListLock);
    return true;
}

class Direct3D9Wrapper final : public IDirect3D9Ex {
public:
    Direct3D9Wrapper(IDirect3D9* real, IDirect3D9Ex* realEx)
        : references_(1), real_(real), realEx_(realEx) {
        if (g_formatGuard) g_guardFactory = real;
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** object) override {
        if (!object) return E_POINTER;
        *object = nullptr;
        if (IsEqualIID(iid, IID_IUnknown) || IsEqualIID(iid, __uuidof(IDirect3D9)) ||
            (realEx_ && IsEqualIID(iid, __uuidof(IDirect3D9Ex)))) {
            *object = static_cast<IDirect3D9Ex*>(this);
            AddRef();
            WriteLog("factory wrapper QueryInterface iid=%08lX returned wrapper=%p", iid.Data1, this);
            return S_OK;
        }
        const HRESULT result = real_->QueryInterface(iid, object);
        WriteLog("factory wrapper QueryInterface iid=%08lX forwarded HRESULT=0x%08lX out=%p",
                 iid.Data1, static_cast<unsigned long>(result), object ? *object : nullptr);
        return result;
    }

    ULONG STDMETHODCALLTYPE AddRef() override { return static_cast<ULONG>(InterlockedIncrement(&references_)); }

    ULONG STDMETHODCALLTYPE Release() override {
        const LONG remaining = InterlockedDecrement(&references_);
        if (remaining == 0) {
            WriteLog("factory wrapper release final wrapper=%p real=%p", this, real_);
            if (g_guardFactory == real_) g_guardFactory = nullptr;
            real_->Release();
            delete this;
            return 0;
        }
        return static_cast<ULONG>(remaining);
    }

    HRESULT STDMETHODCALLTYPE RegisterSoftwareDevice(void* fn) override { return real_->RegisterSoftwareDevice(fn); }
    UINT STDMETHODCALLTYPE GetAdapterCount() override { return real_->GetAdapterCount(); }
    HRESULT STDMETHODCALLTYPE GetAdapterIdentifier(UINT a, DWORD f, D3DADAPTER_IDENTIFIER9* id) override {
        return real_->GetAdapterIdentifier(a, f, id);
    }
    UINT STDMETHODCALLTYPE GetAdapterModeCount(UINT a, D3DFORMAT f) override { return real_->GetAdapterModeCount(a, f); }
    HRESULT STDMETHODCALLTYPE EnumAdapterModes(UINT a, D3DFORMAT f, UINT m, D3DDISPLAYMODE* dm) override {
        return real_->EnumAdapterModes(a, f, m, dm);
    }
    HRESULT STDMETHODCALLTYPE GetAdapterDisplayMode(UINT a, D3DDISPLAYMODE* m) override {
        return real_->GetAdapterDisplayMode(a, m);
    }
    HRESULT STDMETHODCALLTYPE CheckDeviceType(UINT a, D3DDEVTYPE dt, D3DFORMAT af, D3DFORMAT bf, BOOL w) override {
        return real_->CheckDeviceType(a, dt, af, bf, w);
    }
    HRESULT STDMETHODCALLTYPE CheckDeviceFormat(UINT a, D3DDEVTYPE dt, D3DFORMAT af, DWORD u,
                                                D3DRESOURCETYPE rt, D3DFORMAT cf) override {
        const HRESULT result = real_->CheckDeviceFormat(a, dt, af, u, rt, cf);
        NoteCheckedFormat(cf, u, rt, result);
        return result;
    }
    HRESULT STDMETHODCALLTYPE CheckDeviceMultiSampleType(UINT a, D3DDEVTYPE dt, D3DFORMAT sf, BOOL w,
                                                         D3DMULTISAMPLE_TYPE ms, DWORD* q) override {
        return real_->CheckDeviceMultiSampleType(a, dt, sf, w, ms, q);
    }
    HRESULT STDMETHODCALLTYPE CheckDepthStencilMatch(UINT a, D3DDEVTYPE dt, D3DFORMAT af,
                                                     D3DFORMAT rf, D3DFORMAT df) override {
        return real_->CheckDepthStencilMatch(a, dt, af, rf, df);
    }
    HRESULT STDMETHODCALLTYPE CheckDeviceFormatConversion(UINT a, D3DDEVTYPE dt, D3DFORMAT sf, D3DFORMAT tf) override {
        return real_->CheckDeviceFormatConversion(a, dt, sf, tf);
    }
    HRESULT STDMETHODCALLTYPE GetDeviceCaps(UINT a, D3DDEVTYPE dt, D3DCAPS9* c) override {
        return real_->GetDeviceCaps(a, dt, c);
    }
    HMONITOR STDMETHODCALLTYPE GetAdapterMonitor(UINT a) override { return real_->GetAdapterMonitor(a); }
    HRESULT STDMETHODCALLTYPE CreateDevice(UINT adapter, D3DDEVTYPE deviceType, HWND focusWindow, DWORD behaviorFlags,
                                           D3DPRESENT_PARAMETERS* presentationParameters,
                                           IDirect3DDevice9** returnedDevice) override {
        WriteLog("CreateDevice enter wrapper=%p real=%p adapter=%u type=%u flags=0x%08lX out=%p",
                 this, real_, adapter, static_cast<unsigned>(deviceType), behaviorFlags, returnedDevice);
        const HRESULT result = real_->CreateDevice(
            adapter, deviceType, focusWindow, behaviorFlags, presentationParameters, returnedDevice);
        WriteLog("CreateDevice returned HRESULT=0x%08lX device=%p windowed=%d",
                 static_cast<unsigned long>(result), returnedDevice ? *returnedDevice : nullptr,
                 presentationParameters ? presentationParameters->Windowed : -1);
        if (SUCCEEDED(result) && returnedDevice && *returnedDevice) {
            PatchDevice(*returnedDevice);
            InstallWindowLog(focusWindow ? focusWindow : presentationParameters->hDeviceWindow);
        }
        return result;
    }

    UINT STDMETHODCALLTYPE GetAdapterModeCountEx(UINT a, const D3DDISPLAYMODEFILTER* f) override {
        return realEx_ ? realEx_->GetAdapterModeCountEx(a, f) : 0;
    }
    HRESULT STDMETHODCALLTYPE EnumAdapterModesEx(UINT a, const D3DDISPLAYMODEFILTER* f, UINT m,
                                                 D3DDISPLAYMODEEX* dm) override {
        return realEx_ ? realEx_->EnumAdapterModesEx(a, f, m, dm) : D3DERR_INVALIDCALL;
    }
    HRESULT STDMETHODCALLTYPE GetAdapterDisplayModeEx(UINT a, D3DDISPLAYMODEEX* m, D3DDISPLAYROTATION* r) override {
        return realEx_ ? realEx_->GetAdapterDisplayModeEx(a, m, r) : D3DERR_INVALIDCALL;
    }
    HRESULT STDMETHODCALLTYPE CreateDeviceEx(UINT adapter, D3DDEVTYPE deviceType, HWND focusWindow, DWORD behaviorFlags,
                                             D3DPRESENT_PARAMETERS* presentationParameters,
                                             D3DDISPLAYMODEEX* fullscreenDisplayMode,
                                             IDirect3DDevice9Ex** returnedDevice) override {
        WriteLog("CreateDeviceEx enter wrapper=%p realEx=%p adapter=%u type=%u flags=0x%08lX out=%p",
                 this, realEx_, adapter, static_cast<unsigned>(deviceType), behaviorFlags, returnedDevice);
        if (!realEx_) return D3DERR_INVALIDCALL;
        const HRESULT result = realEx_->CreateDeviceEx(
            adapter, deviceType, focusWindow, behaviorFlags, presentationParameters, fullscreenDisplayMode, returnedDevice);
        WriteLog("CreateDeviceEx returned HRESULT=0x%08lX device=%p windowed=%d",
                 static_cast<unsigned long>(result), returnedDevice ? *returnedDevice : nullptr,
                 presentationParameters ? presentationParameters->Windowed : -1);
        if (SUCCEEDED(result) && returnedDevice && *returnedDevice) {
            PatchDevice(*returnedDevice);
            InstallWindowLog(focusWindow ? focusWindow : presentationParameters->hDeviceWindow);
        }
        return result;
    }
    HRESULT STDMETHODCALLTYPE GetAdapterLUID(UINT a, LUID* l) override {
        return realEx_ ? realEx_->GetAdapterLUID(a, l) : D3DERR_INVALIDCALL;
    }

private:
    ~Direct3D9Wrapper() = default;
    LONG references_;
    IDirect3D9* real_;
    IDirect3D9Ex* realEx_;
};

IDirect3D9Ex* PatchFactory(IDirect3D9* factory, IDirect3D9Ex* factoryEx) {
    WriteLog("PatchFactory wrap enter factory=%p factoryEx=%p originalVtable=%p",
             factory, factoryEx, factory ? *reinterpret_cast<void***>(factory) : nullptr);
    if (!factory) { WriteLog("PatchFactory rejected null factory"); return nullptr; }
    Direct3D9Wrapper* wrapper = new (std::nothrow) Direct3D9Wrapper(factory, factoryEx);
    if (!wrapper) {
        WriteLog("PatchFactory wrapper allocation failed; returning raw factory=%p", factory);
        return reinterpret_cast<IDirect3D9Ex*>(factory);
    }
    WriteLog("PatchFactory success via COM wrapper=%p wrapperVtable=%p; original factory vtable left untouched",
             wrapper, *reinterpret_cast<void***>(wrapper));
    return wrapper;
}

void __cdecl LogForwardedExportFirstCall(unsigned exportIndex, FARPROC target) {
    static LONG seen[21] = {};
    static const char* names[21] = {
        "ordinal 16", "ordinal 17", "ordinal 18", "ordinal 19",
        "Direct3DCreate9On12", "Direct3DCreate9On12Ex", "ordinal 22",
        "ordinal 23", "Direct3DShaderValidatorCreate9", "PSGPError",
        "PSGPSampleTexture", "D3DPERF_BeginEvent", "D3DPERF_EndEvent",
        "D3DPERF_GetStatus", "D3DPERF_QueryRepeatFrame", "D3DPERF_SetMarker",
        "D3DPERF_SetOptions", "D3DPERF_SetRegion", "DebugSetLevel",
        "DebugSetMute", "Direct3D9EnableMaximizedWindowedModeShim"
    };
    if (exportIndex < ARRAYSIZE(seen) && InterlockedCompareExchange(&seen[exportIndex], 1, 0) == 0) {
        WriteLog("first forwarded export call: %s target=%p", names[exportIndex], target);
    }
}

}  // namespace

extern "C" IDirect3D9* WINAPI Direct3DCreate9(UINT sdkVersion) {
    if (!EnsureInitialized()) return nullptr;
    WriteLog("Direct3DCreate9 enter sdk=%u target=%p", sdkVersion, g_Direct3DCreate9);
    if (!g_Direct3DCreate9) { WriteLog("Direct3DCreate9 blocked: null chain target"); return nullptr; }
    IDirect3D9* factory = g_Direct3DCreate9(sdkVersion);
    WriteLog("Direct3DCreate9 returned factory=%p vtable=%p",
             factory, factory ? *reinterpret_cast<void***>(factory) : nullptr);
    return PatchFactory(factory, nullptr);
}

extern "C" HRESULT WINAPI Direct3DCreate9Ex(UINT sdkVersion, IDirect3D9Ex** returnedFactory) {
    if (!EnsureInitialized()) return E_FAIL;
    WriteLog("Direct3DCreate9Ex enter sdk=%u out=%p target=%p", sdkVersion, returnedFactory, g_Direct3DCreate9Ex);
    if (!g_Direct3DCreate9Ex) { WriteLog("Direct3DCreate9Ex blocked: null chain target"); return E_NOINTERFACE; }
    const HRESULT result = g_Direct3DCreate9Ex(sdkVersion, returnedFactory);
    WriteLog("Direct3DCreate9Ex returned HRESULT=0x%08lX factory=%p vtable=%p",
             static_cast<unsigned long>(result),
             returnedFactory ? *returnedFactory : nullptr,
             returnedFactory && *returnedFactory ? *reinterpret_cast<void***>(*returnedFactory) : nullptr);
    if (SUCCEEDED(result) && returnedFactory && *returnedFactory) {
        *returnedFactory = PatchFactory(*returnedFactory, *returnedFactory);
    }
    return result;
}

// Naked x86 tail jumps preserve each system export's exact ABI while Direct3DCreate9/Ex wrap COM factories.
#define FORWARD_STUB(symbol, target, index)                    \
    extern "C" __declspec(naked) void symbol() {               \
        __asm pushfd                                           \
        __asm pushad                                           \
        __asm push index                                       \
        __asm call EnsureInitializedForForwarder               \
        __asm add esp, 4                                       \
        __asm mov eax, dword ptr [target]                      \
        __asm push eax                                         \
        __asm push index                                       \
        __asm call LogForwardedExportFirstCall                 \
        __asm add esp, 8                                       \
        __asm popad                                            \
        __asm popfd                                            \
        __asm jmp dword ptr [target]                           \
    }
#define FORWARD_EXPORT(name, index) FORWARD_STUB(Proxy_##name, g_##name, index)
#define FORWARD_ORDINAL(number, index) FORWARD_STUB(ProxyOrdinal##number, g_Ordinal##number, index)

FORWARD_ORDINAL(16, 0)
FORWARD_ORDINAL(17, 1)
FORWARD_ORDINAL(18, 2)
FORWARD_ORDINAL(19, 3)
FORWARD_EXPORT(Direct3DCreate9On12, 4)
FORWARD_EXPORT(Direct3DCreate9On12Ex, 5)
FORWARD_ORDINAL(22, 6)
FORWARD_ORDINAL(23, 7)
FORWARD_EXPORT(Direct3DShaderValidatorCreate9, 8)
FORWARD_EXPORT(PSGPError, 9)
FORWARD_EXPORT(PSGPSampleTexture, 10)
FORWARD_EXPORT(D3DPERF_BeginEvent, 11)
FORWARD_EXPORT(D3DPERF_EndEvent, 12)
FORWARD_EXPORT(D3DPERF_GetStatus, 13)
FORWARD_EXPORT(D3DPERF_QueryRepeatFrame, 14)
FORWARD_EXPORT(D3DPERF_SetMarker, 15)
FORWARD_EXPORT(D3DPERF_SetOptions, 16)
FORWARD_EXPORT(D3DPERF_SetRegion, 17)
FORWARD_EXPORT(DebugSetLevel, 18)
FORWARD_EXPORT(DebugSetMute, 19)
FORWARD_EXPORT(Direct3D9EnableMaximizedWindowedModeShim, 20)

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_proxyInstance = instance;
        DisableThreadLibraryCalls(instance);
        return TRUE;
    }
    if (reason == DLL_PROCESS_DETACH) {
        proxy::FlushLogs(true);
        proxy::RemoveCrashHandler();
    }
    return TRUE;
}
