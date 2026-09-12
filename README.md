# drokk-ffmpeg

A minimal, reproducible, statically-linked **ffmpeg** build for the DrokkMods
game mods, for Windows x86_64 and Linux x86_64.

It is a *stock* build of pinned upstream sources. No patches, no forks — only
`./configure` flags. This repository is the complete recipe: [`PINNED`](PINNED)
names the exact upstream commits, [`configure-flags.sh`](configure-flags.sh) is
the exact flag list, and the two build scripts are exactly how the shipped
binaries were produced.

## Why this exists

The mods used to bundle a general-purpose ffmpeg nightly: **145,852,928 bytes**,
unsigned, of a ~155 MB release zip. It carried libx265, AV1, VP8/9, ~500
decoders, ~400 muxers, fontconfig, harfbuzz, Vulkan, OpenCL, VMAF and unstripped
debug data — for a mod that needs one video encoder and one audio encoder.

This build is **~5 MB**, built from a pinned release tag rather than a nightly,
and Authenticode-signed by us, honestly, as the people who actually built it.

| | bundled nightly | drokk-ffmpeg |
|---|---|---|
| size | 145,852,928 B | ~5,000,000 B (both targets) |
| source | master nightly `N-126342-g...` | release tag `n8.1.2`, pinned commit |
| signature | none | Authenticode, timestamped (Windows only) |
| licence | GPLv3 (`--enable-version3`) | GPLv2-or-later |

## What is in it, and why each piece is there

Nothing is included on the grounds that it "might be useful". Every component
below is traceable to a specific command line in a specific mod, and those
command lines are replayed against synthetic input by [`verify.py`](verify.py)
before any binary is allowed into `dist/`.

The seven call sites:

| # | Mod | What it does |
|---|---|---|
| V1 | Silent Hill 2 | `rawvideo bgra` on stdin → `libx264` → Annex-B `h264` on stdout |
| V2a | Lethal Company | same, `rgba` in, when the nvenc probe says no |
| V2b | Lethal Company | same, but `h264_nvenc`, when the probe says yes |
| V3 | Lethal Company | the nvenc probe itself: `-f lavfi -i color=... -f h264 -f null -` |
| V4 | 7 Days to Die | `rawvideo rgba` → optional `-vf vflip` → `h264_nvenc` → Annex-B |
| V5 | 7 Days to Die | recording remux: `.h264` → `.mp4`, `-c copy` |
| A1/A2 | 7 Days, Lethal Company | `s16le` over `tcp://` → `libopus` → `rtp://` (byte-identical arg lists) |

So:

- **libx264** (GPLv2+) — V1, V2a. Brings `--enable-gpl`.
- **h264_nvenc** — V2b, V3, V4. **Mandatory, not an optimisation:** 7 Days
  hardcodes `-c:v h264_nvenc` with no libx264 fallback, so an ffmpeg without it
  means no puppet at all on that mod.
- **libopus** (BSD-3-Clause) — A1/A2.
- **network + `tcp`/`udp`/`rtp` protocols** — A1/A2. An ffmpeg built
  `--disable-network` breaks audio in *two* mods, and breaks it **silently**:
  both treat a dead audio ffmpeg as a warning and continue video-only.
- **`h264_metadata` bitstream filter** — every video path passes
  `-bsf:v h264_metadata=aud=insert`, and the access-unit splitters on the
  receiving side cut on the AUDs it inserts. Without it the pipeline fails at
  runtime, not at build time.
- **avdevice, `lavfi` indev, `color` filter, `wrapped_avframe` decoder** — V3.
- **`h264` demuxer + parser, `mp4` muxer, `file` protocol** — V5.
- **`null` muxer** — V3's tail.
- **swscale (`scale`/`format`)** — the `bgra`/`rgba` → `yuv420p` hop. Silent
  Hill 2 feeds `bgra`; Lethal Company and 7 Days feed `rgba`.
- **swresample (`aformat`/`aresample`)** — the audio graph, and insurance for
  the case where the game's device rate is not 48 kHz.
