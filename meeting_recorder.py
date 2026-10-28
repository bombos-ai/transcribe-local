#!/usr/bin/env python3
"""
meeting_recorder.py — capture a meeting locally and transcribe it via the
TranscriptionSuite *server* (bypassing the Electron client entirely).

Why this exists: the Electron client can only capture ONE source at a time
(mic OR system), so on a call it recorded everyone but you. This script
captures BOTH your microphone and the system output (what you hear — the
remote participants) at once and saves them, unprocessed, as a 16 kHz stereo
WAV (left = mic, right = system) — to disk FIRST, the only irreplaceable
asset. At transcription time the two sides are levelled and mixed to mono,
sent to the server over plain REST, and the diarized transcript is written to
OUTPUT_DIR (TRANSCRIPTION_OUTPUT_DIR, default ~/Transcriptions).

Usage:
    python meeting_recorder.py                 # record until you press Enter, then transcribe
    python meeting_recorder.py --title "Weekly sync"
    python meeting_recorder.py --file some.wav # skip recording; transcribe an existing recording (WAV, MP3, …)
    python meeting_recorder.py --list-devices  # show input/output devices
    python meeting_recorder.py --selftest      # record 5 s and report mic/system levels
    python meeting_recorder.py --server-up     # start the server and leave it up
    python meeting_recorder.py --server-down   # stop the server

The transcription server is a Docker Compose stack that only runs while it is
needed: it is brought up when transcription starts and taken down when it
finishes. A server that was already running (e.g. the Electron dashboard's) is
used as-is and left alone.

Settings come from environment variables or a .env file next to this script
(see .env.example).

Deps:  pip install soundcard numpy requests   (plus the `docker` CLI on PATH)
"""
import argparse, atexit, contextlib, datetime, os, re, subprocess, sys, threading, time, wave, json, queue, warnings
import numpy as np
import requests

