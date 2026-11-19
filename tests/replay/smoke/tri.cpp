#include <windows.h>
#include <d3d9.h>
#include <cmath>
#include <cstdlib>

struct Vertex {
    float x, y, z;
    DWORD color;
};

// c8..c11 = the columns of view * projection for a camera at (x, y, z) looking down +z.
static __forceinline void UploadCamera(IDirect3DDevice9* device, const D3DMATRIX& proj, float x, float y, float z) {
    D3DMATRIX view = {};
    view._11 = view._22 = view._33 = view._44 = 1.0f;
    view._41 = -x; view._42 = -y; view._43 = -z;
    float columns[4][4];
    for (int column = 0; column < 4; ++column)
        for (int row = 0; row < 4; ++row) {
            columns[column][row] = 0.0f;
            for (int k = 0; k < 4; ++k) columns[column][row] += view.m[row][k] * proj.m[k][column];
        }
    device->SetVertexShaderConstantF(8, &columns[0][0], 4);
}

static LRESULT CALLBACK WndProc(HWND w, UINT m, WPARAM a, LPARAM b) {
    if (m == WM_DESTROY) { PostQuitMessage(0); return 0; }
    return DefWindowProcA(w, m, a, b);
}

int main(int argc, char** argv) {
    const int frames = argc > 1 ? atoi(argv[1]) : 120;
    WNDCLASSA wc = {};
    wc.lpfnWndProc = WndProc;
    wc.hInstance = GetModuleHandleA(nullptr);
    wc.lpszClassName = "ReplaySmoke";
    RegisterClassA(&wc);
    RECT r = {0, 0, 1280, 720};
    AdjustWindowRect(&r, WS_OVERLAPPEDWINDOW, FALSE);
    HWND window = CreateWindowA("ReplaySmoke", "replay smoke", WS_OVERLAPPEDWINDOW | WS_VISIBLE, 50, 50,
                                r.right - r.left, r.bottom - r.top, nullptr, nullptr, wc.hInstance, nullptr);
    IDirect3D9* d3d = Direct3DCreate9(D3D_SDK_VERSION);
    if (!d3d) return 2;
    D3DPRESENT_PARAMETERS pp = {};
    pp.Windowed = TRUE;
    pp.SwapEffect = D3DSWAPEFFECT_DISCARD;
    pp.BackBufferFormat = D3DFMT_X8R8G8B8;
    pp.BackBufferWidth = 1280;
    pp.BackBufferHeight = 720;
    pp.EnableAutoDepthStencil = TRUE;
    pp.AutoDepthStencilFormat = D3DFMT_D24S8;
    pp.PresentationInterval = D3DPRESENT_INTERVAL_IMMEDIATE;
    pp.hDeviceWindow = window;
    IDirect3DDevice9* device = nullptr;
    if (FAILED(d3d->CreateDevice(D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, window, D3DCREATE_HARDWARE_VERTEXPROCESSING,
                                 &pp, &device))) return 3;
    const Vertex tri[3] = {{0.0f, 1.0f, 0.0f, 0xFFFF0000}, {1.0f, -1.0f, 0.0f, 0xFF00FF00},
                           {-1.0f, -1.0f, 0.0f, 0xFF0000FF}};
    const Vertex floorQuad[4] = {{-4.0f, -1.0f, -4.0f, 0xFF808080}, {-4.0f, -1.0f, 4.0f, 0xFF808080},
                                 {4.0f, -1.0f, -4.0f, 0xFF808080}, {4.0f, -1.0f, 4.0f, 0xFF808080}};
    for (int frame = 0; frame < frames; ++frame) {
        MSG msg;
        while (PeekMessageA(&msg, nullptr, 0, 0, PM_REMOVE)) DispatchMessageA(&msg);
        const float angle = frame * 0.05f;
        D3DMATRIX world = {};
        world._11 = cosf(angle); world._13 = -sinf(angle);
        world._22 = 1.0f;
        world._31 = sinf(angle); world._33 = cosf(angle);
        world._44 = 1.0f;
        D3DMATRIX identity = {};
        identity._11 = identity._22 = identity._33 = identity._44 = 1.0f;
        D3DMATRIX view = identity;
        view._43 = 4.0f;
        const float n = 0.1f, f = 100.0f, s = 1.0f / tanf(0.5f);
        D3DMATRIX proj = {};
        proj._11 = s * 720.0f / 1280.0f; proj._22 = s;
        proj._33 = f / (f - n); proj._34 = 1.0f;
        proj._43 = -n * f / (f - n);
        device->Clear(0, nullptr, D3DCLEAR_TARGET | D3DCLEAR_ZBUFFER, 0xFF202040, 1.0f, 0);
        device->BeginScene();
        device->SetRenderState(D3DRS_LIGHTING, FALSE);
        device->SetRenderState(D3DRS_CULLMODE, D3DCULL_NONE);
        device->SetTransform(D3DTS_VIEW, &view);
        device->SetTransform(D3DTS_PROJECTION, &proj);
        device->SetFVF(D3DFVF_XYZ | D3DFVF_DIFFUSE);
        device->SetTransform(D3DTS_WORLD, &identity);
        device->DrawPrimitiveUP(D3DPT_TRIANGLESTRIP, 2, floorQuad, sizeof(Vertex));
        // Three c8..c11 uploads from three call sites, as the game uploads its view-projection: a decoy, the camera
        // (it moves with the frame number so a check can tell the frames apart), a decoy.
        UploadCamera(device, proj, 100.0f, 0.0f, 0.0f);
        UploadCamera(device, proj, static_cast<float>(frame), 20.0f, -30.0f);
        UploadCamera(device, proj, 0.0f, 500.0f, 0.0f);
        device->SetTransform(D3DTS_WORLD, &world);
        device->DrawPrimitiveUP(D3DPT_TRIANGLELIST, 1, tri, sizeof(Vertex));
        device->EndScene();
        device->Present(nullptr, nullptr, nullptr, nullptr);
    }
    device->Release();
    d3d->Release();
    DestroyWindow(window);
    return 0;
}