- **`fd` and `pipe` protocols** — every `-i -` and every `... -`.

Everything else is off. `--disable-everything --disable-autodetect` is the
starting point; nothing is linked merely because it happened to be installed on
the build machine.

## Licence

**GPLv2-or-later.** libx264 is GPLv2+, and `--enable-gpl` alone (deliberately
*not* `--enable-version3`) puts the result there: neither libx264 nor libopus
appears in ffmpeg `configure`'s `EXTERNAL_LIBRARY_VERSION3_LIST`. There is no
`--enable-nonfree` either — nvenc is loaded at runtime, not linked.

Texts are in [`licenses/`](licenses): `COPYING.GPLv2` (the licence of the
distributed binary), `COPYING.LGPLv2.1`, `COPYING.x264`, `COPYING.opus`,
`COPYING.nv-codec-headers`.

**Corresponding source.** Anyone who receives a drokk-ffmpeg binary is entitled
to the source it was built from. That obligation is discharged by this
repository: `PINNED` identifies the exact upstream commits of ffmpeg, x264 and
Opus, and `configure-flags.sh` plus `build-linux.sh` / `build-win64.sh` are the
complete build instructions. Upstream sources are unmodified, so pointing at
their commits *is* pointing at the source.

The mods that ship this binary spawn it as a separate process over pipes. That
is aggregation, not linking, and does not extend the GPL to them.

## Building

Requires the pins to be reachable and a compiler; nothing is installed
system-wide and the artifacts land only in `dist/`.

```sh
./build-linux.sh              # native Linux x86_64 ELF -> dist/linux-x86_64/ffmpeg
./build-win64.sh --setup      # ONE TIME: install the MSYS2 toolchain on the Windows box
./build-win64.sh              # Windows x86_64 PE     -> dist/win64/ffmpeg.exe
./build-win64.sh --sign       # ...and Authenticode-sign it
```

Linux prerequisites (Fedora/Nobara names):

```sh
sudo dnf install git make gcc pkgconf-pkg-config nasm autoconf automake libtool \
                 pulseaudio-libs-devel alsa-lib-devel
```

`nasm` is required and `yasm` is not a substitute — x264's assembly needs
nasm ≥ 2.13.

**The Linux binary needs `libpulse.so.0` and `libasound.so.2` at runtime.**
They back the pulse/alsa audio devices (drokkenemies `AUDIO_PLAN.md` §6) and
neither ships a static library on Fedora, so they are the only shared
dependencies besides glibc. Every desktop Linux has both. `build-linux.sh`
checks the binary's direct `NEEDED` entries and refuses anything else. The
Windows `.exe` is unaffected and stays one self-contained file.

Builds reuse an already-compiled ffmpeg unless `--clean`. If the recipe changed
since that compile, the script refuses to land or stamp it and tells you to
rerun with `--clean`.

The Windows build runs **on** a Windows machine over SSH, inside the MSYS2
MINGW64 shell (ffmpeg and x264 are autoconf projects and need a POSIX shell).
`--setup` installs `mingw-w64-x86_64-toolchain`, `mingw-w64-x86_64-nasm`,
`make`, `pkg-config`, `git`, `autoconf`, `automake`, `libtool`, `diffutils` and
`python` via pacman. It is a separate, announced step and never happens as a
side effect of a build.

## Verifying

**Do not trust the flag list — run the binary.** A mis-specified flag list here
fails silently in several places, and two of those were found only by running:

- `--enable-demuxer=s16le` configures without complaint and produces an ffmpeg
  with **no** PCM demuxer. The component is `pcm_s16le`; `s16le` is only its
  display name.
- A bare `-` on the command line resolves to the **`fd:`** protocol, not
  `pipe:`, since ffmpeg 7.x. Without `--enable-protocol=fd` every video call
  site dies at startup with *"Protocol not found"*.

```sh
python3 verify.py dist/linux-x86_64/ffmpeg
```