# Windows terminals default to cp1252; make our status glyphs safe everywhere.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _load_dotenv():
    """KEY=value lines from the .env next to this script, for anything not
    already set in the real environment (which always wins)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"\'')
            if k and v:
                os.environ.setdefault(k, v)


_load_dotenv()

# ---------------------------------------------------------------- config
SERVER      = "http://localhost:9786"
OUTPUT_DIR  = os.environ.get("TRANSCRIPTION_OUTPUT_DIR") or os.path.join(
    os.path.expanduser("~"), "Transcriptions")      # transcript .txt goes here
AUDIO_DIR   = os.path.join(OUTPUT_DIR, "recordings")  # WAVs kept here
# Optional second folder that gets a clean .md copy of each diarized transcript
# (e.g. a notes app's or knowledge base's inbox). Off when unset.
INBOX_DIR   = os.environ.get("TRANSCRIPTION_INBOX_DIR") or None
LANGUAGE    = os.environ.get("TRANSCRIPTION_LANGUAGE") or "nl"
CAPTURE_SR  = 48000          # WASAPI native
TARGET_SR   = 16000          # what we send (48000/16000 = 3, clean decimation)
DECIM       = CAPTURE_SR // TARGET_SR
BLOCK       = 4800           # 100 ms at 48 kHz
CAPTURE_BUFFER = CAPTURE_SR // 2   # 0.5 s WASAPI buffer per stream (see record_session)
# Mixing (at transcription time): each side gets ONE gain for the whole recording,
# calibrated so both sides' speech comes out equally loud. No ducking — when
# people overlap, both must stay audible.
MIX_TARGET_RMS = 0.08        # ~-22 dBFS for each side's speech
MIX_MAX_GAIN   = 30.0        # never boost a side more than ~30 dB (don't blow up noise)
MIC_GAIN    = 1.0            # extra manual mic multiplier on top of the automatic mix
MIC_NAME    = None           # substring to pick a mic; None = default input

# The transcription server is a Docker Compose stack that we only keep running
# while we actually need it (it holds a GPU and several GB of RAM once the
# models load). COMPOSE_DIR is TranscriptionSuite's compose directory: the
# dashboard's own (whose .env pins COMPOSE_FILE and COMPOSE_PROJECT_NAME), or a
# manual checkout. TRANSCRIPTION_COMPOSE_FILES (separated by ';' on Windows)
# picks the -f files explicitly, for a checkout whose .env doesn't pin them.
COMPOSE_DIR = os.environ.get("TRANSCRIPTION_COMPOSE_DIR") or next(
    (d for d in (os.path.join(os.environ.get("APPDATA", ""), "TranscriptionSuite", "docker"),
                 os.path.join(os.path.expanduser("~"), "TranscriptionSuite", "docker"))
     if os.path.isdir(d)),
    os.path.join(os.environ.get("APPDATA", ""), "TranscriptionSuite", "docker"))
COMPOSE_FILES = [f for f in (os.environ.get("TRANSCRIPTION_COMPOSE_FILES") or "").split(os.pathsep) if f]
SERVER_START_TIMEOUT = 900   # first start bootstraps a venv + models; be generous
SERVER_STOP_TIMEOUT  = 180   # stop_grace_period is 130s
# CPU-only servers are slow on long meetings, so allow plenty of time.
SERVER_TIMEOUT = 4 * 3600
# -----------------------------------------------------------------------


def _ts():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H%M")


# Keep the machine awake WHILE recording and WHILE transcribing, so a
# lock-then-sleep can't cut the capture off, and Modern Standby can't throttle
# the server's Docker VM to a crawl mid-transcription (a 2.5 h file once spent
# 3+ hours of its timeout in standby). The screen may still turn off and lock —
# we only block system sleep, not the display. The flag is per thread, so a
# recording and a transcription running side by side don't clear each other.
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


_dropouts = 0
def _count_dropouts():
    """soundcard forces its warnings to 'always' on import, so a filter can't hide
    them. Count dropouts instead of printing one line per 10 ms glitch."""
    global _dropouts
    _dropouts = 0
    show = warnings.showwarning
    if getattr(show, "_counts_dropouts", False):
        return
    def _show(message, category, *a, **kw):
        global _dropouts
        if "data discontinuity" in str(message):
            _dropouts += 1
        else:
            show(message, category, *a, **kw)
    _show._counts_dropouts = True
    warnings.showwarning = _show


def record_session(wav_path, stop_event, selftest_seconds=None):
    """Capture mic + system loopback on separate threads and write a 16 kHz
    16-bit STEREO WAV: left = mic, right = system, both unprocessed. Levels are
    balanced later, at transcription time (see mix_for_upload), so the raw
    recording can always be remixed. Memory stays flat: each block is downsampled
    and written immediately. Blocks are paired 1:1, so the two streams stay in sync."""
    import soundcard as sc
    _count_dropouts()
    mic_dev, loop_dev, spk = _open_streams(sc)
    print(f"Mic:    {mic_dev.name}")
    print(f"System: {spk.name} (loopback)")
    os.makedirs(os.path.dirname(wav_path), exist_ok=True)

    # 0.5 s WASAPI buffers: soundcard's default (one ~10 ms device period) drops
    # audio whenever Python stalls briefly, e.g. when the machine is short on RAM.
    mic_q, sys_q = queue.Queue(), queue.Queue()
    cap_stop = threading.Event()
    t_mic = threading.Thread(target=_capture_loop,
        args=(mic_dev.recorder(samplerate=CAPTURE_SR, channels=1, blocksize=CAPTURE_BUFFER),
              mic_q, cap_stop), daemon=True)
    t_sys = threading.Thread(target=_capture_loop,
        args=(loop_dev.recorder(samplerate=CAPTURE_SR, channels=2, blocksize=CAPTURE_BUFFER),
              sys_q, cap_stop), daemon=True)

    peak_mic = peak_sys = 0.0
    blocks = 0
    wf = wave.open(wav_path, "wb")
    wf.setnchannels(2); wf.setsampwidth(2); wf.setframerate(TARGET_SR)

    def _down(x):
        n = (len(x) // DECIM) * DECIM
        return x[:n].reshape(-1, DECIM).mean(axis=1)

    def _mix_write(mic, sysd):
        nonlocal peak_mic, peak_sys, blocks
        mic_m = mic[:, 0]
        sys_m = sysd.mean(axis=1)
        peak_mic = max(peak_mic, float(np.abs(mic_m).max(initial=0.0)))
        peak_sys = max(peak_sys, float(np.abs(sys_m).max(initial=0.0)))
        st = np.stack([_down(mic_m), _down(sys_m)], axis=1)
        np.clip(st, -1.0, 1.0, out=st)
        wf.writeframes((st * 32767.0).astype("<i2").tobytes())
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
    print(f"■ Stopped. {secs:0.1f}s written -> {wav_path}  (stereo: L=mic, R=system)")
    print(f"   peak level  mic={peak_mic:0.3f}  system={peak_sys:0.3f}")
    if _dropouts:
        print(f"   ⚠ {_dropouts} capture dropout(s) — short gaps; the machine may be overloaded.")
    if peak_mic < 0.001:
        print("   ⚠ MIC WAS SILENT — your own voice would be missing. Check the mic device!")
    if peak_sys < 0.001:
        print("   ⚠ SYSTEM WAS SILENT — no remote audio captured (ok if nothing was playing).")
    return wav_path, secs, peak_mic, peak_sys


# ---------------------------------------------------------------- mixing
FRAME = TARGET_SR // 10      # 100 ms analysis frames
CHUNK = FRAME * 600          # process 60 s at a time so memory stays flat


def _read_chunks(wav_path):
    """Yield (mic, system) float32 arrays from a stereo 16-bit WAV, 60 s at a time."""
    with wave.open(wav_path, "rb") as wf:
        while True:
            raw = wf.readframes(CHUNK)
            if not raw:
                return
            st = np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(np.float32) / 32767.0
            yield st[:, 0], st[:, 1]


def _speech_level(rms):
    """Speech loudness of one side, from its 100 ms frame RMS. Takes the frames
    clearly above the noise floor (speech, not silence — so a side that only
    talks 10% of the meeting isn't calibrated on its silence), then their 90th
    percentile: the dominant loud moments, with the loudest 10% (coughs, bumps,
    one-off bangs) left out as outliers. 0 means: leave this side alone."""
    if not len(rms):
        return 0.0
    floor = float(np.percentile(rms, 20))
    active = rms[rms > max(floor * 3, 1e-4)]
    if len(active) >= 5:
        return float(np.percentile(active, 90))
    if floor > 1e-3:                    # never quiet (music, constant noise): level it as a whole
        return float(np.percentile(rms, 90))
    return 0.0                          # silent side


def mix_for_upload(wav_path, mic_gain=MIC_GAIN):
    """Return (path_to_send, is_temp). A stereo recording (L=mic, R=system) is
    mixed to mono so that neither side drowns the other: each side gets one
    constant gain, calibrated on this recording, that brings its speech to
    MIX_TARGET_RMS (a quiet built-in mic gets boosted, loud system audio gets
    turned down). Overlapping speech stays fully audible on both sides.
    Mono files (older recordings, other sources) and non-WAV audio (mp3, m4a, …;
    the server decodes those itself) are sent unchanged."""
    import tempfile
    try:
        with wave.open(wav_path, "rb") as wf:
            if wf.getnchannels() != 2 or wf.getsampwidth() != 2 or wf.getframerate() != TARGET_SR:
                return wav_path, False
    except (wave.Error, EOFError):
        return wav_path, False

    # pass 1: per-frame loudness of each side
    rms_mic, rms_sys = [], []
    for mic, sysd in _read_chunks(wav_path):
        n = len(mic) // FRAME * FRAME
        if n:
            rms_mic.append(np.sqrt((mic[:n].reshape(-1, FRAME) ** 2).mean(axis=1)))
            rms_sys.append(np.sqrt((sysd[:n].reshape(-1, FRAME) ** 2).mean(axis=1)))
    if not rms_mic:
        return wav_path, False
    rms_mic, rms_sys = np.concatenate(rms_mic), np.concatenate(rms_sys)

    lvl_mic, lvl_sys = _speech_level(rms_mic), _speech_level(rms_sys)
    g_mic = min(MIX_TARGET_RMS / lvl_mic, MIX_MAX_GAIN) if lvl_mic else 1.0
    g_sys = min(MIX_TARGET_RMS / lvl_sys, MIX_MAX_GAIN) if lvl_sys else 1.0
    g_mic *= mic_gain
    print(f"   mix: mic x{g_mic:.1f}, system x{g_sys:.2f}  "
          f"(speech level mic {lvl_mic:.4f}, system {lvl_sys:.4f})")

    # pass 2: write the mono mix to a temp file
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="meeting_mix_")
    os.close(fd)
    with wave.open(out, "wb") as of:
        of.setnchannels(1); of.setsampwidth(2); of.setframerate(TARGET_SR)
        for mic, sysd in _read_chunks(wav_path):
            mix = g_mic * mic + g_sys * sysd
            np.clip(mix, -1.0, 1.0, out=mix)
            of.writeframes((mix * 32767.0).astype("<i2").tobytes())
    return out, True


# ------------------------------------------------------- server lifecycle
# pythonw has no console, so a bare subprocess call would flash a window.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class ServerStartError(RuntimeError):
    """The server could not be brought up (as opposed to failing a transcription)."""


def _compose_cmd(*args):
    files = [a for f in COMPOSE_FILES for a in ("-f", f)]
    return ["docker", "compose", *files, *args]


def _compose_env():
    """The server's diarization model needs a Hugging Face token. A manual
    checkout's .env often leaves HUGGINGFACE_TOKEN empty, so pass ours (HF_TOKEN
    or HUGGINGFACE_TOKEN from the environment or our .env) through; compose
    prefers the process environment over its .env file."""
    env = dict(os.environ)
    tok = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    if tok:
        env["HUGGINGFACE_TOKEN"] = tok
    return env


def _compose(*args, timeout=None):
    """Run `docker compose <args>` in COMPOSE_DIR. Raises with the stderr tail."""
    if not os.path.isdir(COMPOSE_DIR):
        raise ServerStartError(f"Compose directory not found: {COMPOSE_DIR}\n"
                               "Set TRANSCRIPTION_COMPOSE_DIR (see README).")
    cp = subprocess.run(_compose_cmd(*args), cwd=COMPOSE_DIR, env=_compose_env(),
                        capture_output=True, text=True, timeout=timeout,
                        creationflags=_NO_WINDOW)
    if cp.returncode != 0:
        tail = "\n".join((cp.stderr or cp.stdout or "").strip().splitlines()[-4:])
        raise RuntimeError(f"docker compose {' '.join(args)} failed "
                           f"(is Docker Desktop running?):\n{tail}")
    return cp.stdout


# Several transcriptions can run at once in the tray, so the stack is
# reference-counted: the first user brings it up, the last one takes it down —
# and only if this process started it.
_srv_lock = threading.Lock()
_srv_users = 0              # transcriptions in flight in this process
_owned = False              # True while THIS process is the one keeping the stack up
_stopping = None            # Popen of an in-flight `docker compose down`


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


def _wait_for_stop():
    """A previous run's teardown may still be draining; let it finish first."""
    global _stopping
    if _stopping is not None:
        with contextlib.suppress(Exception):
            _stopping.wait(timeout=SERVER_STOP_TIMEOUT)
        _stopping = None


