#pragma once
// HitchSampler tag for Present: while the real Present runs, the calling thread's samples say "present"
// instead of "noD3D", so a hitch record shows how much of a long frame gap the game spent inside Present.
// HookPresent (d3d9_proxy.cpp) calls this; tests\hitch\hitch_tag_test.cpp calls it with a fake Present.

#include "proxy_diagnostics.h"

namespace proxy {

template <class PresentCall>
HRESULT CallPresentTagged(PresentCall&& present) {
    HitchNoteCall(kPresent);
    const HRESULT result = present();
    HitchNoteCall(-1);
    return result;
}

}  // namespace proxy
