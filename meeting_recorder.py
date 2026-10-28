#!/usr/bin/env python3
"""
meeting_recorder.py — capture a meeting locally and transcribe it via the
TranscriptionSuite *server* (bypassing the Electron client entirely).

Why this exists: the Electron client can only capture ONE source at a time
(mic OR system), so on a call it recorded everyone but you. This script
captures BOTH your microphone and the system output (what you hear — the
remote participants) at once, mixes them into a single 16 kHz mono WAV, saves
that WAV to disk FIRST (the only irreplaceable asset), then sends it to the
server over plain REST and writes the diarized transcript to OUTPUT_DIR
(%TRANSCRIPTION_OUTPUT_DIR%, default ~/Transcriptions).

Usage:
    python meeting_recorder.py                 # record until you press Enter, then transcribe
    python meeting_recorder.py --title "Weekly sync"
    python meeting_recorder.py --file some.wav # skip recording; just transcribe an existing WAV
    python meeting_recorder.py --list-devices  # show input/output devices
    python meeting_recorder.py --selftest      # record 5 s and report mic/system levels
    python meeting_recorder.py --server-up     # start the server and leave it up
    python meeting_recorder.py --server-down   # stop the server

The transcription server is a Docker Compose stack that only runs while it is
needed: it is brought up when transcription starts and taken down when it
finishes. A server that was already running (e.g. the Electron dashboard's) is
used as-is and left alone.

Deps:  pip install soundcard numpy requests   (plus the `docker` CLI on PATH)
"""
import argparse, atexit, contextlib, datetime, os, re, subprocess, sys, threading, time, wave, json, queue, warnings
import numpy as np
import requests

# soundcard emits a per-block "data discontinuity" warning whenever a stream's
# buffer is read late. With the threaded capture below that shouldn't happen,
# but silence the category so a rare startup blip doesn't flood the console.
warnings.filterwarnings("ignore", message="data discontinuity in recording")

# Windows terminals default to cp1252; make our status glyphs safe everywhere.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------- config
SERVER      = "http://localhost:9786"
OUTPUT_DIR  = os.environ.get("TRANSCRIPTION_OUTPUT_DIR") or os.path.join(
    os.path.expanduser("~"), "Transcriptions")      # transcript .txt goes here
AUDIO_DIR   = os.path.join(OUTPUT_DIR, "recordings")  # WAVs kept here
LANGUAGE    = os.environ.get("TRANSCRIPTION_LANGUAGE") or "nl"
CAPTURE_SR  = 48000          # WASAPI native
TARGET_SR   = 16000          # what we send (48000/16000 = 3, clean decimation)
DECIM       = CAPTURE_SR // TARGET_SR
BLOCK       = 4800           # 100 ms at 48 kHz
MIC_GAIN    = 3.0            # boost the mic; headset (HFP) mics run quiet vs. system
SYS_GAIN    = 1.0
MIC_NAME    = None           # substring to pick a mic; None = default input

# The transcription server is a Docker Compose stack that we only keep running
# while we actually need it (it holds a GPU and several GB of RAM once the
# models load). COMPOSE_DIR is the TranscriptionSuite dashboard's own compose
# directory; its .env pins COMPOSE_FILE and COMPOSE_PROJECT_NAME, so we never
# pass -f/-p ourselves and can't drift out of sync with the dashboard.
COMPOSE_DIR = os.environ.get("TRANSCRIPTION_COMPOSE_DIR") or os.path.join(
    os.environ.get("APPDATA", ""), "TranscriptionSuite", "docker")
SERVER_START_TIMEOUT = 900   # first start bootstraps a venv + models; be generous
SERVER_STOP_TIMEOUT  = 180   # stop_grace_period is 130s
# -----------------------------------------------------------------------


def _ts():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H%M")


# Keep the machine awake WHILE recording so a lock-then-sleep can't cut the
# capture off. The screen may still turn off and lock — we only block system
# sleep, not the display. Cleared as soon as recording stops.
_ES_CONTINUOUS       = 0x80000000
_ES_SYSTEM_REQUIRED  = 0x00000001
def _keep_awake(on):
    if sys.platform != "win32":
        return
    try:
        import ctypes
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def list_devices():
    import soundcard as sc
    print("=== Microphones (inputs) ===")
    for m in sc.all_microphones(include_loopback=True):
        tag = " [LOOPBACK]" if getattr(m, "isloopback", False) else ""
        print(f"  {m.name}{tag}")
    print(f"\nDefault mic:     {sc.default_microphone().name}")
    print(f"Default speaker: {sc.default_speaker().name}  (its loopback is what we capture as 'system')")


