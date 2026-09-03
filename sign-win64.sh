#!/usr/bin/env bash
# Authenticode-sign dist/win64/ffmpeg.exe (or any PE given on the command line).
#
# THIS IS A THIN WRAPPER, ON PURPOSE. All signing goes through DrokkPuppet's
# sign_remote.sh, which signs on the Windows box with Azure Artifact Signing.
# Two things follow and neither is negotiable:
#
#   1. Signing CANNOT be done from Linux. The signing dlib authenticates on the
#      machine it runs on, so a Linux `az login` cannot be handed across. Any
#      azuresigntool-from-Linux recipe you find in an older document is wrong;
#      that is the entire reason the remote flow exists.
#   2. Timestamping is mandatory (`/tr`). Azure Artifact Signing certificates
#      rotate every few days, so an untimestamped signature is dead on arrival.
#      sign_remote.sh asserts the word "timestamp" appears in the verify output
#      and dies if it does not.
#
# Signing is for OUR OWN build only. We sign it because we built it from the
# pinned sources in PINNED with the flags in configure-flags.sh, so the
# publisher claim the signature makes is true. Never sign a third-party ffmpeg.
#
# The signer script lives in the (private) DrokkPuppet mod repo, not here, so
# this file is useful to a reader of the public repo as documentation and inert
# without it -- which is correct: a public reader has no signing key either.
#
#   ./sign-win64.sh                     # sign dist/win64/ffmpeg.exe
#   ./sign-win64.sh path/to/other.exe   # sign something else
#   SIGN_REMOTE=/path/to/sign_remote.sh ./sign-win64.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SIGNER="$HERE/../../repo_games/silent_hill_2/mods/DrokkPuppet/sign_remote.sh"
SIGN_REMOTE="${SIGN_REMOTE:-$DEFAULT_SIGNER}"

# Passed through so the signer and the builder agree on which box and which
# scratch root, exactly as DrokkPuppet's build_remote.sh does it.
export REMOTE="${REMOTE:-joshi@192.168.1.114}"
export SCRATCH_WIN="${SCRATCH_WIN:-C:\\Users\\joshi\\drokkbuild}"
export SCRATCH_POSIX="${SCRATCH_POSIX:-C:/Users/joshi/drokkbuild}"

die() { echo "==> ERROR: $*" >&2; exit 1; }

FILES=("$@")
[ ${#FILES[@]} -gt 0 ] || FILES=("$HERE/dist/win64/ffmpeg.exe")

for f in "${FILES[@]}"; do
  [ -f "$f" ] || die "no such file: $f (build it first with ./build-win64.sh)"
  file "$f" | grep -q "PE32" || die "not a Windows PE, refusing to sign: $f"
done

[ -x "$SIGN_REMOTE" ] || die "signer not found: $SIGN_REMOTE
  It lives in the DrokkPuppet mod repo and is not part of this public repo.
  Set SIGN_REMOTE=/path/to/sign_remote.sh if it is elsewhere."

# One invocation, all files: one SSH round trip, one Azure auth, one dlib start.
# Re-signing an already-signed file is safe -- signtool replaces the signature.
"$SIGN_REMOTE" "${FILES[@]}" || die "signing failed"

# Signing REWRITES the binary -- it appends a PE certificate table, so the file
# grows and its hash changes. build-win64.sh wrote SHA256SUMS from the unsigned
# artifact, which is now wrong. Refresh it here rather than in the build script,
# because signing is the last step that mutates the file and the release tooling
# (SHARE_PARTS.md 4.2) verifies staged binaries against exactly these sums.
for f in "${FILES[@]}"; do
  d="$(dirname "$f")"
  ( cd "$d" && sha256sum "$(basename "$f")" > SHA256SUMS ) \
    || die "could not refresh SHA256SUMS in $d"
  echo "==> SHA256SUMS refreshed: $(cut -d' ' -f1 "$d/SHA256SUMS")  $(basename "$f")"
done