def start_server(progress=None, timeout=SERVER_START_TIMEOUT):
    """Bring the transcription stack up and wait until /health answers.

    Returns True if WE started it (so the caller should stop it again), False if
    it was already running — someone else's session, e.g. the Electron
    dashboard, must not be torn down underneath them. Raises ServerStartError."""
    global _owned
    say = progress or (lambda m: print(m))
    _wait_for_stop()
    if server_healthy():
        if not _owned:
            say("Transcription server already running — leaving it alone.")
        return False
    say("Starting transcription server…")
    try:
        _compose("up", "-d", timeout=300)
    except ServerStartError:
        raise
    except Exception as e:
        raise ServerStartError(str(e)) from e
    _owned = True
    deadline = time.monotonic() + timeout
    last = 0.0
    while time.monotonic() < deadline:
        if server_healthy():            # /health only answers once the model is loaded
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
    raise ServerStartError(f"Transcription server did not become healthy within {timeout}s.")


def stop_server(progress=None, wait=True):
    """`docker compose down`. wait=False fires it off in the background: the
    server drains gracefully (up to ~130 s) and nothing needs to sit through
    that; the next start_server waits for it to finish."""
    global _owned, _stopping
    say = progress or (lambda m: print(m))
    say("Stopping transcription server…")
    _owned = False
    if wait:
        _compose("down", timeout=SERVER_STOP_TIMEOUT)
        say("Transcription server stopped.")
    else:
        _stopping = subprocess.Popen(_compose_cmd("down"), cwd=COMPOSE_DIR, env=_compose_env(),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=_NO_WINDOW)