`verify.py` checks the component tables, asserts the licence is what we claim,
and then replays all seven call sites end to end: it pipes synthetic
`bgra`/`rgba` frames in and checks the Annex-B output actually contains AUD
NALs, remuxes to MP4 and checks the `ftyp` box, and — for audio — stands up a
loopback TCP listener, feeds 48 kHz stereo `s16le` at it, and counts the RTP
datagrams that come back out while checking the SDP on stdout.

It distinguishes **"not compiled in"** (a build defect, fails the build) from
**"this machine's GPU or driver refused"** (reported as `HWSKIP`). The two are
never conflated, because an `h264_nvenc` that is absent and an `h264_nvenc`
that cannot open a device look similar in a log and mean opposite things.

### One thing verification found that is not a build problem

Lethal Company's nvenc probe renders **64x64**, and NVENC rejects it:
*"Frame Dimension less than the minimum supported value."* That is a hardware
limit, not a missing feature — H.264 NVENC's minimum width is 145 — and it was
reproduced on two different GPUs (RTX 3090, RTX 4080) with both this build and
a stock distribution ffmpeg. So the probe as written reports "nvenc unusable"
on every NVIDIA card, and Lethal Company always takes the libx264 branch.

`verify.py` reports that call site as `HWSKIP` rather than `FAIL`, because the
encoder is provably in the binary — the identical 320x240 encode next to it
passes. Fixing the probe's resolution belongs to the mod, not here.

## How mods should locate this binary

Do **not** hard-pin the bundled binary. Resolution order, in every mod:

1. **`DROKK_FFMPEG`** — if set, use exactly that path, and fail loudly if it is
   not usable. This is the escape hatch: a user whose GPU, driver or distro
   makes our build unhappy can point at their own ffmpeg without a new release.
2. **The bundled binary** — `<mod>/runtime/ffmpeg.exe` on Windows,
   `<mod>/runtime/ffmpeg` on Linux.
3. **`PATH`** — a plain `ffmpeg` lookup.

Step 3 matters more than it looks. Native-Linux 7 Days to Die currently uses
the distribution's ffmpeg and gets nvenc, Opus and RTP from it for free.
Bundling our own build *replaces* a full-featured ffmpeg with a deliberately
minimal one, which moves that risk onto us, on the platform we can test least.
Keeping the `PATH` fallback — and the env override above it — means a bad
drokk-ffmpeg build degrades to "the old behaviour" instead of "no puppet".

One constraint is not negotiable: on Windows, and under Proton, the resolved
binary must be a Windows PE. Silent Hill 2 rejects any candidate whose basename
is not `ffmpeg.exe`, and any `/usr/...` path, on purpose — handing a Linux ELF
to a Proton process produces an unhelpful `CreateProcess` failure.

## Layout

```
PINNED               upstream tag + commit for ffmpeg, x264, nv-codec-headers, opus
configure-flags.sh   the shared flag list, sourced by both build scripts
build-linux.sh       native Linux build
build-win64.sh       Windows build, driven over SSH against a Windows box
remote-build.sh      the Windows half, as it runs inside the MSYS2 MINGW64 shell
sign-win64.sh        Authenticode signing (delegates to the mod repo's sign_remote.sh)
verify.py            runs the binary against all seven real call sites
licenses/            GPLv2, LGPLv2.1, x264, Opus, nv-codec-headers
dist/                gitignored: built artifacts + SHA256SUMS + BUILD-INFO (see build-info.sh)
```

## Attribution

This software uses libraries from the FFmpeg project under the GPLv2-or-later,
and includes:

- **FFmpeg** — © the FFmpeg developers, <https://ffmpeg.org>
- **x264** — © VideoLAN and contributors, <https://www.videolan.org/developers/x264.html>
- **Opus** — © Xiph.Org, Skype Limited, Octasic, Jean-Marc Valin,
  Timothy B. Terriberry, CSIRO, Gregory Maxwell, Mark Borgerding,
  Erik de Castro Lopo, Mozilla, Amazon — <https://opus-codec.org>
- **nv-codec-headers** — © NVIDIA Corporation (MIT-style; headers only, nothing
  from the NVENC SDK is linked)