def _open_streams(sc):
    mic_dev = sc.default_microphone()
    if MIC_NAME:
        mic_dev = sc.get_microphone(MIC_NAME, include_loopback=False)
    spk = sc.default_speaker()
    loop_dev = sc.get_microphone(spk.name, include_loopback=True)
    return mic_dev, loop_dev, spk


def _capture_loop(recorder_ctx, q, stop_event):
    """Drain one WASAPI stream continuously into a queue. Running each stream in
    its own thread means neither buffer overflows while the other is processed,
    which is what caused the 'data discontinuity' drops (and slow mic/system
    desync over a long meeting)."""
    with recorder_ctx as rec:
        while not stop_event.is_set():
            q.put(rec.record(numframes=BLOCK))


def record_session(wav_path, stop_event, selftest_seconds=None, mic_gain=MIC_GAIN):
    """Capture mic + system loopback on separate threads, mix, write a 16 kHz
    mono 16-bit WAV. Memory stays flat: each block is downsampled and written
    immediately. Blocks are paired 1:1, so the two streams stay in sync."""
    import soundcard as sc
    mic_dev, loop_dev, spk = _open_streams(sc)
    print(f"Mic:    {mic_dev.name}  (gain x{mic_gain:g})")
    print(f"System: {spk.name} (loopback)")
    os.makedirs(os.path.dirname(wav_path), exist_ok=True)

    mic_q, sys_q = queue.Queue(), queue.Queue()
    cap_stop = threading.Event()
    t_mic = threading.Thread(target=_capture_loop,
        args=(mic_dev.recorder(samplerate=CAPTURE_SR, channels=1), mic_q, cap_stop), daemon=True)
    t_sys = threading.Thread(target=_capture_loop,
        args=(loop_dev.recorder(samplerate=CAPTURE_SR, channels=2), sys_q, cap_stop), daemon=True)

    peak_mic = peak_sys = 0.0
    blocks = 0
    wf = wave.open(wav_path, "wb")
    wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(TARGET_SR)

    def _mix_write(mic, sysd):
        nonlocal peak_mic, peak_sys, blocks
        mic_m = mic[:, 0]
        sys_m = sysd.mean(axis=1)
        peak_mic = max(peak_mic, float(np.abs(mic_m).max(initial=0.0)))
        peak_sys = max(peak_sys, float(np.abs(sys_m).max(initial=0.0)))
        mix = mic_gain * mic_m + SYS_GAIN * sys_m
        np.clip(mix, -1.0, 1.0, out=mix)
        n = (len(mix) // DECIM) * DECIM
        down = mix[:n].reshape(-1, DECIM).mean(axis=1)
        wf.writeframes((down * 32767.0).astype("<i2").tobytes())
        blocks += 1

    try:
        _keep_awake(True)                 # block system sleep for the duration
        t_mic.start(); t_sys.start()
        print("● Recording…  (press Enter to stop)" if selftest_seconds is None
              else f"● Self-test: capturing {selftest_seconds}s…")
        t0 = time.monotonic()
        while not stop_event.is_set():
            try:
                mic = mic_q.get(timeout=1.0)
                sysd = sys_q.get(timeout=1.0)
            except queue.Empty:
                continue
            _mix_write(mic, sysd)
            if selftest_seconds is not None and time.monotonic() - t0 >= selftest_seconds:
                break
    finally:
        cap_stop.set()
        # drain whatever both streams already captured, keeping them paired
        while not mic_q.empty() and not sys_q.empty():
            _mix_write(mic_q.get(), sys_q.get())
        wf.close()
        _keep_awake(False)                # allow sleep again
    secs = blocks * BLOCK / CAPTURE_SR
    print(f"■ Stopped. {secs:0.1f}s written -> {wav_path}")
    print(f"   peak level  mic={peak_mic:0.3f} (x{mic_gain:g} -> {min(peak_mic*mic_gain,1.0):0.3f})  system={peak_sys:0.3f}")
    if peak_mic < 0.001:
        print("   ⚠ MIC WAS SILENT — your own voice would be missing. Check the mic device!")
    if peak_sys < 0.001:
        print("   ⚠ SYSTEM WAS SILENT — no remote audio captured (ok if nothing was playing).")
    return wav_path, secs, peak_mic, peak_sys


# ------------------------------------------------------- server lifecycle
# pythonw has no console, so a bare subprocess call would flash a window.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _compose(*args, timeout=None):
    """Run `docker compose <args>` in COMPOSE_DIR. Raises with the stderr tail."""
    if not os.path.isdir(COMPOSE_DIR):
        raise RuntimeError(f"Compose directory not found: {COMPOSE_DIR}")
    cp = subprocess.run(["docker", "compose", *args], cwd=COMPOSE_DIR,
                        capture_output=True, text=True, timeout=timeout,
                        creationflags=_NO_WINDOW)
    if cp.returncode != 0:
        tail = "\n".join((cp.stderr or cp.stdout or "").strip().splitlines()[-4:])
        raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{tail}")
    return cp.stdout


_owned = False      # True while THIS process is the one keeping the stack up


def _stop_if_owned():
    """Last line of defence: if the process exits mid-transcription (tray Quit,
    Ctrl-C), don't leave the stack — and the GPU — occupied."""
    if _owned:
        with contextlib.suppress(Exception):
            stop_server()


atexit.register(_stop_if_owned)


def server_healthy(timeout=3):
    try:
        return requests.get(f"{SERVER}/health", timeout=timeout).ok
    except Exception:
        return False


def start_server(progress=None, timeout=SERVER_START_TIMEOUT):
    """Bring the transcription stack up and wait until /health answers.

    Returns True if WE started it (so the caller should stop it again), False if
    it was already running — someone else's session, e.g. the Electron
    dashboard, must not be torn down underneath them."""
    say = progress or (lambda m: print(m))
    if server_healthy():
        say("Transcription server already running — leaving it alone.")
        return False
    say("Starting transcription server…")
    global _owned
    _compose("up", "-d", timeout=300)
    _owned = True
    deadline = time.monotonic() + timeout
    last = 0.0
    while time.monotonic() < deadline:
        if server_healthy():
            say("Transcription server is up.")
            return True
        now = time.monotonic()
        if now - last >= 30:
            last = now
            say(f"Waiting for the server to become healthy… ({int(deadline - now)}s left)")
        time.sleep(2)
    # Don't leave a half-started stack behind on a failed bring-up.
    with contextlib.suppress(Exception):
        stop_server()
    raise TimeoutError(f"Transcription server did not become healthy within {timeout}s.")


def stop_server(progress=None):
    global _owned
    say = progress or (lambda m: print(m))
    say("Stopping transcription server…")
    _compose("down", timeout=SERVER_STOP_TIMEOUT)
    _owned = False
    say("Transcription server stopped.")


@contextlib.contextmanager
def transcription_server(progress=None, manage=True, keep_up=False):
    """Own the server only for the duration of the block.

    manage=False leaves Docker entirely alone (server already handled elsewhere).
    keep_up=True starts it if needed but never stops it."""
    global _owned
    if not manage:
        yield False
        return
    started = start_server(progress)
    try:
        yield started
    finally:
        if keep_up:
            _owned = False                  # deliberately left running; disown it
        elif started:
            # A teardown failure must never mask a finished transcript.
            try:
                stop_server(progress)
            except Exception as e:
                (progress or print)(f"WARNING: could not stop the server: {e}")


def transcribe_file(wav_path, language=LANGUAGE, diarization=True, expected_speakers=None):
    """POST the WAV to the server and return the result dict. Handles both the
    synchronous /audio response and a job-based fallback."""
    url = f"{SERVER}/api/transcribe/audio"
    data = {"language": language,
            "diarization": str(bool(diarization)).lower(),
            "word_timestamps": "true"}
    if expected_speakers:
        data["expected_speakers"] = str(int(expected_speakers))
    print(f"→ Uploading {os.path.basename(wav_path)} to {url} (diarization={diarization}) …")
    with open(wav_path, "rb") as fh:
        files = {"file": (os.path.basename(wav_path), fh, "audio/wav")}
        r = requests.post(url, data=data, files=files, timeout=(15, 3600))
    r.raise_for_status()
    result = r.json()
    if "text" in result:
        return result
    # fallback: job-based
    job_id = result.get("job_id") or result.get("id")
    if not job_id:
        raise RuntimeError(f"Unexpected response: {json.dumps(result)[:300]}")
    print(f"   job {job_id} queued; polling …")
    deadline = time.monotonic() + 3600      # never poll forever
    while True:
        time.sleep(3)
        pr = requests.get(f"{SERVER}/api/transcribe/result/{job_id}", timeout=30)
        if pr.status_code == 200:
            jr = pr.json()
            if (jr.get("status") or "").lower() in ("failed", "error"):
                raise RuntimeError(jr.get("error") or "Server reported the job failed.")
            return jr
        if pr.status_code not in (202, 404):
            pr.raise_for_status()
        if time.monotonic() > deadline:
            raise TimeoutError(f"Timed out waiting for job {job_id} after 1h.")


def _speaker_label(spk):
    """'SPEAKER_00' -> 'speaker 0'; anything without a trailing number is kept as-is."""
    m = re.search(r"(\d+)$", str(spk))
    return f"speaker {int(m.group(1))}" if m else str(spk)


def format_turns(segments):
    """One line per speaker turn: consecutive segments by the same speaker are
    merged. [(speaker, text), ...] -> '[speaker 0] ...\\n[speaker 1] ...\\n'."""
    turns = []   # [speaker, [texts]]
    for spk, text in segments:
        text = (text or "").strip()
        if not text:
            continue
        if turns and turns[-1][0] == spk:
            turns[-1][1].append(text)
        else:
            turns.append([spk, [text]])
    lines = []
    for spk, texts in turns:
        prefix = f"[{_speaker_label(spk)}] " if spk else ""
        lines.append(prefix + " ".join(texts))
    return "\n".join(lines) + "\n" if lines else ""


_TURN_RE = re.compile(r"^\[([^\]]+)\] ?(.*)$")


def read_turns(txt_path):
    """Parse a speaker-turn .txt back into [(label, text), ...]."""
    turns = []
    with open(txt_path, encoding="utf-8-sig") as f:
        for line in f:
            m = _TURN_RE.match(line.rstrip("\n"))
            if m:
                turns.append((m.group(1), m.group(2)))
    return turns


def rename_speakers(txt_path, names):
    """Rewrite '[old] ...' line prefixes using names={old_label: new_name}.
    Blank names are left as they were."""
    names = {k: v.strip() for k, v in names.items() if v and v.strip()}
    if not names:
        return
    with open(txt_path, encoding="utf-8-sig") as f:
        lines = f.readlines()
    out = []
    for line in lines:
        m = _TURN_RE.match(line.rstrip("\n"))
        if m and m.group(1) in names:
            line = f"[{names[m.group(1)]}] {m.group(2)}\n"
        out.append(line)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.writelines(out)


def write_outputs(result, base):
    segs = result.get("segments") or []
    if segs:
        body = format_turns((s.get("speaker"), s.get("text")) for s in segs)
    else:
        body = (result.get("text") or "").strip() + "\n"
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(body)
    print(f"✓ Wrote:\n   {base}.txt  ({len(segs)} segments, {body.count(chr(10))} turns)")
    print(f"   language={result.get('language')}  duration={result.get('duration')}s  "
          f"speakers={result.get('num_speakers')}")
    return base + ".txt"


def main():
    ap = argparse.ArgumentParser(description="Record a meeting and transcribe via the TranscriptionSuite server.")
    ap.add_argument("--title", default="", help="label for the output filenames")
    ap.add_argument("--file", help="transcribe an existing WAV instead of recording")
    ap.add_argument("--language", default=LANGUAGE)
    ap.add_argument("--speakers", type=int, default=None, help="expected number of speakers (hint for diarization)")
    ap.add_argument("--no-diarization", action="store_true")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="record 5 s and report mic/system levels")
    ap.add_argument("--mic-gain", type=float, default=MIC_GAIN, help=f"mic boost in the mix (default {MIC_GAIN:g})")
    ap.add_argument("--no-manage-server", action="store_true",
                    help="don't touch Docker; assume the server is already running")
    ap.add_argument("--keep-server", action="store_true",
                    help="start the server if needed but leave it running afterwards")
    ap.add_argument("--server-up", action="store_true", help="just start the server and exit")
    ap.add_argument("--server-down", action="store_true", help="just stop the server and exit")
    args = ap.parse_args()

    if args.server_up:
        global _owned
        start_server()
        _owned = False          # explicit "up" means leave it up after we exit
        return
    if args.server_down:
        stop_server(); return

    if args.list_devices:
        list_devices(); return
    os.makedirs(OUTPUT_DIR, exist_ok=True); os.makedirs(AUDIO_DIR, exist_ok=True)
    label = ("_" + args.title.replace(" ", "-")) if args.title else ""

    if args.selftest:
        wav = os.path.join(AUDIO_DIR, f"selftest_{_ts()}.wav")
        record_session(wav, threading.Event(), selftest_seconds=5, mic_gain=args.mic_gain); return

    if args.file:
        wav = args.file
        base = os.path.join(OUTPUT_DIR, f"{_ts()}{label}")
    else:
        wav = os.path.join(AUDIO_DIR, f"{_ts()}{label}.wav")
        stop = threading.Event()
        t = threading.Thread(target=lambda: (input(), stop.set()), daemon=True)
        t.start()
        record_session(wav, stop, mic_gain=args.mic_gain)
        base = os.path.join(OUTPUT_DIR, f"{_ts()}{label}")

    with transcription_server(manage=not args.no_manage_server, keep_up=args.keep_server):
        result = transcribe_file(wav, language=args.language,
                                 diarization=not args.no_diarization,
                                 expected_speakers=args.speakers)
    write_outputs(result, base)
    print(f"\nAudio kept at: {wav}")


if __name__ == "__main__":
    main()
