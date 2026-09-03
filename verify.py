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

Exit status 0 = every check that can run on this machine passed. nvenc call
sites are reported separately as HWSKIP when the encoder is present in the
binary but no NVIDIA device is available -- that is a property of the test
machine, not of the build, and the two are never conflated.
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

STATIC = [
    ("-encoders",  [r"\blibx264\b", r"\bh264_nvenc\b", r"\blibopus\b"]),
    ("-decoders",  [r"\brawvideo\b", r"\bpcm_s16le\b"]),
    ("-demuxers",  [r"\brawvideo\b", r"\bs16le\b", r"\bh264\b"]),
    ("-muxers",    [r"\bh264\b", r"\bmp4\b", r"\brtp\b", r"\bnull\b"]),
    ("-devices",   [r"\blavfi\b"]),
    ("-bsfs",      [r"\bh264_metadata\b"]),
    ("-protocols", [r"\bpipe\b", r"\bfile\b", r"\btcp\b", r"\budp\b", r"\brtp\b"]),
    ("-filters",   [r"\bscale\b", r"\bformat\b", r"\baformat\b", r"\baresample\b",
                    r"\bvflip\b", r"\bcolor\b", r"\bnull\b", r"\banull\b"]),
]

def static_checks(ff):
    print("\n== static component checks ==")
    for flag, pats in STATIC:
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

    static_checks(ff)
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

    n_fail = sum(1 for s, _, _ in results if s == "FAIL")
    n_hw   = sum(1 for s, _, _ in results if s == "HWSKIP")
    n_pass = sum(1 for s, _, _ in results if s == "PASS")
    print(f"\n{n_pass} passed, {n_fail} failed, {n_hw} skipped for lack of NVIDIA hardware")
    if n_hw:
        print("  (HWSKIP means the encoder IS in the binary and failed only at "
              "device-open time -- a property of this machine, not of the build.)")
    return 1 if n_fail else 0

if __name__ == "__main__":
    sys.exit(main())
