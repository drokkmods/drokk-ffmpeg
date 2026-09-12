#!/usr/bin/env bash
# Build the native Linux drokk-ffmpeg ELF.
#
# WHY: 7 Days to Die has a native Linux build and its mod shells out to ffmpeg.
# (Silent Hill 2 and Lethal Company are Windows-only; on Linux they run under
# Proton and get ffmpeg.exe, which is build-win64.sh's job. SH2's
# paths.RejectLinuxFFmpeg refuses an ELF outright, deliberately.)
#
# Everything is built from the pins in PINNED into a private prefix under
# build/. Nothing is installed system-wide and the system ffmpeg on PATH is
# never shadowed; the artifact lands in dist/ and nowhere else.
#
# Helper style (say/ok/warn/die, colors, -flag parsing) is deliberately copied
# from mods/DrokkPuppet/host/framedump/build_remote.sh so a reader of that
# script recognises this one.
#
#   ./build-linux.sh              # build (reuses build/ if present)
#   ./build-linux.sh --clean      # wipe build/ and start from nothing
#   ./build-linux.sh --verify     # skip the build, re-run verify.py on dist/
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WORK:-$HERE/build/linux}"
PREFIX="$WORK/prefix"
DIST="$HERE/dist/linux-x86_64"
JOBS="${JOBS:-$(nproc)}"

if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_YEL=$'\033[33m'; C_GRN=$'\033[32m'; C_CYA=$'\033[36m'; C_RST=$'\033[0m'
else
  C_RED=""; C_YEL=""; C_GRN=""; C_CYA=""; C_RST=""
fi
say()  { echo "${C_CYA}==>${C_RST} $*"; }
ok()   { echo "${C_GRN}==>${C_RST} $*"; }
warn() { echo "${C_YEL}==> WARN:${C_RST} $*"; }
die()  { echo "${C_RED}==> ERROR:${C_RST} $*" >&2; exit 1; }

CLEAN=0; ONLY_VERIFY=0
while [ $# -gt 0 ]; do
  case "$1" in
    -clean|--clean)   CLEAN=1 ;;
    -verify|--verify) ONLY_VERIFY=1 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Pins and flags -- PINNED and configure-flags.sh are the source of truth
# ---------------------------------------------------------------------------
# shellcheck disable=SC1090
set -a; . <(grep -E '^[A-Z0-9_]+=' "$HERE/PINNED"); set +a
. "$HERE/configure-flags.sh"
# "linux" is not the default-by-uname: it is the whole point of this script, and
# an unqualified call here would silently build a win64 flag set if the function's
# platform guess ever changed. See configure-flags.sh's PLATFORM ARGUMENT note.
mapfile -t FLAGS < <(drokk_ffmpeg_flags_clean linux)

if [ "$ONLY_VERIFY" = "1" ]; then
  [ -x "$DIST/ffmpeg" ] || die "no binary at $DIST/ffmpeg"
  exec python3 "$HERE/verify.py" "$DIST/ffmpeg" --outdir "$WORK/verify-out"
fi

# ---------------------------------------------------------------------------
# Preflight: fail before a 20-minute build, not during it
# ---------------------------------------------------------------------------
for t in git make gcc pkg-config nasm python3 autoconf automake libtool; do
  command -v "$t" >/dev/null 2>&1 || die "missing build tool: $t
  On Fedora/Nobara:  sudo dnf install git make gcc pkgconf-pkg-config nasm autoconf automake libtool"
done
# x264's asm needs nasm >= 2.13; the distro yasm 1.3 is NOT a substitute.
say "nasm: $(nasm -v | head -1)"
# --enable-libpulse/--enable-alsa (configure-flags.sh's linux block) are FATAL
# to ./configure when the dev packages are missing -- require_pkg_config and
# "alsa requested but not found" both die -- so catch it here rather than
# fifteen minutes into x264.
for pc in libpulse alsa; do
  pkg-config --exists "$pc" || die "missing dev package for pkg-config module: $pc
  The native-Linux audio devices (AUDIO_PLAN.md section 6) need both.
  On Fedora/Nobara:  sudo dnf install pulseaudio-libs-devel alsa-lib-devel"
done
say "libpulse: $(pkg-config --modversion libpulse)   alsa: $(pkg-config --modversion alsa)"

[ "$CLEAN" = "1" ] && { warn "--clean: removing $WORK"; rm -rf "$WORK"; }
mkdir -p "$WORK" "$PREFIX" "$DIST"

# clone_pin <dir> <repo> <ref> [<expected-commit>]
clone_pin() {
  local dir="$1" repo="$2" ref="$3" want="${4:-}"
  if [ -d "$WORK/$dir/.git" ]; then
    ok "$dir already cloned"
  else
    say "cloning $dir @ $ref"
    git clone --depth 1 --branch "$ref" "$repo" "$WORK/$dir" >/dev/null 2>&1 \
      || die "clone failed: $repo @ $ref"
  fi
  local got; got="$(git -C "$WORK/$dir" rev-parse HEAD)"
  if [ -n "$want" ] && [ "$got" != "$want" ]; then
    die "$dir pin mismatch -- PINNED says $want, clone gave $got.
  Either upstream moved the ref or PINNED is stale. Do not build past this."
  fi
  ok "$dir at $got"
}