@contextlib.contextmanager
def transcription_server(progress=None, manage=True, keep_up=False):
    """Have the server running for the duration of the block.

    manage=False leaves Docker entirely alone (server already handled elsewhere).
    keep_up=True starts it if needed but never stops it."""
    global _owned, _srv_users
    if not manage:
        yield
        return
    with _srv_lock:
        start_server(progress)
        _srv_users += 1
    try:
        yield
    finally:
        with _srv_lock:
            _srv_users -= 1
            if keep_up:
                _owned = False                  # deliberately left running; disown it
            elif _owned and _srv_users == 0:
                # A teardown failure must never mask a finished transcript.
                try:
                    stop_server(progress, wait=False)
                except Exception as e:
                    (progress or print)(f"WARNING: could not stop the server: {e}")


def transcribe_file(wav_path, language=LANGUAGE, diarization=True, expected_speakers=None,
                    mic_gain=MIC_GAIN, progress=None, manage_server=True, keep_server=False):
    """Mix (if stereo), bring the server up if needed, transcribe, take it down
    again. Blocks system sleep for the duration (see _keep_awake). Raises
    ServerStartError if the server can't be started."""
    _keep_awake(True)
    upload, is_temp = wav_path, False
    try:
        upload, is_temp = mix_for_upload(wav_path, mic_gain)
        with transcription_server(progress, manage=manage_server, keep_up=keep_server):
            (progress or print)("Transcribing…")
            return _transcribe_request(wav_path, upload, language, diarization, expected_speakers)
    finally:
        _keep_awake(False)
        if is_temp:
            with contextlib.suppress(OSError):
                os.remove(upload)


