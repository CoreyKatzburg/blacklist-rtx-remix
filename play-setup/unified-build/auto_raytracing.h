#pragma once
// AutoRaytracing ([Lights] AutoRaytracing=1): decides each frame whether Remix ray traces, and whether Remix keeps
// its scene while it does not. Pure logic, so tests/verify_proxy_tests.cpp replays goggle and level transitions.
//
// Why the scene matters: with ray tracing off, Remix clears its whole scene (every BLAS, instance and texture
// binding, after a GPU wait) once more than rtx.sceneKeepAliveFrames frames go by (rtx_context.cpp, default 0, so
// at the first frame). Goggles switch ray tracing off only to show the game's own picture, so a clear there made
// every goggle toggle rebuild the level when ray tracing came back. Now Remix keeps the scene while a level is
// loaded, through the pause and after it, and clears it only when the level itself goes away (loading, menus
// without a level).

#include <cstdint>

namespace proxy {

constexpr uint32_t kLevelOnFrames = 60;          // frames of game lights before a level counts as loaded
constexpr uint32_t kLevelOffFrames = 30;         // frames without lights before it counts as gone
constexpr uint32_t kKeepSceneFrames = 1000000;   // rtx.sceneKeepAliveFrames while a level is loaded (4.6 h at 60 FPS)

struct AutoRtAction {
    bool send = false;        // send both options below this frame (keep-alive first, then enableRaytracing)
    bool raytracing = false;  // rtx.enableRaytracing
    bool keepScene = false;   // rtx.sceneKeepAliveFrames = kKeepSceneFrames (true) or 0 (false)
};

class AutoRaytracing {
public:
    // inLevel: the game's light database has qualifying lights this frame.
    // showGamePicture: goggles (or a menu with PassFilter off) need the game's own picture this frame.
    AutoRtAction Update(bool inLevel, bool showGamePicture) {
        if (inLevel) {
            noLevelFrames_ = 0;
            if (levelFrames_ < kLevelOnFrames) ++levelFrames_;
        } else {
            levelFrames_ = 0;
            if (noLevelFrames_ < kLevelOffFrames) ++noLevelFrames_;
        }
        if (levelFrames_ >= kLevelOnFrames) levelLoaded_ = true;
        if (noLevelFrames_ >= kLevelOffFrames) levelLoaded_ = false;

        AutoRtAction action;
        action.raytracing = levelLoaded_ && !showGamePicture;
        // Keep the scene for as long as the level is loaded, not only during the pause. Dropping the keep-alive to 0
        // when goggles come off landed a Remix frame before ray tracing did (or on a frame without a valid camera),
        // so Remix cleared and rebuilt every mesh and showed one dark frame on each goggles-off.
        action.keepScene = levelLoaded_;
        const int want = action.raytracing ? 1 : 0;
        action.send = want != state_ || action.keepScene != keepScene_;
        // Before any level has loaded there is nothing to switch off (rtx.conf starts with ray tracing off).
        if (state_ < 0 && !action.raytracing && !levelLoaded_) action.send = false;
        if (action.send) {
            state_ = want;
            keepScene_ = action.keepScene;
        }
        return action;
    }

    // After an ini reload (which may have set rtx.enableRaytracing itself): re-send the current decision.
    void Reassert() { state_ = -1; }
    // -1 nothing sent yet, 0 off, 1 on.
    int State() const { return state_; }

private:
    uint32_t levelFrames_ = 0;
    uint32_t noLevelFrames_ = 0;
    bool levelLoaded_ = false;
    bool keepScene_ = false;
    int state_ = -1;
};

}  // namespace proxy
