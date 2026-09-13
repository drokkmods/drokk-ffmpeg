#!/usr/bin/env python3
"""Verify a drokk-ffmpeg binary by RUNNING it, not by reading its flags.

    python3 verify.py path/to/ffmpeg[.exe] [--outdir DIR]

Runs on Linux against the ELF and inside the MSYS2 shell on Windows against
the .exe -- ffmpeg.exe is a native binary, so only the *build* needs MSYS2;
this only needs a python and a loopback socket.

WHY IT EXISTS: the mods' failure modes are asymmetric. A missing video codec
is a loud hard failure, but Lethal Company and 7 Days both downgrade a dead
audio ffmpeg to a Warning ("continuing video-only"), so an ffmpeg built
without libopus, tcp or rtp ships a silent regression that no flag audit and
no `-encoders` grep would catch. Hence: every one of the seven real call
sites is replayed here, byte-for-byte from the mods' source, against
synthetic input.

The four AUDIO_PLAN.md section 6 rows are checked the same way and for the
same reason -- section 3.4 makes mic failure explicitly non-fatal, so a build
without dshow/amix/libopus-decoder produces a session that connects, shows
video, and is simply mute in one direction.

Exit status 0 = every check that can run on this machine passed. nvenc call
sites are reported separately as HWSKIP when the encoder is present in the
binary but no NVIDIA device is available -- that is a property of the test
machine, not of the build, and the two are never conflated. The audio-device
checks use HWSKIP the same way: "compiled in, but this box has no microphone /
no PulseAudio server" must never read as a build defect.

WHICH BUILD AM I LOOKING AT: decided from the binary's own magic bytes (MZ vs
\x7fELF), NOT from sys.platform. The win64 .exe is routinely verified from a
Linux-hosted MSYS2 shell and the question being asked -- "does THIS BINARY have
dshow" -- is about the build, never about the box.
"""

import argparse, os, re, socket, struct, subprocess, sys, threading, time

RESET = "\033[0m" if sys.stdout.isatty() else ""
RED   = "\033[31m" if sys.stdout.isatty() else ""
GRN   = "\033[32m" if sys.stdout.isatty() else ""
YEL   = "\033[33m" if sys.stdout.isatty() else ""

results = []          # (status, name, detail)

def record(status, name, detail=""):
    color = {"PASS": GRN, "FAIL": RED, "HWSKIP": YEL, "SKIP": YEL}[status]
    print(f"  {color}{status:6}{RESET} {name}" + (f"  -- {detail}" if detail else ""))
    results.append((status, name, detail))

# --- the synthetic inputs ----------------------------------------------------

W, H, FPS, NFRAMES = 320, 240, 30, 30
SAMPLE_RATE, CHANNELS, AUDIO_SECONDS = 48000, 2, 2

def raw_frames(pix_fmt):
    """NFRAMES of a moving gradient. bgra and rgba differ only in byte order,
    which is exactly the difference between SH2 (bgra) and LC/7D (rgba).

    The pattern MUST be vertically asymmetric (note the y term, and the bright
    bar in the top eighth). A vertically uniform pattern makes -vf vflip a
    no-op, and the first version of this file had exactly that bug: the vflip
    case "passed" while proving nothing."""
    out = bytearray()
    for f in range(NFRAMES):
        frame = bytearray()
        for y in range(H):
            top = y < H // 8
            for x in range(W):
                r = 255 if top else (x + f * 4) % 256
                g = (y * 3) % 256
                b = (f * 8 + y) % 256
                frame += bytes((b, g, r, 255) if pix_fmt == "bgra" else (r, g, b, 255))
        out += bytes(frame)
    return bytes(out)

def pcm_s16le():
    """A 440 Hz stereo tone, 48 kHz s16le -- PuppetAudioFormat's exact format."""
    import math
    n = SAMPLE_RATE * AUDIO_SECONDS
    buf = bytearray()
    for i in range(n):
        v = int(20000 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))
        buf += struct.pack("<hh", v, v)
    return bytes(buf)

def quiet_tone(seconds=0.25, amp=1200):
    """A deliberately SHORT and QUIET 440 Hz tone for the pulse outdev check.
    That check really does push sound at the machine running it; 20000/32767
    (pcm_s16le() above) out of a build box's speakers at 3am is not acceptable
    collateral for a verification run."""
    import math
    buf = bytearray()
    for i in range(int(SAMPLE_RATE * seconds)):
        v = int(amp * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))
        buf += struct.pack("<hh", v, v)
    return bytes(buf)