# ---------------------------------------------------------------------------
# 1. x264 (GPLv2-or-later) -- SH2's only encoder, LC's fallback branch
# ---------------------------------------------------------------------------
clone_pin x264 "$X264_REPO" "$X264_BRANCH" "$X264_COMMIT"
if [ ! -f "$PREFIX/lib/pkgconfig/x264.pc" ]; then
  say "building x264"
  ( cd "$WORK/x264" \
    && ./configure --prefix="$PREFIX" --enable-static --enable-pic \
                   --disable-cli --disable-opencl \
    && make -j"$JOBS" && make install ) > "$WORK/x264.log" 2>&1 \
    || { grep -iE "error|fatal" "$WORK/x264.log" | tail -20; die "x264 build failed -- $WORK/x264.log"; }
  ok "x264 installed"
else ok "x264 already installed"; fi

# ---------------------------------------------------------------------------
# 2. opus (BSD-3-Clause) -- the LC and 7D audio pipes
# ---------------------------------------------------------------------------
clone_pin opus "$OPUS_REPO" "$OPUS_TAG" "$OPUS_COMMIT"
if [ ! -f "$PREFIX/lib/pkgconfig/opus.pc" ]; then
  say "building opus"
  ( cd "$WORK/opus" && ./autogen.sh \
    && ./configure --prefix="$PREFIX" --enable-static --disable-shared \
                   --disable-doc --disable-extra-programs \
    && make -j"$JOBS" && make install ) > "$WORK/opus.log" 2>&1 \
    || { grep -iE "error|fatal" "$WORK/opus.log" | tail -20; die "opus build failed -- $WORK/opus.log"; }
  ok "opus installed"
else ok "opus already installed"; fi

# ---------------------------------------------------------------------------
# 3. nv-codec-headers -- headers + a .pc only, no compiler involved.
#    This is what makes h264_nvenc available; the NVENC SDK itself is NOT
#    linked, it is dlopen'd at runtime, which is why we need no --enable-nonfree
#    and why a machine with no NVIDIA GPU still builds fine.
# ---------------------------------------------------------------------------
clone_pin nv-codec-headers "$NVCODEC_REPO" "$NVCODEC_TAG" "$NVCODEC_COMMIT"
if [ ! -f "$PREFIX/lib/pkgconfig/ffnvcodec.pc" ]; then
  say "installing nv-codec-headers"
  make -C "$WORK/nv-codec-headers" install PREFIX="$PREFIX" > "$WORK/nvcodec.log" 2>&1 \
    || { tail -20 "$WORK/nvcodec.log"; die "nv-codec-headers install failed"; }
  ok "ffnvcodec headers installed"
else ok "ffnvcodec headers already installed"; fi

# ---------------------------------------------------------------------------
# 4. ffmpeg
# ---------------------------------------------------------------------------
clone_pin ffmpeg "$FFMPEG_REPO" "$FFMPEG_TAG" "$FFMPEG_COMMIT"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:$PREFIX/lib64/pkgconfig"
if [ ! -x "$WORK/ffmpeg/ffmpeg" ]; then
  say "configuring ffmpeg (${#FLAGS[@]} flags from configure-flags.sh)"
  ( cd "$WORK/ffmpeg" \
    && ./configure "${FLAGS[@]}" \
         --prefix="$PREFIX" \
         --pkg-config-flags=--static \
         --extra-cflags="-I$PREFIX/include" \
         --extra-ldflags="-L$PREFIX/lib" \
    && make -j"$JOBS" ) > "$WORK/ffmpeg.log" 2>&1 \
    || { grep -iE "error|fatal|not found|no such" "$WORK/ffmpeg.log" | tail -30
         die "ffmpeg build failed -- full log: $WORK/ffmpeg.log"; }
  ok "ffmpeg built"
else ok "ffmpeg already built"; fi

# ---------------------------------------------------------------------------
# 5. Land it in dist/ and verify by RUNNING it
# ---------------------------------------------------------------------------
install -m 0755 "$WORK/ffmpeg/ffmpeg" "$DIST/ffmpeg"
strip "$DIST/ffmpeg" 2>/dev/null || warn "strip failed (binary still valid, just larger)"
say "size: $(stat -c%s "$DIST/ffmpeg") bytes ($(du -h "$DIST/ffmpeg" | cut -f1))"
file "$DIST/ffmpeg" | sed 's/^/    /'
# Self-contained means self-contained: nothing from $PREFIX may be a shared dep.
if ldd "$DIST/ffmpeg" 2>/dev/null | grep -qiE "x264|opus|avcodec|avformat"; then
  die "binary links a shared libav*/x264/opus -- the static build did not take:
$(ldd "$DIST/ffmpeg")"
fi
( cd "$DIST" && sha256sum ffmpeg > SHA256SUMS )
ok "sha256: $(cut -d' ' -f1 "$DIST/SHA256SUMS")"

say "verifying by running every real call site"
python3 "$HERE/verify.py" "$DIST/ffmpeg" --outdir "$WORK/verify-out" || die "verification FAILED"
ok "linux build complete: $DIST/ffmpeg"
