# Our changes to NVIDIA's dxvk-remix

These 17 patch files are every change we made to NVIDIA's RTX Remix runtime, made with `git format-patch` from our local checkout. We do not ship NVIDIA's source tree here.

- Upstream: https://github.com/NVIDIAGameWorks/dxvk-remix
- Base: tag `remix-1.5.2` (commit `b81a7b566b1eeb9edb4dc2b3c9d3972e0f253ad4`)
- License: dxvk-remix is NVIDIA's code under the MIT license (`LICENSE-MIT.dxvk-remix`, copied unchanged from the upstream tree). It builds on DXVK, which is under the zlib/libpng license (`LICENSE-zlib.dxvk`, also copied unchanged). Our patches are offered under the same terms as the files they change.

## Apply

```
git clone https://github.com/NVIDIAGameWorks/dxvk-remix.git
cd dxvk-remix
git checkout remix-1.5.2
git submodule update --init --recursive
git am /path/to/this/folder/*.patch
```

Then build as NVIDIA's README describes. `build_custom_remix.ps1` at the top of this repo is the script we use on our build PC; it expects the tree at `src\dxvk-remix` and the stock `remix-1.5.2-release.zip` beside it (neither is in this repo).

## What the patches do

- 0001 is the baseline: the changes we had made before we started tracking them one by one (telemetry, scene manager and capture work, a skinned-triangle gate shader, option plumbing).
- 0002 to 0010: per-frame telemetry (free video memory, zero normals, shader compiles, exposure), Ray Reconstruction logging, waiting for the GPU before the swap chain is recreated (part of the quit-crash fix), and the vertex capture layout for instanced draws as a testable header.
- 0011 to 0014: an option to capture every instance of an instanced draw, then reverted. Both the change and the revert are kept so the history is true.
- 0015 to 0017: a one-frame per-draw scene log for measurement, and binding materials for meshes whose first instance is stale.
