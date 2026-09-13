# shellcheck shell=bash
# Sourced by build-linux.sh and build-win64.sh. Writes dist/<flavour>/BUILD-INFO,
# the record of what produced the binary beside it.
#
# WHY: dist/ used to hold only the binary and SHA256SUMS, so nothing could tell a
# September-3 ffmpeg from one built against today's configure-flags.sh. The
# release pipeline (drokkenemies RELEASE_PLAN.md section 2.4) recomputes
# RECIPE_HASH from this repo and refuses to ship a dist/ whose BUILD-INFO
# disagrees, or that has no BUILD-INFO at all.
#
# FORMAT (SCHEMA=1): KEY=VALUE lines, '#' comments -- the same shape as PINNED,
# so a reader can take it with  grep -E '^[A-Z0-9_]+=' BUILD-INFO.
#   SCHEMA               1
#   FLAVOUR              linux-x86_64 | win64
#   RECIPE_HASH          sha256 hex, see drokk_ffmpeg_recipe_hash below
#   RECIPE_DIRTY         0 | 1  (recipe files had uncommitted changes at build time)
#   FFMPEG_TAG           from PINNED
#   FFMPEG_COMMIT        from PINNED
#   DROKK_FFMPEG_COMMIT  git HEAD of this repo
#   BUILT_AT             UTC, ISO-8601, e.g. 2026-09-12T18:22:04Z
#   BINARY               ffmpeg | ffmpeg.exe (file name inside the same dir)
#   BINARY_SHA256        sha256 of BINARY as landed (post-sign on win64)
#
# It is written LAST, after verification (and signing), so a BUILD-INFO exists
# only beside a binary that passed.

# The recipe inputs, in this order. RELEASE_PLAN.md section 2.3's ffmpeg row;
# change both together or every check will refuse.
DROKK_FFMPEG_RECIPE=(PINNED configure-flags.sh build-linux.sh build-win64.sh remote-build.sh)

# RELEASE_PLAN.md section 2.2's component_hash, verbatim: committed tree/blob
# hashes, plus sha256 of each dirty file, so an uncommitted recipe hashes to what
# its contents are rather than to HEAD.
drokk_ffmpeg_recipe_hash() {
  local repo="$1"
  {
    for p in "${DROKK_FFMPEG_RECIPE[@]}"; do git -C "$repo" rev-parse "HEAD:$p" 2>/dev/null || echo "absent:$p"; done
    git -C "$repo" status --porcelain -- "${DROKK_FFMPEG_RECIPE[@]}" \
      | while read -r _st f; do [ -f "$repo/$f" ] && sha256sum "$repo/$f"; done
  } | sha256sum | cut -d' ' -f1
}

# drokk_ffmpeg_require_built_from <hash-recorded-at-compile> <current-recipe-hash>
# The build scripts reuse an already-compiled ffmpeg unless --clean. Each writes
# the recipe hash beside the binary when it actually compiles
# (build/.../ffmpeg/drokk-recipe-hash); this refuses to land or stamp a binary
# whose recorded hash is missing or differs, so BUILD-INFO never claims a recipe
# that did not produce the bytes.
drokk_ffmpeg_require_built_from() {
  [ -n "$1" ] && [ "$1" = "$2" ] && return 0
  echo "==> ERROR: the compiled ffmpeg was not built from the current recipe
  compiled from: ${1:-unknown (no drokk-recipe-hash beside the binary)}
  recipe now:    $2
  Refusing to land or stamp it. Rerun with --clean." >&2
  return 1
}

# drokk_ffmpeg_write_build_info <repo> <dist-dir> <flavour> <binary-name>
# Reads FFMPEG_TAG / FFMPEG_COMMIT from PINNED itself: build-win64.sh never
# sources PINNED locally (remote-build.sh does, on the build box).
drokk_ffmpeg_write_build_info() {
  local repo="$1" dist="$2" flavour="$3" bin="$4" dirty=0
  [ -f "$dist/$bin" ] || { echo "==> ERROR: BUILD-INFO: no $dist/$bin" >&2; return 1; }
  local FFMPEG_TAG FFMPEG_COMMIT
  FFMPEG_TAG="$(sed -n 's/^FFMPEG_TAG=//p' "$repo/PINNED")"
  FFMPEG_COMMIT="$(sed -n 's/^FFMPEG_COMMIT=//p' "$repo/PINNED")"
  [ -n "$FFMPEG_TAG" ] && [ -n "$FFMPEG_COMMIT" ] || { echo "==> ERROR: BUILD-INFO: FFMPEG_TAG/FFMPEG_COMMIT missing from $repo/PINNED" >&2; return 1; }
  [ -n "$(git -C "$repo" status --porcelain -- "${DROKK_FFMPEG_RECIPE[@]}")" ] && dirty=1
  cat > "$dist/BUILD-INFO.tmp" <<EOF || return 1
# drokk-ffmpeg BUILD-INFO -- written by the build, never by hand. See build-info.sh.
SCHEMA=1
FLAVOUR=$flavour
RECIPE_HASH=$(drokk_ffmpeg_recipe_hash "$repo")
RECIPE_DIRTY=$dirty
FFMPEG_TAG=$FFMPEG_TAG
FFMPEG_COMMIT=$FFMPEG_COMMIT
DROKK_FFMPEG_COMMIT=$(git -C "$repo" rev-parse HEAD)
BUILT_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
BINARY=$bin
BINARY_SHA256=$(sha256sum "$dist/$bin" | cut -d' ' -f1)
EOF
  mv -f "$dist/BUILD-INFO.tmp" "$dist/BUILD-INFO"
}
