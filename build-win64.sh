#!/usr/bin/env bash
# Build ffmpeg.exe for Windows x86_64 on a real Windows machine over SSH, then
# pull it back into dist/.
#
# WHY NOT A MINGW CROSS-BUILD FROM LINUX: the project's locked decision is that
# every Windows artifact is produced (and signed) on the Windows box, so that
# what ships is what was tested, on the OS it runs on. A cross-build would also
# have no way to exercise h264_nvenc against a real driver.
#
# WHY NOT THE PLAIN-POWERSHELL FLOW the C++/CMake build uses: ffmpeg's and
# x264's build systems are autoconf + make, i.e. POSIX shell scripts. They must
# run inside the MSYS2 MINGW64 environment. This script therefore ships
# remote-build.sh up and runs it via `bash.exe -lc` with MSYSTEM=MINGW64,
# reusing only the *transport* mechanics of the CMake flow (EncodedCommand over
# SSH, one scratch root, scp both ways, verify content not existence).
#
#   ./build-win64.sh --setup    # ONE TIME: install the MSYS2 toolchain (see below)
#   ./build-win64.sh            # build + verify + pull back
#   ./build-win64.sh --clean    # wipe the remote build tree first
#   ./build-win64.sh --sign     # ...and Authenticode-sign the result
#   ./build-win64.sh --verify   # re-run verification against what is already there
set -uo pipefail

REMOTE="${REMOTE:-joshi@192.168.1.114}"
SCRATCH_WIN="${SCRATCH_WIN:-C:\\Users\\joshi\\drokkbuild}"
SCRATCH_POSIX="${SCRATCH_POSIX:-C:/Users/joshi/drokkbuild}"   # scp/sftp form (NOT msys /c/... -- Windows OpenSSH rejects it)
MSYS_ROOT="${MSYS_ROOT:-C:\\msys64}"
MIN_FREE_GB="${MIN_FREE_GB:-8}"

FF_WIN="$SCRATCH_WIN\\ffmpeg"
FF_POSIX="$SCRATCH_POSIX/ffmpeg"
FF_MSYS="/c/Users/joshi/drokkbuild/ffmpeg"      # same dir, MSYS2 path form
[ -n "${FF_MSYS_OVERRIDE:-}" ] && FF_MSYS="$FF_MSYS_OVERRIDE"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$HERE/dist/win64"
EXE_DEST="$DIST/ffmpeg.exe"

if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_YEL=$'\033[33m'; C_GRN=$'\033[32m'; C_CYA=$'\033[36m'; C_RST=$'\033[0m'
else
  C_RED=""; C_YEL=""; C_GRN=""; C_CYA=""; C_RST=""
fi
say()  { echo "${C_CYA}==>${C_RST} $*"; }
ok()   { echo "${C_GRN}==>${C_RST} $*"; }
warn() { echo "${C_YEL}==> WARN:${C_RST} $*"; }
die()  { echo "${C_RED}==> ERROR:${C_RST} $*" >&2; exit 1; }

SETUP=0; CLEAN=0; SIGN=0; ONLY_VERIFY=0
while [ $# -gt 0 ]; do
  case "$1" in
    -setup|--setup)   SETUP=1 ;;
    -clean|--clean)   CLEAN=1 ;;
    -sign|--sign)     SIGN=1 ;;
    -verify|--verify) ONLY_VERIFY=1 ;;
    -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
  shift
done

# PowerShell over SSH: the remote default shell is PowerShell, so plain
# `ssh host "cmd"` mis-parses quoting, $ and &. -EncodedCommand takes UTF-16LE
# base64 and sidesteps all of it. Helper deliberately identical to the one in
# DrokkPuppet's build_remote.sh / sign_remote.sh.
psh() {
  local script="\$ProgressPreference='SilentlyContinue'
$1"
  local b64
  b64="$(printf '%s' "$script" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)" || return 1
  ssh -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE" \
      powershell -NoProfile -EncodedCommand "$b64"
}