def is_windows_build(ff):
    """PE ('MZ') vs ELF -- a property of the BINARY, not of this machine."""
    with open(ff, "rb") as f:
        return f.read(2) == b"MZ"

def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

# --- running the binary ------------------------------------------------------

def run(ff, args, stdin=None, timeout=120):
    p = subprocess.run([ff] + args, input=stdin,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout)
    return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")

# "the GPU/driver on THIS machine will not do it" -- says nothing about the build.
NO_HARDWARE = re.compile(
    r"cannot load|nvEncodeAPI|no NVIDIA|no capable devices|"
    r"does not support the required nvenc API|minimum required Nvidia driver|"
    r"OpenEncodeSessionEx failed|Cannot init CUDA|CUDA_ERROR|"
    r"Frame Dimension less than the minimum|device.*not.*found",
    re.I)
NOT_BUILT = re.compile(r"unknown encoder|unknown filter|unknown bitstream filter|"
                       r"unknown muxer|unknown demuxer|not found|"
                       r"protocol not found|invalid argument", re.I)

def classify_nvenc_failure(err):
    """Distinguish 'this build lacks nvenc' -- a real defect we must fix -- from
    'this machine's GPU/driver refused' -- a property of the test box. Never
    conflate them: the first must fail the build, the second must not."""
    if re.search(r"unknown encoder|encoder not found|Unrecognized option", err, re.I):
        return "FAIL", "h264_nvenc is NOT COMPILED IN"
    if re.search(r"no decoder found for|unknown filter|unknown demuxer|"
                 r"protocol not found|unknown bitstream filter", err, re.I):
        line = next(l for l in err.splitlines()
                    if re.search(r"no decoder found for|unknown|protocol not found", l, re.I))
        return "FAIL", "missing component, not a hardware problem: " + line.strip()[:140]
    if NO_HARDWARE.search(err):
        line = next(l for l in err.splitlines() if NO_HARDWARE.search(l))
        return "HWSKIP", "compiled in; this machine's GPU/driver refused: " + line.strip()[:150]
    return "FAIL", err.strip().splitlines()[-1][:160] if err.strip() else "no stderr"

# --- static component checks -------------------------------------------------

# NOTE the naming split, which is the pcm_s16le-vs-s16le trap again and bites in
# BOTH directions here: -encoders/-decoders print CODEC names (pcm_s16le,
# libopus) while -muxers/-demuxers print FORMAT names (s16le). configure wants
# pcm_s16le for all four. Getting it wrong in this table makes the check pass
# against a binary that is missing the component.
STATIC = [
    ("-encoders",  [r"\blibx264\b", r"\bh264_nvenc\b", r"\blibopus\b", r"\bpcm_s16le\b"]),
    ("-decoders",  [r"\brawvideo\b", r"\bpcm_s16le\b", r"\blibopus\b"]),
    ("-demuxers",  [r"\brawvideo\b", r"\bs16le\b", r"\bh264\b", r"\brtp\b", r"\bsdp\b"]),
    ("-muxers",    [r"\bh264\b", r"\bmp4\b", r"\brtp\b", r"\bnull\b", r"\bs16le\b"]),
    ("-devices",   [r"\blavfi\b"]),
    ("-bsfs",      [r"\bh264_metadata\b"]),
    ("-protocols", [r"\bpipe\b", r"\bfile\b", r"\btcp\b", r"\budp\b", r"\brtp\b"]),
    ("-filters",   [r"\bscale\b", r"\bformat\b", r"\baformat\b", r"\baresample\b",
                    r"\bvflip\b", r"\bcolor\b", r"\bnull\b", r"\banull\b", r"\bamix\b"]),
]

# The capture/playback devices are the ONE place the two builds legitimately
# differ (configure-flags.sh's PLATFORM ARGUMENT note): dshow cannot exist in an
# ELF and pulse/alsa cannot exist in a mingw .exe. Keyed off the binary, so
# neither list is ever checked against the wrong build.
STATIC_WIN   = [("-devices", [r"\bdshow\b"])]
STATIC_LINUX = [("-devices", [r"\bpulse\b", r"\balsa\b"])]

def static_checks(ff, win):
    print("\n== static component checks ==")
    for flag, pats in STATIC + (STATIC_WIN if win else STATIC_LINUX):
        rc, out, err = run(ff, ["-hide_banner", flag])
        text = out.decode("utf-8", "replace") + err
        missing = [p for p in pats if not re.search(p, text)]
        if missing:
            record("FAIL", f"{flag}", "missing: " + ", ".join(m.strip('\\b') for m in missing))
        else:
            record("PASS", f"{flag}", f"{len(pats)} entries present")

