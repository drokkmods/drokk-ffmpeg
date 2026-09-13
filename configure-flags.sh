#!/usr/bin/env bash
# The shared ffmpeg ./configure flag list. Sourced by build-linux.sh and
# build-win64.sh; also runnable on its own (`./configure-flags.sh [platform]`)
# to print the flags one per line.
#
# METHOD: start from --disable-everything and add back ONLY what the mods'
# actual ffmpeg command lines prove they need. Every flag below is traceable to
# a specific invocation; the seven of them are enumerated in README.md, the
# four audio-path additions are AUDIO_PLAN.md section 6, and the turn-recording
# remux additions (ogg demuxer, opus parser) are AUDIO_PLAN.md section 9.6. Do
# not add a flag without a call site, and do not remove one without checking
# all of the above.
#
# The single most important thing to know about this list: a WRONG list mostly
# fails SILENTLY. Both Lethal Company and 7 Days treat a dead audio ffmpeg as a
# warning and continue video-only, so an ffmpeg missing libopus, tcp or rtp
# produces a session that looks fine and has no sound. That is why the build
# scripts run every call site's real argv against synthetic input instead of
# grepping `-encoders` and calling it done.
#
# PLATFORM ARGUMENT: `linux` or `win64`. Almost everything is shared; the only
# divergence is the native-Linux audio devices (pulse/alsa), which CANNOT go in
# the shared list because --enable-libpulse and --enable-alsa are hard failures
# on mingw -- configure's require_pkg_config/"requested but not found" paths
# both `die`. Enabling a *component* whose deps are missing only warns
# (configure.4679 warn_if_gets_disabled), but enabling a *library* that is not
# there kills the build. That asymmetry is the whole reason for the split.

set -euo pipefail

drokk_ffmpeg_flags() {
  local platform="${1:-${DROKK_FFMPEG_PLATFORM:-}}"
  if [ -z "$platform" ]; then
    case "$(uname -s)" in
      MINGW*|MSYS*|CYGWIN*) platform=win64 ;;
      *)                    platform=linux ;;
    esac
  fi
  case "$platform" in
    linux|win64) ;;
    *) echo "drokk_ffmpeg_flags: unknown platform '$platform' (want linux|win64)" >&2
       return 2 ;;
  esac

  cat <<'FLAGS'
# --- shape -------------------------------------------------------------------
--disable-everything
--disable-autodetect
--disable-doc
--disable-htmlpages
--disable-manpages
--disable-podpages
--disable-txtpages
--disable-ffplay
--disable-ffprobe
--disable-debug
--enable-small
--enable-static
--disable-shared

# --- licence -----------------------------------------------------------------
# --enable-gpl is pulled in by libx264 (GPLv2-or-later).
# Deliberately NO --enable-version3: neither libx264 (GPLv2+) nor libopus
# (BSD-3-Clause) appears in configure's EXTERNAL_LIBRARY_VERSION3_LIST, so the
# result is GPLv2-or-later and licenses/COPYING.GPLv2 is the text we owe.
# Deliberately NO --enable-nonfree: nvenc is dlopen/LoadLibrary'd
# (nvenc_deps_any="libdl LoadLibrary"), never linked.
--enable-gpl

# --- external libraries ------------------------------------------------------
--enable-libx264
--enable-libopus
--enable-ffnvcodec
--enable-nvenc

# --- components --------------------------------------------------------------
# pcm_s16le is an ENCODER as well as a decoder here. AUDIO_PLAN.md section 4.2's
# decode-to-PCM call site ends `-f s16le -ar 48000 -ac 2 -`, and that output
# leg needs BOTH the pcm_s16le muxer (below) and the pcm_s16le encoder, because
# the s16le muxer's default codec is pcm_s16le and there is nothing else it can
# fall back to. Neither was needed while ffmpeg only ever READ s16le.
--enable-encoder=libx264,h264_nvenc,libopus,pcm_s16le
# wrapped_avframe is NOT optional: the lavfi indev hands frames to the ffmpeg
# CLI as wrapped_avframe packets, so Lethal Company's nvenc probe
# (-f lavfi -i color=...) dies with "no decoder found for: wrapped_avframe"
# without it. Another one only a real run finds.
#
# libopus is the DECODER, a separate component from the libopus encoder above
# (configure has libopus_decoder_deps and libopus_encoder_deps as two entries).
# Enabling the encoder does not get you the decoder, and a build with only the
# encoder fails AUDIO_PLAN.md section 4.2 the silent way: ffmpeg exits with
# "Decoder (codec opus) not found" and the host simply never hears the player.
--enable-decoder=rawvideo,pcm_s16le,wrapped_avframe,libopus
# NOTE the demuxer is pcm_s16le, NOT s16le. `ffmpeg -demuxers` DISPLAYS it as
# "s16le" but configure's component is PCM_S16LE_DEMUXER, and configure accepts
# --enable-demuxer=s16le without a word of complaint and builds without it.
# That is a silent audio-only failure; it was caught by running the binary.
# The same trap applies to the pcm_s16le MUXER on the --enable-muxer line.
#
# rtp,sdp: AUDIO_PLAN.md section 4.2 feeds the sidecar's jitter-buffer output
# back in as `-protocol_whitelist file,udp,rtp -i <sdp file>`. rtp_demuxer
# selects sdp_demuxer which selects rtpdec, so `rtp` alone would drag both in,
# but both are named because the call site names both.
#
# ogg: AUDIO_PLAN.md section 9.5/9.6. The sidecar's turn recorder writes
# host.ogg/player.ogg with pion's oggwriter (section 9.4) and the end-of-turn
# remux reads them back with `-i host.ogg -i player.ogg` before amix -- so the
# ogg DEMUXER has to be in this ffmpeg even though nothing here ever writes an
# Ogg container (oggwriter is pure Go, no cgo, section 0.2). Without it the
# remux fails the section 3.4 way: the turn's .ogg files are left on disk and
# the finished .mp4 is simply missing tracks, or missing entirely if it was the
# only audio input.
--enable-demuxer=rawvideo,pcm_s16le,h264,rtp,sdp,ogg
--enable-muxer=h264,mp4,rtp,null,pcm_s16le
--enable-parser=h264
# opus: the ogg demuxer above hands ffmpeg a bare Opus stream inside the Ogg
# container, and the OPUS PARSER (not the libopus decoder, already enabled
# above) is what lets that stream be `-map`ped straight through with `-c copy`
# -- section 9.5's stem tracks 2 and 3 are copied, never decoded. libopus is
# only decoded on the path amix needs (track 1, "mix"). Confirmed against the
# pinned 8.1.2 source: OPUS_PARSER exists as its own component, separate from
# both the libopus encoder and decoder above.
--enable-parser=opus
--enable-bsf=h264_metadata

