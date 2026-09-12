#!/usr/bin/env bash
# The Windows half of the build, as it runs ON the Windows box, inside the
# MSYS2 MINGW64 shell. build-win64.sh ships this file up and invokes it; it is
# never run on Linux.
#
# WHY IT IS A SEPARATE FILE FROM build-linux.sh: ffmpeg's and x264's build
# systems are autoconf/make, so they need a POSIX shell -- MSYS2's bash with
# MSYSTEM=MINGW64, not the plain-PowerShell flow the C++/CMake build uses. It
# is also cheaper to reason about a script that only ever sees one environment
# than one straddling both. The pins and the configure flags are NOT duplicated:
# both scripts read the same PINNED and configure-flags.sh, which travel with
# this file.
#
# Deliberately native, not cross: MSYSTEM=MINGW64 gcc already targets
# x86_64-w64-mingw32, so no --cross-prefix/--target-os is needed or wanted.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$HERE/build"
PREFIX="$WORK/prefix"
JOBS="${JOBS:-$(nproc)}"

say()  { echo "==> $*"; }
die()  { echo "==> ERROR: $*" >&2; exit 1; }

[ "${MSYSTEM:-}" = "MINGW64" ] || die "must run with MSYSTEM=MINGW64 (got '${MSYSTEM:-unset}')"
command -v gcc >/dev/null || die "no gcc -- run build-win64.sh --setup first"
command -v nasm >/dev/null || die "no nasm -- run build-win64.sh --setup first"
gcc -dumpmachine | grep -q mingw || die "gcc is not a mingw compiler: $(gcc -dumpmachine)"

set -a; . <(grep -E '^[A-Z0-9_]+=' "$HERE/PINNED"); set +a
. "$HERE/configure-flags.sh"
# win64 explicitly: this script only ever runs under MSYSTEM=MINGW64 (asserted
# above), and the pulse/alsa flags configure-flags.sh adds for linux would kill
# ./configure here. See its PLATFORM ARGUMENT note.
mapfile -t FLAGS < <(drokk_ffmpeg_flags_clean win64)

mkdir -p "$WORK" "$PREFIX"

clone_pin() {
  local dir="$1" repo="$2" ref="$3" want="${4:-}"
  if [ -d "$WORK/$dir/.git" ]; then
    say "$dir already cloned"
  else
    say "cloning $dir @ $ref"
    git clone --depth 1 --branch "$ref" "$repo" "$WORK/$dir" >/dev/null 2>&1 \
      || die "clone failed: $repo @ $ref"
  fi
  local got; got="$(git -C "$WORK/$dir" rev-parse HEAD)"
  [ -z "$want" ] || [ "$got" = "$want" ] \
    || die "$dir pin mismatch -- PINNED says $want, clone gave $got"
  say "$dir at $got"
}

# --- x264 -------------------------------------------------------------------
clone_pin x264 "$X264_REPO" "$X264_BRANCH" "$X264_COMMIT"
if [ ! -f "$PREFIX/lib/pkgconfig/x264.pc" ]; then
  say "building x264"
  ( cd "$WORK/x264" \
    && ./configure --prefix="$PREFIX" --host=x86_64-w64-mingw32 \
                   --enable-static --enable-pic --disable-cli --disable-opencl \
    && make -j"$JOBS" && make install ) > "$WORK/x264.log" 2>&1 \
    || { grep -iE "error|fatal" "$WORK/x264.log" | tail -20; die "x264 failed -- $WORK/x264.log"; }
fi
say "x264 ok"

# --- opus -------------------------------------------------------------------
clone_pin opus "$OPUS_REPO" "$OPUS_TAG" "$OPUS_COMMIT"
if [ ! -f "$PREFIX/lib/pkgconfig/opus.pc" ]; then
  say "building opus"
  ( cd "$WORK/opus" && ./autogen.sh \
    && ./configure --prefix="$PREFIX" --host=x86_64-w64-mingw32 \
                   --enable-static --disable-shared \
                   --disable-doc --disable-extra-programs \
    && make -j"$JOBS" && make install ) > "$WORK/opus.log" 2>&1 \
    || { grep -iE "error|fatal" "$WORK/opus.log" | tail -20; die "opus failed -- $WORK/opus.log"; }
fi
say "opus ok"

# --- nv-codec-headers -------------------------------------------------------
clone_pin nv-codec-headers "$NVCODEC_REPO" "$NVCODEC_TAG" "$NVCODEC_COMMIT"
if [ ! -f "$PREFIX/lib/pkgconfig/ffnvcodec.pc" ]; then
  say "installing nv-codec-headers"
  make -C "$WORK/nv-codec-headers" install PREFIX="$PREFIX" > "$WORK/nvcodec.log" 2>&1 \
    || { tail -20 "$WORK/nvcodec.log"; die "nv-codec-headers failed"; }
fi
say "ffnvcodec ok"

# --- ffmpeg -----------------------------------------------------------------
clone_pin ffmpeg "$FFMPEG_REPO" "$FFMPEG_TAG" "$FFMPEG_COMMIT"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
if [ ! -x "$WORK/ffmpeg/ffmpeg.exe" ]; then
  say "configuring ffmpeg (${#FLAGS[@]} flags)"
  # --extra-ldflags=-static: mingw x264/opus pull libwinpthread and libgcc.
  # Without it the exe needs libwinpthread-1.dll beside it, which breaks the
  # one-file rule the mods depend on (they copy a single ffmpeg.exe into
  # runtime/).
  ( cd "$WORK/ffmpeg" \
    && ./configure "${FLAGS[@]}" \
         --prefix="$PREFIX" \
         --pkg-config-flags=--static \
         --extra-cflags="-I$PREFIX/include" \
         --extra-ldflags="-L$PREFIX/lib -static" \
    && make -j"$JOBS" ) > "$WORK/ffmpeg.log" 2>&1 \
    || { grep -iE "error|fatal|not found|no such" "$WORK/ffmpeg.log" | tail -30
         die "ffmpeg failed -- full log: $WORK/ffmpeg.log"; }
fi
[ -x "$WORK/ffmpeg/ffmpeg.exe" ] || die "no ffmpeg.exe produced"
strip "$WORK/ffmpeg/ffmpeg.exe" || echo "==> WARN: strip failed"
say "built: $(stat -c%s "$WORK/ffmpeg/ffmpeg.exe") bytes"

# One file, no DLLs: objdump the import table and refuse anything that is not a
# stock Windows system DLL. nvEncodeAPI64.dll must NOT appear -- it is
# LoadLibrary'd at runtime, which is what keeps this build free of --enable-nonfree.
BAD="$(objdump -p "$WORK/ffmpeg/ffmpeg.exe" | grep -i 'DLL Name:' \
       | grep -viE 'kernel32|user32|advapi32|ws2_32|secur32|bcrypt|shell32|ole32|psapi|msvcrt|ucrtbase|api-ms-win|iphlpapi|gdi32|version|shlwapi|mfplat|d3d11|dxgi|crypt32|winmm|cfgmgr32|ntdll|imm32|setupapi|vcruntime')" || true
[ -z "$BAD" ] || die "ffmpeg.exe has non-system DLL imports (not self-contained):
$BAD"
say "imports are stock system DLLs only"