def licence_check(ff):
    rc, out, err = run(ff, ["-hide_banner", "-version"])
    text = out.decode("utf-8", "replace")
    if "--enable-version3" in text:
        record("FAIL", "licence", "built with --enable-version3 (would be GPLv3)")
    elif "--enable-nonfree" in text:
        record("FAIL", "licence", "built with --enable-nonfree (undistributable)")
    elif "--enable-gpl" in text:
        record("PASS", "licence", "GPLv2-or-later (gpl, no version3, no nonfree)")
    else:
        record("FAIL", "licence", "no --enable-gpl; libx264 cannot be present")

# --- the seven real call sites ----------------------------------------------

def annexb_has_aud(buf):
    """h264_metadata=aud=insert must produce access-unit delimiters (NAL 9);
    host/internal/annexb/split.go and PuppetPipe.cs both cut on them."""
    for sc in (b"\x00\x00\x00\x01", b"\x00\x00\x01"):
        i = 0
        while True:
            i = buf.find(sc, i)
            if i < 0:
                break
            nal = buf[i + len(sc)]
            if (nal & 0x1f) == 9:
                return True
            i += len(sc)
    return False

VIDEO_TAIL = ["-profile:v", "baseline", "-level", "3.1",
              "-b:v", "3500k", "-maxrate", "5000k", "-bufsize", "3500k",
              "-bf", "0", "-g", "30", "-forced-idr", "1", "-pix_fmt", "yuv420p",
              "-bsf:v", "h264_metadata=aud=insert", "-f", "h264", "-"]

def video_case(ff, name, pix_fmt, encoder_args, extra_in=(), outdir=".", nvenc=False):
    args = (["-hide_banner", "-loglevel", "warning",
             "-f", "rawvideo", "-pix_fmt", pix_fmt, "-s", f"{W}x{H}",
             "-r", str(FPS), "-i", "-"] + list(extra_in)
            + encoder_args + VIDEO_TAIL)
    rc, out, err = run(ff, args, stdin=raw_frames(pix_fmt))
    if rc != 0 or not out:
        if nvenc:
            return record(*((lambda s, d: (s, name, d))(*classify_nvenc_failure(err))))
        return record("FAIL", name, (err.strip().splitlines() or ["no output"])[-1][:160])
    path = os.path.join(outdir, name.replace("/", "_") + ".h264")
    open(path, "wb").write(out)
    if not annexb_has_aud(out):
        return record("FAIL", name, "no AUD NALs -- h264_metadata=aud=insert did nothing")
    record("PASS", name, f"{len(out)} bytes Annex-B, AUDs present -> {path}")
    return path

def case_V3_nvenc_probe(ff):
    """Lethal Company Transport/VideoPipe.cs:291 ProbeNvenc(), verbatim."""
    args = ["-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1:r=2",
            "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll",
            "-profile:v", "baseline", "-level", "3.1", "-rc", "vbr",
            "-b:v", "500k", "-bf", "0", "-g", "2", "-forced-idr", "1",
            "-pix_fmt", "yuv420p", "-f", "h264", "-f", "null", "-"]
    rc, out, err = run(ff, args)
    if rc == 0:
        return record("PASS", "V3 LC nvenc probe (lavfi+color -> null)", "probe reports nvenc usable")
    if re.search(r"unknown|no such|not found", err, re.I) and re.search(r"lavfi|color", err, re.I):
        return record("FAIL", "V3 LC nvenc probe (lavfi+color -> null)",
                      "lavfi indev or color filter MISSING: " + err.strip().splitlines()[-1][:120])
    s, d = classify_nvenc_failure(err)
    record(s, "V3 LC nvenc probe (lavfi+color -> null)", d)

def case_V5_remux(ff, h264_path, outdir):
    """7 Days PuppetPipe.cs:453 RemuxAsync(): .h264 -> .mp4 with -c copy."""
    if not h264_path:
        return record("SKIP", "V5 7D remux (.h264 -> .mp4, -c copy)", "no upstream .h264 to remux")
    mp4 = os.path.splitext(h264_path)[0] + ".mp4"
    rc, out, err = run(ff, ["-hide_banner", "-loglevel", "warning", "-y",
                            "-r", str(FPS), "-i", h264_path, "-c", "copy", mp4])
    if rc != 0 or not os.path.exists(mp4):
        return record("FAIL", "V5 7D remux (.h264 -> .mp4, -c copy)",
                      (err.strip().splitlines() or ["no file"])[-1][:160])
    head = open(mp4, "rb").read(12)
    if head[4:8] != b"ftyp":
        return record("FAIL", "V5 7D remux (.h264 -> .mp4, -c copy)", "output is not ISO-BMFF")
    record("PASS", "V5 7D remux (.h264 -> .mp4, -c copy)",
           f"{os.path.getsize(mp4)} bytes, ftyp box ok -> {mp4}")