def _transcribe_request(wav_path, upload, language, diarization, expected_speakers):
    """POST the (mixed) audio to the server and return the result dict. Handles
    both the synchronous /audio response and a job-based fallback."""
    import mimetypes
    url = f"{SERVER}/api/transcribe/audio"
    data = {"language": language,
            "diarization": str(bool(diarization)).lower(),
            "word_timestamps": "true"}
    if expected_speakers:
        data["expected_speakers"] = str(int(expected_speakers))
    print(f"→ Uploading {os.path.basename(wav_path)} to {url} (diarization={diarization}) …")
    # the server keeps the filename's extension and decodes by it, so the name
    # must match the bytes: a mixed temp is always WAV, an unmixed upload is the original
    name = os.path.basename(wav_path) if upload == wav_path else \
        os.path.splitext(os.path.basename(wav_path))[0] + ".wav"
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    with open(upload, "rb") as fh:
        files = {"file": (name, fh, mime)}
        r = requests.post(url, data=data, files=files, timeout=(15, SERVER_TIMEOUT))
    r.raise_for_status()
    result = r.json()
    if "text" in result:
        return result
    # fallback: job-based
    job_id = result.get("job_id") or result.get("id")
    if not job_id:
        raise RuntimeError(f"Unexpected response: {json.dumps(result)[:300]}")
    print(f"   job {job_id} queued; polling …")
    deadline = time.monotonic() + SERVER_TIMEOUT      # never poll forever
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
            raise TimeoutError(f"Timed out waiting for job {job_id} after {SERVER_TIMEOUT // 3600}h.")


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


