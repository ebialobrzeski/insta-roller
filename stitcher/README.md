# insta-stitcher

Command line stitcher on the Insta360 MediaSDK: one camera file (`.insv`) and a
time span in, an equirectangular (2:1) H.264/H.265 MP4 of exactly that span out.
`core/stitch.py` calls it for the spans a reel uses.

**Status (2026-11-23): built and in use.** Visual Studio 2022 Build Tools 17.14
and CMake, SDK 3.2.0, RTX 3070 Ti. Flight footage (5.7K, no stabilisation) and
holiday footage (8K, FlowState and direction lock) were stitched and rendered
into reels; measurements are in `docs/stitch-benchmark.md`.

Two things found on the first build:

- The SDK puts an older `msvcp140.dll` next to the executable. Code built with a
  current MSVC crashes on its first `std::mutex` lock against it, so the build
  defines `_DISABLE_CONSTEXPR_MUTEX_CONSTRUCTOR`.
- A partial span costs about 15 s per second of 5.7K footage and 17-26 s per
  second of 8K, independent of where the span starts. The time goes into the
  SDK's image sequence export.

## Prerequisites

| | Windows | Linux |
| --- | --- | --- |
| SDK | `MediaSDK-3.2.0-...-win64.zip` (inside `Windows_CameraSDK-2.2.4_MediaSDK-3.2.0.zip`), **fully** extracted: `include/`, `lib/`, `bin/` (2.4 GB) | `MediaSDK-3.2.0-...-linux64.tar.gz`: install the `.deb` (`sudo dpkg -i`, goes to `/opt/MediaSDK-3.2.0-linux`), keep `include/` from the tarball |
| Compiler | Visual Studio 2022 Build Tools, workload "Desktop development with C++" (MSVC x64, C++17) | Ubuntu 22.04 + GCC 11. The SDK readme warns: not GCC 13 on 22.04 (libstdc++ too old, the process fails before `main`) |
| CMake | 3.16 or newer (3.26+ avoids re-copying the runtime on every build) | same |
| GPU | NVIDIA GPU with a current driver | same |
| FFmpeg | needed for partial spans (see [Trimming](#trimming)); the project's `imageio-ffmpeg` binary works | same |

CUDA: nothing to install. The Windows package ships its own CUDA runtime,
TensorRT (`nvinfer_10.dll`), NPP and OpenCV-CUDA DLLs in `bin/`; the Linux `.deb`
brings its libraries with a built-in RPATH. The SDK package does not state a
minimum driver version. The bundled `cudart64_12.dll` means a driver that
supports CUDA 12 (NVIDIA's figure for CUDA 12.0: 527.41 on Windows, 525.60.13 on
Linux) — inferred from the bundled files, not from SDK documentation. Intended
machine: RTX 3070 Ti (8 GB), driver 621.14 — not run there yet.

The SDK and its archives are licensed material: `sdk/` and `*.zip` are in
`.gitignore`. In this repository only the headers, the example and the import
library were extracted to `sdk/MediaSDK-win64/`; extract `bin/` as well before
building.

## Build

Windows, from the repository root, in a "x64 Native Tools" prompt or any shell
where `cmake` is on `PATH`:

```powershell
cmake -S stitcher -B stitcher\build -A x64 -DINSTA_MEDIA_SDK_DIR=C:\Work\insta-roller\sdk\MediaSDK-win64
cmake --build stitcher\build --config Release
stitcher\build\Release\insta-stitcher.exe --version
```

Linux:

```bash
cmake -S stitcher -B stitcher/build \
      -DINSTA_MEDIA_SDK_DIR=/opt/MediaSDK-3.2.0-linux \
      -DINSTA_MEDIA_SDK_INCLUDE_DIR=$HOME/MediaSDK-3.2.0-20261026-linux64/include
cmake --build stitcher/build -j
stitcher/build/insta-stitcher --version
```

CMake options:

| Variable | Meaning |
| --- | --- |
| `INSTA_MEDIA_SDK_DIR` | MediaSDK folder (Windows: extracted zip; Linux: `.deb` prefix). Required. |
| `INSTA_MEDIA_SDK_INCLUDE_DIR` | Folder with `ins_stitcher.h` when it is not under the SDK folder (Linux). |
| `INSTA_STITCHER_COPY_RUNTIME` | `ON` (default): after the build copy the SDK runtime next to the executable — on Windows all of `bin/` (DLLs and `models/`, 2.4 GB), on Linux `models/`. `OFF`: put the SDK `bin/` on `PATH` yourself and pass `--models-dir`. |

Build `Release`. All configurations are forced to the release CRT (`/MD`) because
`std::string`/`std::vector`/`std::function` cross the DLL boundary and
`MediaSDK.dll` is a release build.

The Python side finds the executable through `INSTA_ROLLER_STITCHER` or as the
first `insta-stitcher(.exe)` under `stitcher/build/**`.

## Usage

```
insta-stitcher --input <file.insv> --output <out.mp4>
               [--start-ms N] [--duration-ms M]
               [--stabilize none|flowstate|direction-lock]
               [--width W --height H] [--bitrate BPS] [--gpu INDEX] [--progress]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--input` | required | Camera file. X4/X5: one `.insv` holds both lens streams. |
| `--output` | required | MP4 to write. Parent folders are created. |
| `--start-ms`, `--duration-ms` | 0, to the end | Span inside the file. Rounded to whole frames. |
| `--stabilize` | `direction-lock` | See [Stabilisation](#stabilisation). |
| `--width`, `--height` | native | Must be 2:1 and even; one of them is enough. Native = 2 x lens width: 5760x2880 for 5.7K, 7680x3840 for 8K. |
| `--bitrate` | encoder default | Bits per second, `80000000` or `80M`. |
| `--codec` | `auto` | `h264` up to 4096 px wide, `h265` above (H.264 hardware encoders stop at 4096). |
| `--gpu` | driver default | Sets `CUDA_VISIBLE_DEVICES` for the process. |
| `--progress` | off | `progress N%` lines on stdout, N rising to 100. |
| `--stitch-type` | `optflow` | `template` (fastest), `optflow`, `dynamic`, `ai` (needs models). |
| `--stitch-fusion` | off | Colour matching across the seam (Studio's "Image Fusion"). |
| `--accessory` | 0 | `ins::CameraAccessoryType` number (lens guard, dive case, ND); `-1` = auto detect. |
| `--trim-mode`, `--intermediate`, `--ffmpeg`, `--temp-dir`, `--keep-temp`, `--no-audio` | | See [Trimming](#trimming). |
| `--no-cuda`, `--soft-encode`, `--soft-decode` | off | Turn hardware paths off. |
| `--models-dir` | `<exe dir>/models` | SDK model files. |
| `--stall-timeout` | 600 | Give up after this many seconds without progress; 0 = never. |
| `--log-file`, `--verbose` | | SDK log to a file / SDK log level INFO plus feature status. |

Exit code 0 = the output exists and is complete; 1 = failure (message on stderr,
no output file left behind); 2 = bad arguments. Information lines also go to
stderr, so stdout carries only the progress lines (and anything the SDK prints
itself).

Example:

```powershell
stitcher\build\Release\insta-stitcher.exe --input D:\flight\VID_20260624_125012_00_007.insv `
    --output work\stitch-test\thermal.mp4 --start-ms 497000 --duration-ms 20000 `
    --stabilize direction-lock --progress
```

## Stabilisation

`ins::VideoStitcher` (`ins_stitcher.h`) has exactly two switches,
`EnableFlowState(bool)` and `EnableDirectionLock(bool)`; the header says the
direction lock depends on FlowState.

| `--stabilize` | `EnableFlowState` | `EnableDirectionLock` | Picture |
| --- | --- | --- | --- |
| `none` | false | false | No stabilisation. The picture is tied to the camera, so to the aircraft: the horizon banks in turns. |
| `flowstate` | true | false | Shake removed, horizon levelled, the view still follows the camera heading. |
| `direction-lock` | true | true | Stabilised with the heading locked: level horizon, the view keeps one compass direction while the aircraft turns under it. |

The exact look of `flowstate` against `direction-lock` on glider footage has to
be judged on a real stitch; the table states what the SDK documents. If the file
has no gyro data the SDK skips stabilisation silently; the tool reads
`GetFeatureStatusMap()` after the run and prints a warning in that case.

The framing stage picks the mode per shot simply by asking for a segment with
the mode it wants; `core/stitch.py` already keys its cache on the mode.

## Trimming

**The SDK has no time range call.** `VideoStitcher` offers no `SetTrimRange`, no
seek and no start/end; the exported symbols of `MediaSDK.lib` confirm that the
header is complete. A video export always covers the whole file. The only way to
make the SDK render part of a file is its image sequence export:
`SetImageSequenceInfo(dir, type)` with `SetExportFrameSequence(frame indices)`.
Cutting the `.insv` beforehand is not an option, the gyro and calibration data
live in a trailer that a cut does not keep.

So the tool has three paths, chosen by `--trim-mode`:

| Mode | When | How | Encoder |
| --- | --- | --- | --- |
| direct | `auto`, span = whole file | SDK video export straight to the output (`SetOutputPath`) | the SDK's own, NVENC by default (`SetSoftwareCodecUsage(false, false)`), audio by the SDK |
| frames | `auto`, partial span; or `--trim-mode frames` | SDK exports the frames of the span as JPEG (or PNG) into `<output>.frames/`, FFmpeg encodes them and adds the audio of the span from the camera file | FFmpeg `hevc_nvenc`/`h264_nvenc`, falling back to `libx265`/`libx264` if NVENC fails |
| full | `--trim-mode full` | SDK stitches the whole file to `<output>.full.tmp.mp4`, FFmpeg cuts the span out frame-exactly (re-encode) | SDK NVENC, then FFmpeg NVENC |

Frames mode costs temporary disk space (roughly 6 to 7 MB per 5.7K JPEG frame,
about 4 GB for 20 s; several times that with `--intermediate png`) and one JPEG
generation of quality. Full mode costs time proportional to the whole file, so
it only pays off when most of a file is used. Temporary files are removed at the
end unless `--keep-temp` is given; `--temp-dir` moves them to another disk.

FFmpeg is looked up as `--ffmpeg`, then `INSTA_ROLLER_FFMPEG`, then `ffmpeg` on
`PATH`. Output of the FFmpeg path: limited range BT.709 `yuv420p`, one keyframe
per second, AAC audio, `+faststart`.

## Known limits

- Not compiled or run yet (see Status). Nothing below the SDK's public headers is known for certain.
- One file per call. A span that crosses a file boundary of a split take is handled by `core/stitch.py`, which stitches each part and joins them.
- One GPU per process (SDK rule). `--gpu` works through `CUDA_VISIBLE_DEVICES`; whether the SDK's NVENC session follows it on a multi-GPU machine is untested.
- The SDK does not document how it names the images of a sequence. The tool therefore takes every image in its private temp folder and orders by the number in the file name.
- Frame indices are computed from the frame rate the SDK reports (`MediaFileInfo::fps`) as `round(ms * fps / 1000)`. Variable frame rate files are not handled.
- Finish is detected as the vendor example does it: the progress callback reports 100. A state callback with error code 0 is treated as information, any other code as failure.
- `--bitrate` in direct mode goes to `SetOutputBitRate`; without it the SDK chooses (the example says "same as input video").
- 8K output (7680x3840) on an 8 GB card is untested; if the SDK runs out of VRAM use `--width 5760`.
- Denoise, ColorPlus, deflicker, defringe and the colour sliders of the SDK are not exposed.

## First build checklist

1. `insta-stitcher --version` prints the SDK version (proves the DLLs load).
2. Whole file, direct mode: `--input short.insv --output a.mp4 --progress`.
3. Partial span, frames mode, with `--keep-temp --verbose`: look into `a.mp4.frames/` — were exactly the requested frames written, and how are they named? Check the warning lines.
4. Same span with `--stabilize none`, `flowstate`, `direction-lock`; compare the three pictures in a turn.
5. `tools/bench_stitch.py <file.insv>` to fill `docs/stitch-benchmark.md`.
