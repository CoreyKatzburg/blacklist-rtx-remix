#pragma once
// Shared D3D9 function pointer types and per-device hook state used across proxy modules.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d9.h>
#include <cstddef>
#include <cstdint>

namespace proxy {

using SetTransformFn = HRESULT(WINAPI*)(IDirect3DDevice9*, D3DTRANSFORMSTATETYPE, const D3DMATRIX*);
using SetVertexShaderConstantFFn = HRESULT(WINAPI*)(IDirect3DDevice9*, UINT, const float*, UINT);
using PresentFn = HRESULT(WINAPI*)(IDirect3DDevice9*, const RECT*, const RECT*, HWND, const RGNDATA*);
using DrawPrimitiveFn = HRESULT(WINAPI*)(IDirect3DDevice9*, D3DPRIMITIVETYPE, UINT, UINT);
using DrawIndexedPrimitiveFn = HRESULT(WINAPI*)(IDirect3DDevice9*, D3DPRIMITIVETYPE, INT, UINT, UINT, UINT, UINT);
using DrawPrimitiveUPFn = HRESULT(WINAPI*)(IDirect3DDevice9*, D3DPRIMITIVETYPE, UINT, const void*, UINT);
using DrawIndexedPrimitiveUPFn = HRESULT(WINAPI*)(IDirect3DDevice9*, D3DPRIMITIVETYPE, UINT, UINT, UINT,
                                                  const void*, D3DFORMAT, const void*, UINT);

// One camera publication recorded inside a traced frame.
struct TracedPublish {
    char source;          // 'G' gameplay site, 'P' pure site
    bool skipped;         // filtered by CameraOncePerFrame
    UINT drawsBefore;     // draw calls issued earlier in this frame
    float position[3];    // camera position recovered from the view matrix
    float forward[3];     // view-space +Z axis expressed in world space
    float vpDifference;   // max |VP - first VP of this frame|
};

struct FrameTrace {
    UINT publishCount;
    TracedPublish publishes[8];
    D3DMATRIX firstViewProjection;
    LONG draws;
    LONGLONG lastPresentEntry;
};

struct DeviceHook {
    DeviceHook* next;
    void** vtable;
    SetTransformFn setTransform;
    SetVertexShaderConstantFFn setVertexShaderConstantF;
    SRWLOCK constantsLock;
    float constants[12][4];
    uint32_t validRegisters;
    uint32_t pendingRegisters;
    UINT candidateCallCount;
    UINT capturedUploadCount;
    UINT candidateUploadStart[8];
    UINT candidateUploadVectorCount[8];
    UINT lastWorldUploadStart;
    UINT lastWorldUploadCount;
    UINT lastWvpUploadStart;
    UINT lastWvpUploadCount;
    uintptr_t callerRva;
    bool pureCameraUpload;
    bool dumpedRejectedFrame;
    DWORD lastAcceptedLogTick;
    DWORD lastRejectedLogTick;
    uint32_t suppressedAcceptedLogs;
    uint32_t suppressedRejectedLogs;
    PresentFn present;
    DrawPrimitiveFn drawPrimitive;
    DrawIndexedPrimitiveFn drawIndexedPrimitive;
    DrawPrimitiveUPFn drawPrimitiveUP;
    DrawIndexedPrimitiveUPFn drawIndexedPrimitiveUP;
    bool publishedThisFrame;
    UINT tracedFrames;
    FrameTrace frame;
    char* traceBuffer;
    size_t traceLength;
    LONGLONG lastTraceFlush;
    char* callBuffer;
    size_t callLength;
    char* gateBuffer;
    size_t gateLength;
};

struct UploadInfo {
    uint32_t validRegisters;
    uint32_t pendingRegisters;
    UINT candidateCallCount;
    UINT capturedUploadCount;
    UINT candidateUploadStart[8];
    UINT candidateUploadVectorCount[8];
    UINT lastWorldUploadStart;
    UINT lastWorldUploadCount;
    UINT lastWvpUploadStart;
    UINT lastWvpUploadCount;
    uintptr_t callerRva;
    bool pureCameraUpload;
    bool dumpRawConstants;
};

}  // namespace proxy
