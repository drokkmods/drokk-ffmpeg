#!/usr/bin/env bash
# The shared ffmpeg ./configure flag list. Sourced by build-linux.sh and
# build-win64.sh; also runnable on its own (`./configure-flags.sh`) to print the
# flags one per line.
#
# METHOD: start from --disable-everything and add back ONLY what the mods'
# actual ffmpeg command lines prove they need. Every flag below is traceable to
# a specific invocation; the seven of them are enumerated in README.md. Do not
# add a flag without a call site, and do not remove one without checking all
# seven.
#
# The single most important thing to know about this list: a WRONG list mostly
# fails SILENTLY. Both Lethal Company and 7 Days treat a dead audio ffmpeg as a
# warning and continue video-only, so an ffmpeg missing libopus, tcp or rtp
# produces a session that looks fine and has no sound. That is why the build
# scripts run every call site's real argv against synthetic input instead of
# grepping `-encoders` and calling it done.

set -euo pipefail

drokk_ffmpeg_flags() {
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
--enable-encoder=libx264,h264_nvenc,libopus
# wrapped_avframe is NOT optional: the lavfi indev hands frames to the ffmpeg
# CLI as wrapped_avframe packets, so Lethal Company's nvenc probe
# (-f lavfi -i color=...) dies with "no decoder found for: wrapped_avframe"
# without it. Another one only a real run finds.
--enable-decoder=rawvideo,pcm_s16le,wrapped_avframe
# NOTE the demuxer is pcm_s16le, NOT s16le. `ffmpeg -demuxers` DISPLAYS it as
# "s16le" but configure's component is PCM_S16LE_DEMUXER, and configure accepts
# --enable-demuxer=s16le without a word of complaint and builds without it.
# That is a silent audio-only failure; it was caught by running the binary.
--enable-demuxer=rawvideo,pcm_s16le,h264
--enable-muxer=h264,mp4,rtp,null
--enable-parser=h264
--enable-bsf=h264_metadata

# --- filters -----------------------------------------------------------------
# lavfi is an INDEV, not a demuxer: --disable-devices on its own breaks Lethal
# Company's nvenc probe (-f lavfi -i color=...). Keep avdevice, drop every
# device except lavfi.
--enable-avdevice
--disable-devices
--enable-indev=lavfi
--enable-filter=null,anull,scale,format,aformat,aresample,vflip,color
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
}

# Print flags one per line, comments and blanks stripped.
drokk_ffmpeg_flags_clean() {
  drokk_ffmpeg_flags | sed -e 's/#.*//' -e '/^[[:space:]]*$/d' -e 's/[[:space:]]*$//'
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  drokk_ffmpeg_flags_clean
fi