# Run a command in the MSYS2 MINGW64 login shell. MSYSTEM must be set BEFORE
# bash runs: /etc/profile builds PATH from it, and getting that ordering by
# hand (mingw64/bin ahead of usr/bin) is exactly the mistake this avoids.
msys() {
  psh "\$env:MSYSTEM='MINGW64'
\$env:CHERE_INVOKING='1'
& '$MSYS_ROOT\\usr\\bin\\bash.exe' -lc '$1'
exit \$LASTEXITCODE"
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
say "remote: $REMOTE   scratch: $FF_WIN"
ssh -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE" "echo ok" >/dev/null 2>&1 \
  || die "cannot SSH to $REMOTE with BatchMode (key auth)."
ok "SSH reachable"

psh "Test-Path '$MSYS_ROOT\\usr\\bin\\bash.exe'" | grep -qi true \
  || die "MSYS2 not found at $MSYS_ROOT on $REMOTE. Install it (https://www.msys2.org) and re-run --setup."

free_gb="$(psh "[math]::Floor((Get-PSDrive C).Free / 1GB)" | tr -d '\r' | tail -1)"
if [[ "$free_gb" =~ ^[0-9]+$ ]]; then
  say "C: free = ${free_gb} GB"
  [ "$free_gb" -lt "$MIN_FREE_GB" ] \
    && die "only ${free_gb} GB free on C: (floor ${MIN_FREE_GB}). Refusing to fill the machine."
else
  warn "could not read free space on C: (got '$free_gb')"
fi

# ---------------------------------------------------------------------------
# --setup: the one-time toolchain install. ANNOUNCED AND EXPLICIT -- this
# installs software on someone's work PC, so it never happens as a side effect
# of a build. A base MSYS2 install has pacman and nothing to compile with.
# ---------------------------------------------------------------------------
PACMAN_PKGS="mingw-w64-x86_64-toolchain mingw-w64-x86_64-nasm make pkg-config git autoconf automake libtool diffutils python"
if [ "$SETUP" = "1" ]; then
  warn "--setup will INSTALL PACKAGES on $REMOTE via MSYS2 pacman:"
  for p in $PACMAN_PKGS; do echo "      $p"; done
  echo "    toolchain/nasm: build ffmpeg + x264 at all (a base MSYS2 has no compiler)"
  echo "    make/pkg-config/git/autoconf/automake/libtool/diffutils: autoconf's own prerequisites"
  echo "                    (opus ships no configure script; ./autogen.sh needs the autotools)"
  echo "    python:         runs verify.py ON the box, so the exe is exercised where it will run"
  say "installing (this can take several minutes)"
  msys "pacman -Sy --noconfirm && pacman -S --noconfirm --needed $PACMAN_PKGS" \
    || die "pacman install failed"
  msys "gcc --version | head -1; nasm -v; make --version | head -1; python --version" \
    || die "toolchain still not usable after install"
  ok "MSYS2 toolchain ready"
fi

# ---------------------------------------------------------------------------
# Ship the recipe up. The SOURCES are never shipped: remote-build.sh clones the
# pins on the box, so only these four small files cross the wire.
# ---------------------------------------------------------------------------
if [ "$CLEAN" = "1" ]; then
  warn "--clean: removing $FF_WIN on the remote"
  psh "if (Test-Path '$FF_WIN') { Remove-Item -Recurse -Force '$FF_WIN' }" >/dev/null
fi
psh "New-Item -ItemType Directory -Force -Path '$FF_WIN' | Out-Null" >/dev/null
. "$HERE/build-info.sh"
RECIPE_HASH="$(drokk_ffmpeg_recipe_hash "$HERE")"

for f in PINNED configure-flags.sh remote-build.sh verify.py; do
  scp -q -o BatchMode=yes "$HERE/$f" "$REMOTE:$FF_POSIX/$f" || die "scp failed: $f"
done
ok "recipe staged (PINNED, configure-flags.sh, remote-build.sh, verify.py)"

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
if [ "$ONLY_VERIFY" = "0" ]; then
  LOG="$(mktemp -p "${TMPDIR:-/tmp}" drokk-ffmpeg-win64.XXXXXX.log)"
  say "building on $REMOTE (log: $LOG)"
  msys "cd $FF_MSYS && chmod +x remote-build.sh && DROKK_RECIPE_HASH=$RECIPE_HASH ./remote-build.sh" 2>&1 | tee "$LOG"
  if [ "${PIPESTATUS[0]}" -ne 0 ]; then
    grep -iE "error|fatal" "$LOG" | tail -30
    die "remote build failed -- full log: $LOG
  Build logs stay on the box under $FF_WIN\\build\\*.log"
  fi
  ok "remote build finished"
fi
# remote-build.sh reuses an existing ffmpeg.exe; only land one compiled from this recipe.
BUILT_FROM="$(msys "cat $FF_MSYS/build/ffmpeg/drokk-recipe-hash 2>/dev/null" | tr -d '\r' | tail -1)"
drokk_ffmpeg_require_built_from "$BUILT_FROM" "$RECIPE_HASH" || exit 1

# ---------------------------------------------------------------------------
# Verify ON the box, where the exe will actually run (and where the NVIDIA
# driver is), before it is allowed anywhere near dist/.
# ---------------------------------------------------------------------------
say "verifying on $REMOTE by running every real call site"
msys "cd $FF_MSYS && python verify.py build/ffmpeg/ffmpeg.exe --outdir verify-out" \
  || die "verification FAILED on the remote box -- not landing the binary"
ok "remote verification passed"

# ---------------------------------------------------------------------------
# Pull it back and check content, not existence
# ---------------------------------------------------------------------------
mkdir -p "$DIST"
scp -q -o BatchMode=yes "$REMOTE:$FF_POSIX/build/ffmpeg/ffmpeg.exe" "$EXE_DEST" \
  || die "could not fetch ffmpeg.exe"
file "$EXE_DEST" | grep -q "PE32+" || die "fetched file is not a 64-bit Windows PE: $(file "$EXE_DEST")"
say "size: $(stat -c%s "$EXE_DEST") bytes ($(du -h "$EXE_DEST" | cut -f1))"
( cd "$DIST" && sha256sum ffmpeg.exe > SHA256SUMS )
ok "sha256: $(cut -d' ' -f1 "$DIST/SHA256SUMS")"

# ---------------------------------------------------------------------------
# Signing is last, opt-in, and only ever runs on an artifact that verified.
# ---------------------------------------------------------------------------
if [ "$SIGN" = "1" ]; then
  REMOTE="$REMOTE" SCRATCH_WIN="$SCRATCH_WIN" SCRATCH_POSIX="$SCRATCH_POSIX" \
    "$HERE/sign-win64.sh" "$EXE_DEST" || die "signing failed -- the unsigned exe is still at $EXE_DEST"
else
  say "unsigned build (pass --sign for a release build)"
fi
drokk_ffmpeg_write_build_info "$HERE" "$DIST" win64 ffmpeg.exe || die "could not write $DIST/BUILD-INFO"
ok "win64 build complete: $EXE_DEST"