# --- filters -----------------------------------------------------------------
# lavfi is an INDEV, not a demuxer: --disable-devices on its own breaks Lethal
# Company's nvenc probe (-f lavfi -i color=...). Keep avdevice, drop every
# device except lavfi and the capture devices enabled below.
--enable-avdevice
--disable-devices
# dshow is the Windows/Wine microphone capture of AUDIO_PLAN.md section 3.2
# (`-f dshow -i audio="<micName>"`), and section 0.1 makes that the LINUX case
# too, because on Linux the capture process runs inside the Wine prefix and
# therefore uses ffmpeg.exe. On the native-Linux ELF dshow_indev_deps=IBaseFilter
# is unsatisfiable, so configure prints one "Disabled dshow_indev" warning and
# carries on -- deliberately harmless, not an error, and cheaper than a second
# platform branch for a component the Linux binary would never open anyway.
--enable-indev=lavfi,dshow
# amix mixes the host mic into the existing game-audio stream for Lethal Company
# and 7 Days (AUDIO_PLAN.md section 3.2). It has to happen as PCM inside ONE
# ffmpeg: section 0.3 shows two ffmpegs writing one SSRC produce an unplayable
# stream, not a degraded one.
--enable-filter=null,anull,scale,format,aformat,aresample,vflip,color,amix
--enable-swscale
--enable-swresample

# --- io ----------------------------------------------------------------------
# NOT --disable-network. The audio pipes read tcp:// and write rtp://, and
# rtp_protocol_select="udp_protocol" while tcp/udp both select "network".
--enable-network
--disable-protocols
# `fd` is NOT optional and NOT the same as `pipe`. Since ffmpeg 7.x a bare "-"
# on the command line resolves to fd:, not pipe:; every one of the video call
# sites is `-i -` and/or `... -`, so without fd they all die at startup with
# "Protocol not found / Did you mean file:fd:?". pipe stays for pipe:N callers.
--enable-protocol=fd,pipe,file,tcp,udp,rtp

# --- misc --------------------------------------------------------------------
--disable-hwaccels
FLAGS

  if [ "$platform" = linux ]; then
    cat <<'FLAGS'

# --- native Linux only: pulse/alsa audio devices ------------------------------
# 7 Days to Die is the one game that runs a native-Linux ffmpeg (build-linux.sh's
# header), so this block exists for it and for nothing else.
#
# --enable-libpulse and --enable-alsa are LIBRARY flags, not component flags,
# and both are fatal when the library is absent: configure:7359 uses
# require_pkg_config for libpulse, and alsa is in EXTERNAL_AUTODETECT_LIBRARY_LIST
# so --enable-alsa marks it "requested" and configure:8287 dies with
# "alsa requested but not found". Hence: linux only, never in the shared list.
#
# BUILD DEPENDENCY: these make the Linux ELF link libpulse.so.0 and
# libasound.so.2 at runtime, and need pulseaudio-libs-devel + alsa-lib-devel
# (Fedora/Nobara) present at build time. build-linux.sh preflights both.
--enable-libpulse
--enable-alsa
# indev pulse/alsa: AUDIO_PLAN.md section 3.2's native-Linux mic capture,
# `-f pulse -i <micName>` in place of the dshow branch. alsa is the fallback for
# a box with no PulseAudio/PipeWire-pulse socket.
--enable-indev=pulse,alsa
# outdev pulse: AUDIO_PLAN.md section 4.2. This is the ONLY platform where
# ffmpeg can play the remote player's voice itself -- section 0.4 establishes
# that this ffmpeg has no Windows audio outdev at all and cannot get one, which
# is why the Windows/Wine path decodes to PCM and lets Go do the playback.
# Deliberately no alsa outdev: the fallback for "no pulse" is the Go path, not
# a second ffmpeg device.
--enable-outdev=pulse
FLAGS
  fi
}

# Print flags one per line, comments and blanks stripped.
drokk_ffmpeg_flags_clean() {
  drokk_ffmpeg_flags "$@" | sed -e 's/#.*//' -e '/^[[:space:]]*$/d' -e 's/[[:space:]]*$//'
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  drokk_ffmpeg_flags_clean "$@"
fi