def _slug(text):
    import unicodedata
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def write_inbox_copy(txt_path, title=""):
    """Copy a diarized transcript to INBOX_DIR as YYYY-MM-DD-transcript-<title>.md,
    one '[speaker] text' line per turn, no timestamps. Never overwrites: adds
    -2, -3, … instead. Returns the written path, or None when skipped (inbox not
    configured or unreachable, or the transcript has no speaker labels)."""
    if not INBOX_DIR:
        return None
    turns = read_turns(txt_path)
    if not turns:
        print("   (no speaker labels — skipped inbox copy)")
        return None
    if not os.path.isdir(INBOX_DIR):
        print(f"   ⚠ inbox not reachable, skipped: {INBOX_DIR}")
        return None

    stamp = os.path.basename(txt_path)[:10]     # transcripts start with YYYY-MM-DD_HHMM
    try:
        datetime.date.fromisoformat(stamp)
    except ValueError:
        stamp = datetime.date.today().isoformat()
    name = "-".join(p for p in (stamp, "transcript", _slug(title)) if p)
    path, n = os.path.join(INBOX_DIR, name + ".md"), 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(INBOX_DIR, f"{name}-{n}.md")

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# Transcript: {title or stamp}\n\n")
        f.write("\n".join(f"[{label}] {text}" for label, text in turns) + "\n")
    print(f"✓ Inbox copy: {path}")
    return path


def title_from_txt(txt_path):
    """Recover the title from '<YYYY-MM-DD_HHMM>_<title-with-dashes>.txt'."""
    stem = os.path.splitext(os.path.basename(txt_path))[0]
    if stem[:10].count("-") == 2 and stem[15:16] == "_":
        return stem[16:].replace("-", " ")
    return "" if stem[:10].count("-") == 2 else stem


def main():
    ap = argparse.ArgumentParser(description="Record a meeting and transcribe via the TranscriptionSuite server.")
    ap.add_argument("--title", default="", help="label for the output filenames")
    ap.add_argument("--file", help="transcribe an existing recording (WAV, MP3, …) instead of recording")
    ap.add_argument("--language", default=LANGUAGE)
    ap.add_argument("--speakers", type=int, default=None, help="expected number of speakers (hint for diarization)")
    ap.add_argument("--no-diarization", action="store_true")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="record 5 s and report mic/system levels")
    ap.add_argument("--mic-gain", type=float, default=MIC_GAIN,
                    help=f"extra mic multiplier on top of the automatic mix (default {MIC_GAIN:g})")
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
        record_session(wav, threading.Event(), selftest_seconds=5); return

    if args.file:
        wav = args.file
        base = os.path.join(OUTPUT_DIR, f"{_ts()}{label}")
    else:
        wav = os.path.join(AUDIO_DIR, f"{_ts()}{label}.wav")
        stop = threading.Event()
        t = threading.Thread(target=lambda: (input(), stop.set()), daemon=True)
        t.start()
        record_session(wav, stop)
        base = os.path.join(OUTPUT_DIR, f"{_ts()}{label}")

    result = transcribe_file(wav, language=args.language,
                             diarization=not args.no_diarization,
                             expected_speakers=args.speakers,
                             mic_gain=args.mic_gain,
                             manage_server=not args.no_manage_server,
                             keep_server=args.keep_server)
    txt = write_outputs(result, base)
    write_inbox_copy(txt, args.title)
    print(f"\nAudio kept at: {wav}")


if __name__ == "__main__":
    main()