def case_audio(ff, name, outdir):
    """PuppetAudio.cs:177 / AudioPipe.cs:73 -- byte-identical in both mods.
    ffmpeg is the TCP *client*, so the listener must be up first; the RTP
    output is real UDP datagrams we count on the wire."""
    pcm = pcm_s16le()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0)); srv.listen(1); srv.settimeout(20)
    send_port = srv.getsockname()[1]

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0)); udp.settimeout(0.5)
    udp_port = udp.getsockname()[1]

    served = {"n": 0, "err": None}
    def serve():
        try:
            c, _ = srv.accept()
            c.sendall(pcm); served["n"] = len(pcm)
            time.sleep(0.3); c.close()
        except Exception as e:                       # noqa: BLE001
            served["err"] = repr(e)
    t = threading.Thread(target=serve, daemon=True); t.start()

    packets = []
    stop = threading.Event()
    def recv():
        while not stop.is_set():
            try:
                packets.append(udp.recv(4096))
            except socket.timeout:
                pass
            except OSError:
                return
    r = threading.Thread(target=recv, daemon=True); r.start()

    args = ["-hide_banner", "-loglevel", "warning",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-i", f"tcp://127.0.0.1:{send_port}",
            "-c:a", "libopus", "-b:a", "64k", "-application", "lowdelay",
            "-frame_duration", "20",
            "-f", "rtp", f"rtp://127.0.0.1:{udp_port}"]
    try:
        rc, out, err = run(ff, args, timeout=60)
    finally:
        stop.set(); time.sleep(0.8); r.join(timeout=2)
        try: udp.close()
        except OSError: pass
        try: srv.close()
        except OSError: pass

    sdp = out.decode("utf-8", "replace")
    if rc != 0:
        return record("FAIL", name, (err.strip().splitlines() or ["rc=%d" % rc])[-1][:160])
    if not packets:
        return record("FAIL", name, "ffmpeg exited 0 but NO RTP datagrams arrived")
    if "m=audio" not in sdp or "opus/48000/2" not in sdp:
        return record("FAIL", name, "SDP missing m=audio/opus: " + sdp.replace("\n", " ")[:140])
    # RTP header: version must be 2, payload type dynamic (96..127)
    pt = packets[0][1] & 0x7f
    if (packets[0][0] >> 6) != 2:
        return record("FAIL", name, "first datagram is not RTP v2")
    open(os.path.join(outdir, "audio.sdp"), "w").write(sdp)
    record("PASS", name,
           f"{len(packets)} RTP datagrams (pt={pt}) from {served['n']} bytes s16le; SDP ok")

# --- the four AUDIO_PLAN.md section 6 rows -----------------------------------
#
# One check per row of that table, and each one runs the real call site rather
# than grepping a list, for the same reason as the seven above: section 3.4
# makes an unopenable microphone a warning, so every missing component here
# lands as "the session works and nobody can hear anyone".
#
# The HWSKIP/FAIL split is the nvenc split exactly: "not compiled in" must fail
# the build, "this box has no microphone / no PulseAudio server" must not.

NO_AUDIO_HARDWARE = re.compile(
    r"Could not connect to pulse|Connection refused|connection refused|"
    r"No such file or directory.*pulse|Failed to connect|"
    r"Could not enumerate audio only devices|Could not find audio only device|"
    r"No such device|cannot open audio device|Device or resource busy|"
    r"I/O error|Input/output error|No default device",
    re.I)
NOT_COMPILED = re.compile(
    r"Unknown input format|Unknown output format|Unknown decoder|Unknown encoder|"
    r"Unknown filter|No such filter|Requested output format .* is not|"
    r"Protocol not found|Decoder .* not found|Unrecognized option",
    re.I)

def classify_device_failure(component, err):
    """Same contract as classify_nvenc_failure, for audio devices."""
    if NOT_COMPILED.search(err):
        line = next(l for l in err.splitlines() if NOT_COMPILED.search(l))
        return "FAIL", f"{component} is NOT COMPILED IN: " + line.strip()[:140]
    if NO_AUDIO_HARDWARE.search(err):
        line = next(l for l in err.splitlines() if NO_AUDIO_HARDWARE.search(l))
        return "HWSKIP", "compiled in; this machine has no usable device: " + line.strip()[:140]
    return "FAIL", (err.strip().splitlines() or ["no stderr"])[-1][:160]

def serve_pcm_over_tcp(pcm):
    """ffmpeg is the TCP *client* at every s16le call site, so the listener has
    to be up first. Returns the port; the socket is served once and closed."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0)); srv.listen(1); srv.settimeout(20)
    def serve():
        try:
            c, _ = srv.accept()
            c.sendall(pcm)
            time.sleep(0.3); c.close()
        except Exception:                                # noqa: BLE001
            pass
        finally:
            try: srv.close()
            except OSError: pass
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]

# --- row 1: --enable-indev=dshow --------------------------------------------

def case_D1_dshow(ff, win, outdir):
    """AUDIO_PLAN.md section 3.2 (Windows/Wine mic) and 5.2 (the app's device
    picker, `-list_devices true -f dshow -i dummy`).

    Section 0.1: on Linux the capture process runs INSIDE the Wine prefix and so
    runs ffmpeg.exe, which is why this is checked against the .exe only and why
    the ELF not having dshow is correct rather than a gap."""
    name = "D1 dshow mic capture (-list_devices, then a real open)"
    if not win:
        return record("SKIP", name,
                      "native ELF: dshow is Windows/Wine only (section 0.1) and "
                      "configure-flags.sh expects it dropped here")

    rc, out, err = run(ff, ["-hide_banner", "-list_devices", "true",
                            "-f", "dshow", "-i", "dummy"], timeout=60)
    # -list_devices ALWAYS exits non-zero ("Immediate exit requested"); the
    # device list is on stderr. rc says nothing, the text says everything.
    if NOT_COMPILED.search(err):
        return record("FAIL", name, "dshow indev is NOT COMPILED IN: "
                      + next(l for l in err.splitlines() if NOT_COMPILED.search(l)).strip()[:140])
    # Two listing formats: older ffmpeg prints a "DirectShow audio devices"
    # header followed by quoted names; ffmpeg >= 5 prints one
    # `"Name" (audio)` / `"Name" (video)` line per device and no header.
    tagged = re.findall(r'"([^"]+)"\s+\((audio|video|none)\)', err)
    if "DirectShow audio devices" not in err and not tagged:
        return record("FAIL", name,
                      "-list_devices produced no DirectShow device list: "
                      + err.strip().replace("\n", " ")[:150])
    open(os.path.join(outdir, "dshow-devices.txt"), "w").write(err)

    if tagged:
        names = [n for n, kind in tagged if kind == "audio"]
    else:
        tail = err.split("DirectShow audio devices", 1)[1]
        names = [m for m in re.findall(r'"([^"]+)"', tail)
                 if not m.startswith("@device_")]
    if not names:
        return record("HWSKIP", name,
                      "dshow enumerated fine; this machine has no audio capture "
                      "device to open -> dshow-devices.txt")

    # "A synthetic capture opens": section 3.2's argv, truncated to 0.5s and
    # dumped to null. Opening a real mic is the only way to prove the indev
    # actually works -- enumeration alone passes on a half-built dshow.
    mic = names[0]
    rc, out, err = run(ff, ["-hide_banner", "-loglevel", "warning",
                            "-f", "dshow", "-i", f"audio={mic}",
                            "-t", "0.5",
                            "-c:a", "libopus", "-b:a", "64k",
                            "-application", "lowdelay", "-frame_duration", "20",
                            "-f", "null", "-"], timeout=60)
    if rc != 0:
        s, d = classify_device_failure("dshow indev", err)
        return record(s, name, f'device "{mic[:40]}": {d}')
    record("PASS", name, f'opened "{mic[:60]}" and encoded 0.5s to libopus')

# --- row 2: --enable-indev=pulse,alsa + --enable-outdev=pulse ----------------

def case_D2_pulse_alsa(ff, win):
    """AUDIO_PLAN.md section 3.2 native-Linux capture (`-f pulse -i <micName>`),
    section 5.2's `-sources pulse`, and section 4.2's playback leg.

    Section 0.4 is why the outdev half matters so much: native Linux is the ONLY
    target where ffmpeg can play the remote player's voice itself. Everywhere
    else ffmpeg decodes and Go does the playback, because no Windows outdev
    exists in this ffmpeg and none can be configured in."""
    name_in  = "D2a pulse/alsa indev (-sources, section 5.2's own call site)"
    name_out = "D2b pulse outdev (a synthetic tone actually plays)"
    if win:
        record("SKIP", name_in,  "win64 build: pulse/alsa are native-Linux only")
        record("SKIP", name_out, "win64 build: section 0.4 -- there is no Windows outdev")
        return

    # THE TRAP HERE, confirmed against the pre-A1 ELF: `ffmpeg -sources <name>`
    # for a device that is NOT COMPILED IN prints NOTHING and exits 0. So does
    # `-sources bogus`. rc is worthless; the only honest signal is the
    # "Auto-detected sources for <name>:" header, which ffmpeg emits only after
    # it has actually resolved the device. Never relax this to an rc check.
    for dev in ("pulse", "alsa"):
        rc, out, err = run(ff, ["-hide_banner", "-sources", dev], timeout=30)
        text = out.decode("utf-8", "replace") + err
        if f"Auto-detected sources for {dev}" not in text:
            record("FAIL", f"{name_in} [{dev}]",
                   f"{dev} indev is NOT COMPILED IN (no header; ffmpeg exits 0 either way)")
            continue
        body = text.split(f"Auto-detected sources for {dev}", 1)[1]
        if re.search(r"Function not implemented", body, re.I):
            record("PASS", f"{name_in} [{dev}]",
                   "indev registered; this one does not implement enumeration")
            continue
        if NO_AUDIO_HARDWARE.search(body) or rc != 0:
            st, d = classify_device_failure(f"{dev} indev", body)
            record(st, f"{name_in} [{dev}]", d)
            continue
        n = len([l for l in body.splitlines() if l.strip()])
        record("PASS", f"{name_in} [{dev}]", f"enumerated, {n} line(s) of devices")

    # Playback. quiet_tone() not pcm_s16le(): see its docstring.
    rc, out, err = run(ff, ["-hide_banner", "-loglevel", "warning",
                            "-f", "s16le", "-ar", str(SAMPLE_RATE),
                            "-ac", str(CHANNELS), "-i", "-",
                            "-f", "pulse", "drokk-ffmpeg verify"],
                       stdin=quiet_tone(), timeout=60)
    if rc != 0:
        s, d = classify_device_failure("pulse outdev", err)
        return record(s, name_out, d)
    record("PASS", name_out, "0.25s tone played through the pulse outdev")

# --- row 3: --enable-filter=amix --------------------------------------------

def case_D3_amix(ff, outdir):
    """AUDIO_PLAN.md section 3.2's LC/7 Days invocation, with the dshow input
    replaced by a second s16le input -- amix does not care where its inputs came
    from, and a real microphone cannot be assumed on a build box.

    Section 0.3 is the reason this must work in ONE ffmpeg: two processes both
    writing type-3 frames onto one SSRC interleave two RTP sequence spaces and
    produce an unplayable stream, not a quiet one."""
    name = "D3 amix: two s16le inputs -> one libopus RTP stream"

    game = pcm_s16le()                       # 440 Hz, the game-audio leg
    mic_path = os.path.join(outdir, "amix-mic.s16le")
    open(mic_path, "wb").write(quiet_tone(seconds=AUDIO_SECONDS, amp=6000))

    # (a) the real thing: mixed -> libopus -> RTP on the wire.
    port = serve_pcm_over_tcp(game)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0)); udp.settimeout(0.5)
    udp_port = udp.getsockname()[1]
    packets, stop = [], threading.Event()
    def recv():
        while not stop.is_set():
            try: packets.append(udp.recv(4096))
            except socket.timeout: pass
            except OSError: return
    r = threading.Thread(target=recv, daemon=True); r.start()

    args = ["-hide_banner", "-loglevel", "warning",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-i", f"tcp://127.0.0.1:{port}",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-i", mic_path,
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[a]",
            "-map", "[a]",
            "-c:a", "libopus", "-b:a", "64k", "-application", "lowdelay",
            "-frame_duration", "20",
            "-f", "rtp", f"rtp://127.0.0.1:{udp_port}"]
    try:
        rc, out, err = run(ff, args, timeout=60)
    finally:
        stop.set(); time.sleep(0.8); r.join(timeout=2)
        try: udp.close()
        except OSError: pass

    if rc != 0:
        return record("FAIL", name, (err.strip().splitlines() or ["rc=%d" % rc])[-1][:160])
    sdp = out.decode("utf-8", "replace")
    if not packets:
        return record("FAIL", name, "ffmpeg exited 0 but NO RTP datagrams arrived")
    if "m=audio" not in sdp or "opus/48000/2" not in sdp:
        return record("FAIL", name, "SDP missing m=audio/opus: " + sdp.replace("\n", " ")[:140])

    # (b) "it ran" is not "it mixed" -- the V4x-vflip lesson. Render the same
    # graph to raw PCM and compare against input 0 alone: a build where amix
    # quietly degenerates to a passthrough of the first input would sail through
    # (a) above.
    mixed = os.path.join(outdir, "amix-mixed.s16le")
    solo  = os.path.join(outdir, "amix-solo.s16le")
    p2 = serve_pcm_over_tcp(game)
    rc2, _, e2 = run(ff, ["-hide_banner", "-loglevel", "warning", "-y",
                          "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                          "-i", f"tcp://127.0.0.1:{p2}",
                          "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                          "-i", mic_path,
                          "-filter_complex",
                          "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[a]",
                          "-map", "[a]", "-f", "s16le", mixed], timeout=60)
    p3 = serve_pcm_over_tcp(game)
    rc3, _, e3 = run(ff, ["-hide_banner", "-loglevel", "warning", "-y",
                          "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                          "-i", f"tcp://127.0.0.1:{p3}",
                          "-f", "s16le", solo], timeout=60)
    if rc2 != 0 or rc3 != 0:
        return record("FAIL", name, "RTP leg ok but the raw-PCM control run failed: "
                      + ((e2 or e3).strip().splitlines() or ["?"])[-1][:130])
    if open(mixed, "rb").read() == open(solo, "rb").read():
        return record("FAIL", name,
                      "amix output is byte-identical to input 0 alone -- it did not mix")
    record("PASS", name,
           f"{len(packets)} RTP datagrams, SDP ok, and the mix differs from input 0 alone")

# --- row 4: --enable-decoder=libopus + --enable-demuxer=sdp,rtp --------------

def case_D4_rtp_decode(ff, outdir):
    """AUDIO_PLAN.md section 4.2's sidecar leg, verbatim:
        -protocol_whitelist file,udp,rtp -i <sdp file> -f s16le -ar 48000 -ac 2 -
    This is the direction that does not exist today at all, so nothing else in
    the tree would notice these components missing.

    Three components have to line up: the sdp demuxer to read the file, the rtp
    demuxer to read the socket, and the libopus DECODER -- which the pre-A1
    builds did not have, having enabled only the libopus encoder."""
    name = "D4 SDP/RTP -> libopus decode -> s16le"

    # An SDP has to name a port before anything is listening on it, so: run the
    # section 3.2 sender once to a chosen port purely to capture the SDP text it
    # prints, then bring the receiver up on that SDP and run the sender again.
    port = free_udp_port()
    def send():
        p = serve_pcm_over_tcp(pcm_s16le())
        return run(ff, ["-hide_banner", "-loglevel", "warning",
                        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                        "-i", f"tcp://127.0.0.1:{p}",
                        "-c:a", "libopus", "-b:a", "64k",
                        "-application", "lowdelay", "-frame_duration", "20",
                        "-f", "rtp", f"rtp://127.0.0.1:{port}"], timeout=60)

    rc, out, err = send()
    sdp_text = out.decode("utf-8", "replace")
    if rc != 0 or "m=audio" not in sdp_text:
        return record("FAIL", name, "could not produce an SDP to decode: "
                      + (err.strip().splitlines() or ["rc=%d" % rc])[-1][:140])
    sdp_path = os.path.join(outdir, "inbound.sdp")
    open(sdp_path, "w").write(sdp_text)
    pcm_path = os.path.join(outdir, "inbound.s16le")

    recv = subprocess.Popen(
        [ff, "-hide_banner", "-loglevel", "warning", "-y",
         "-protocol_whitelist", "file,udp,rtp", "-i", sdp_path,
         "-t", "1", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
         pcm_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(1.0)                      # let the demuxer bind before we send
    if recv.poll() is not None:          # died on startup == missing component
        e = recv.stderr.read().decode("utf-8", "replace")
        s, d = classify_device_failure("sdp/rtp demuxer or libopus decoder", e)
        return record(s, name, d)

    send()
    try:
        _, rerr = recv.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        recv.kill(); _, rerr = recv.communicate()
    rerr = rerr.decode("utf-8", "replace")

    if not os.path.exists(pcm_path) or os.path.getsize(pcm_path) == 0:
        s, d = classify_device_failure("sdp/rtp demuxer or libopus decoder", rerr)
        return record(s, name, d)
    data = open(pcm_path, "rb").read()
    # Decoded, not merely muxed: silence would mean the opus frames never made
    # it through the decoder.
    peak = max(abs(v) for v, in struct.iter_unpack("<h", data[:len(data) // 2 * 2]))
    if peak < 500:
        return record("FAIL", name,
                      f"{len(data)} bytes of s16le but peak amplitude {peak} -- decoded silence")
    record("PASS", name,
           f"{len(data)} bytes s16le, peak {peak}, from an RTP/SDP opus stream -> {pcm_path}")

# --- main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ffmpeg")
    ap.add_argument("--outdir", default="verify-out")
    a = ap.parse_args()
    ff = os.path.abspath(a.ffmpeg)
    os.makedirs(a.outdir, exist_ok=True)

    rc, out, err = run(ff, ["-hide_banner", "-version"])
    if rc != 0:
        print(f"{RED}cannot execute {ff}{RESET}"); return 2
    print((out.decode("utf-8", "replace").splitlines() or [""])[0])

    win = is_windows_build(ff)
    print(f"build flavour: {'win64 PE (.exe)' if win else 'native Linux ELF'} "
          "-- decided from the binary's magic bytes")
    static_checks(ff, win)
    licence_check(ff)

    print("\n== the seven real call sites ==")
    v1 = video_case(ff, "V1 SH2 bgra libx264", "bgra",
                    ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"],
                    outdir=a.outdir)
    video_case(ff, "V2a LC rgba libx264 fallback", "rgba",
               ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"],
               outdir=a.outdir)
    video_case(ff, "V2b LC rgba h264_nvenc", "rgba",
               ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll", "-rc", "vbr"],
               outdir=a.outdir, nvenc=True)
    case_V3_nvenc_probe(ff)
    video_case(ff, "V4 7D rgba h264_nvenc +vflip", "rgba",
               ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll", "-rc", "vbr"],
               extra_in=["-vf", "vflip"], outdir=a.outdir, nvenc=True)
    case_V5_remux(ff, v1, a.outdir)
    case_audio(ff, "A1 7D s16le/tcp -> libopus/rtp", a.outdir)
    case_audio(ff, "A2 LC s16le/tcp -> libopus/rtp", a.outdir)

    # -vf vflip must also work on a path that does not need a GPU, otherwise a
    # HWSKIP on V4 would hide a missing vflip filter. And "it ran" is not
    # "it flipped": compare against the identical run without -vf.
    v4x = video_case(ff, "V4x vflip on libx264 (vflip coverage without a GPU)", "rgba",
                     ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"],
                     extra_in=["-vf", "vflip"], outdir=a.outdir)
    v2a = os.path.join(a.outdir, "V2a LC rgba libx264 fallback.h264")
    if v4x and os.path.exists(v2a):
        if open(v4x, "rb").read() == open(v2a, "rb").read():
            record("FAIL", "V4x vflip actually flipped",
                   "vflip output is byte-identical to the un-flipped run")
        else:
            record("PASS", "V4x vflip actually flipped",
                   "output differs from the un-flipped run")

    print("\n== AUDIO_PLAN.md section 6: the four audio-path rows ==")
    case_D1_dshow(ff, win, a.outdir)
    case_D2_pulse_alsa(ff, win)
    case_D3_amix(ff, a.outdir)
    case_D4_rtp_decode(ff, a.outdir)

    n_fail = sum(1 for s, _, _ in results if s == "FAIL")
    n_hw   = sum(1 for s, _, _ in results if s == "HWSKIP")
    n_pass = sum(1 for s, _, _ in results if s == "PASS")
    n_skip = sum(1 for s, _, _ in results if s == "SKIP")
    print(f"\n{n_pass} passed, {n_fail} failed, {n_hw} skipped for lack of hardware, "
          f"{n_skip} not applicable to this build")
    if n_hw:
        print("  (HWSKIP means the component IS in the binary and failed only at "
              "device-open time -- no GPU, no microphone, no PulseAudio server. "
              "A property of this machine, not of the build.)")
    if n_skip:
        print("  (SKIP means the check does not apply to this build flavour at all, "
              "e.g. dshow against the ELF or pulse against the .exe.)")
    return 1 if n_fail else 0

if __name__ == "__main__":
    sys.exit(main())
