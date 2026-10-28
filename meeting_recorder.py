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

The transcription server only runs while it is needed: it is brought up when
transcription starts and taken down when it finishes. On Windows (and Intel
Macs) that's TranscriptionSuite's Docker Compose stack; on Apple Silicon it's
the dashboard's native Metal/MLX server process. A server that was already
running (e.g. the Electron dashboard's) is used as-is and left alone.

Platforms:
  Windows  mic + WASAPI loopback via `soundcard`
  macOS    mic + a Core Audio process tap via the small Swift helper in
           macos/ (built on first use; needs the Xcode command line tools)

Deps:  pip install -r requirements.txt   (plus the `docker` CLI on PATH for Docker mode)
"""
import argparse, atexit, contextlib, datetime, os, re, signal, subprocess, sys, threading, time, urllib.parse, wave, json, queue, warnings
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
CAPTURE_SR  = 48000          # WASAPI native; the macOS helper resamples to it
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
MACOS       = sys.platform == "darwin"
# The dashboard's app-data folder (Electron userData).
TS_DATA_DIR = (os.path.expanduser("~/Library/Application Support/TranscriptionSuite") if MACOS
               else os.path.join(os.environ.get("APPDATA", ""), "TranscriptionSuite"))
COMPOSE_DIR = os.environ.get("TRANSCRIPTION_COMPOSE_DIR") or os.path.join(TS_DATA_DIR, "docker")
SERVER_START_TIMEOUT = 900   # first start bootstraps a venv + models; be generous
SERVER_STOP_TIMEOUT  = 180   # stop_grace_period is 130s

# How the server is run: "docker" (Compose stack, above) or "metal" (macOS on
# Apple Silicon: the dashboard's native MLX server, a plain uvicorn process
# from its app bundle). "auto" picks metal when that backend is installed.
SERVER_MODE = (os.environ.get("TRANSCRIPTION_SERVER_MODE") or "auto").lower()
TS_APP      = os.environ.get("TRANSCRIPTION_SUITE_APP")       # default: /Applications/TranscriptionSuite.app

_HERE         = os.path.dirname(os.path.abspath(__file__))
AUDIOCAP      = os.path.join(_HERE, "macos", "build", "audiocap")   # macOS capture helper
METAL_PIDFILE = os.path.join(_HERE, "metal-server.pid")
METAL_LOG     = os.path.join(_HERE, "metal-server.log")
# -----------------------------------------------------------------------

# Apps started by launchd or Finder get PATH=/usr/bin:/bin:/usr/sbin:/sbin, which
# has neither `docker` nor Homebrew's tools.
if MACOS:
    _path = os.environ.get("PATH", "").split(os.pathsep)
    for _d in ("/usr/local/bin", "/opt/homebrew/bin"):
        if _d not in _path:
            _path.insert(0, _d)
    os.environ["PATH"] = os.pathsep.join(_path)


def _ts():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H%M")


# Keep the machine awake WHILE recording so a lock-then-sleep can't cut the
# capture off. The screen may still turn off and lock — we only block system
# sleep, not the display. Cleared as soon as recording stops.
_ES_CONTINUOUS       = 0x80000000
_ES_SYSTEM_REQUIRED  = 0x00000001
_caffeinate = None           # macOS: a `caffeinate` child holding the assertion
def _keep_awake(on):
    global _caffeinate
    if MACOS:
        if on and _caffeinate is None:
            with contextlib.suppress(Exception):
                # -i: no idle sleep; -w: let go by itself if we die
                _caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        elif not on and _caffeinate is not None:
            with contextlib.suppress(Exception):
                _caffeinate.terminate()
                _caffeinate.wait(5)
            _caffeinate = None
        return
    if sys.platform != "win32":
        return
    try:
        import ctypes
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def list_devices():
    if MACOS:
        subprocess.run([audiocap_path(), "list"], check=False)
        print("(system audio is a tap on everything the Mac plays, whichever output it goes to)")
        return
    import soundcard as sc
    print("=== Microphones (inputs) ===")
    for m in sc.all_microphones(include_loopback=True):
        tag = " [LOOPBACK]" if getattr(m, "isloopback", False) else ""
        print(f"  {m.name}{tag}")
    print(f"\nDefault mic:     {sc.default_microphone().name}")
    print(f"Default speaker: {sc.default_speaker().name}  (its loopback is what we capture as 'system')")


# --------------------------------------------------- macOS capture helper
def audiocap_path():
    """The Swift capture helper, built on first use (and after an edit)."""
    src = os.path.join(_HERE, "macos", "audiocap.swift")
    stale = (not os.path.exists(AUDIOCAP) or
             (os.path.exists(src) and os.path.getmtime(src) > os.path.getmtime(AUDIOCAP)))
    if stale:
        print("Building the audio capture helper (macos/build.sh)…")
        cp = subprocess.run(["/bin/bash", os.path.join(_HERE, "macos", "build.sh")],
                            capture_output=True, text=True)
        if cp.returncode != 0 or not os.path.exists(AUDIOCAP):
            tail = "\n".join((cp.stderr or cp.stdout or "").strip().splitlines()[-6:])
            raise RuntimeError("Could not build the audio capture helper. Install the Xcode command "
                               f"line tools (xcode-select --install) and run macos/build.sh.\n{tail}")
    return AUDIOCAP


class _HelperSource:
    """A soundcard-shaped capture source backed by `audiocap mic|system`, so
    record_session() doesn't care which platform it's on."""
    def __init__(self, mode, name, device=None):
        self.mode, self.name, self.device = mode, name, device

    def recorder(self, samplerate, channels):
        assert samplerate == CAPTURE_SR
        return _HelperRecorder(self.mode, channels, self.device)


class _HelperRecorder:
    def __init__(self, mode, channels, device):
        self.mode, self.channels, self.device = mode, channels, device
        self.proc, self.err_tail = None, []

    def __enter__(self):
        cmd = [audiocap_path(), self.mode] + (["--device", self.device] if self.device else [])
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, bufsize=0)
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        return self

    def _pump_stderr(self):
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            self.err_tail = (self.err_tail + [line])[-5:]
            print(line)

    def record(self, numframes):
        want = numframes * self.channels * 4
        buf = bytearray()
        while len(buf) < want:
            chunk = self.proc.stdout.read(want - len(buf))
            if not chunk:
                self.proc.wait()
                time.sleep(0.2)                      # let the stderr pump catch up
                why = "; ".join(self.err_tail) or f"exit code {self.proc.returncode}"
                raise RuntimeError(f"{self.mode} capture stopped: {why}")
            buf += chunk
        return np.frombuffer(bytes(buf), dtype="<f4").reshape(-1, self.channels)

    def __exit__(self, *exc):
        with contextlib.suppress(Exception):
            self.proc.terminate()
            self.proc.wait(3)
        with contextlib.suppress(Exception):
            self.proc.kill()


def _mac_default_names():
    """(default mic, default output) as shown by `audiocap list`."""
    out = subprocess.run([audiocap_path(), "list"], capture_output=True, text=True).stdout
    names = dict(re.findall(r"^Default (mic|speaker):\s+(.*)$", out, re.M))
    return names.get("mic", "default input"), names.get("speaker", "default output")


def _open_streams(sc):
    if sc is None:                                    # macOS
        mic_name, spk_name = _mac_default_names()
        mic_dev = _HelperSource("mic", f"{MIC_NAME} (match)" if MIC_NAME else mic_name, MIC_NAME)
        loop_dev = _HelperSource("system", "system audio")
        return mic_dev, loop_dev, _HelperSource("system", f"process tap on all output (now: {spk_name})")
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
    desync over a long meeting). A failure is queued too, so the mixer raises
    it instead of waiting forever on a stream that has died."""
    try:
        with recorder_ctx as rec:
            while not stop_event.is_set():
                q.put(rec.record(numframes=BLOCK))
    except Exception as e:
        if not stop_event.is_set():
            q.put(e)


def _take(q, timeout):
    item = q.get(timeout=timeout)
    if isinstance(item, Exception):
        raise item
    return item


def record_session(wav_path, stop_event, selftest_seconds=None, mic_gain=MIC_GAIN):
    """Capture mic + system loopback on separate threads, mix, write a 16 kHz
    mono 16-bit WAV. Memory stays flat: each block is downsampled and written
    immediately. Blocks are paired 1:1, so the two streams stay in sync."""
    if MACOS:
        sc = None
    else:
        import soundcard as sc
    mic_dev, loop_dev, spk = _open_streams(sc)
    print(f"Mic:    {mic_dev.name}  (gain x{mic_gain:g})")
    print(f"System: {spk.name}" + ("" if MACOS else " (loopback)"))
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
        # The streams rarely start at the same instant (on macOS the very first
        # run even waits for a permission prompt). Pair them from the moment
        # both are live, dropping whatever the early one captured before that.
        mic = sysd = None
        while not stop_event.is_set() and (mic is None or sysd is None):
            with contextlib.suppress(queue.Empty):
                mic = mic if mic is not None else _take(mic_q, 0.2)
                sysd = sysd if sysd is not None else _take(sys_q, 0.2)
        if mic is not None and sysd is not None:
            while mic_q.qsize() > 1 and not isinstance(mic_q.queue[0], Exception):
                mic = mic_q.get()
            while sys_q.qsize() > 1 and not isinstance(sys_q.queue[0], Exception):
                sysd = sys_q.get()
            _mix_write(mic, sysd)
        mic = None
        t0 = time.monotonic()
        while not stop_event.is_set():
            try:
                if mic is None:               # keep a mic block while its partner is late
                    mic = _take(mic_q, 1.0)
                sysd = _take(sys_q, 1.0)
            except queue.Empty:
                continue
            _mix_write(mic, sysd)
            mic = None
            if selftest_seconds is not None and time.monotonic() - t0 >= selftest_seconds:
                break
    finally:
        cap_stop.set()
        # drain whatever both streams already captured, keeping them paired
        drained = [[x for x in list(q_.queue) if not isinstance(x, Exception)] for q_ in (mic_q, sys_q)]
        for m, s in zip(*drained):
            _mix_write(m, s)
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


# --- native Metal/MLX server (macOS, Apple Silicon) -----------------------
# Started exactly the way the dashboard's mlxServerManager does it: the app
# bundle's own venv python running uvicorn, with the dashboard's model choices
# and Hugging Face token passed in through the environment.
def _metal_backend_dir():
    """TranscriptionSuite's server/backend folder (with its .venv), or None."""
    apps = [TS_APP] if TS_APP else ["/Applications/TranscriptionSuite.app",
                                    os.path.expanduser("~/Applications/TranscriptionSuite.app")]
    for d in [os.path.join(a, "Contents", "Resources", "backend") for a in apps]:
        if os.path.isfile(os.path.join(d, ".venv", "bin", "python3")):
            return d
    return None


def server_mode():
    if SERVER_MODE in ("docker", "metal"):
        return SERVER_MODE
    return "metal" if MACOS and _metal_backend_dir() else "docker"


def _dashboard_settings():
    with contextlib.suppress(Exception), open(os.path.join(TS_DATA_DIR, "dashboard-config.json"), encoding="utf-8") as f:
        return json.load(f)
    return {}


def _metal_env():
    """Mirror of mlxServerManager.start() + main.ts's auto-start model resolution."""
    st = _dashboard_settings()
    data = os.path.join(TS_DATA_DIR, "data")
    for d in ("logs", "audio", "tokens"):
        os.makedirs(os.path.join(data, d), exist_ok=True)
    env = dict(os.environ, DATA_DIR=data, HF_HOME=os.path.join(TS_DATA_DIR, "models"),
               USER_CONFIG_DIR=os.path.join(TS_DATA_DIR, "server-config"),
               LOG_DIR=os.path.join(data, "logs"), LOG_LEVEL="INFO", PYTHONUNBUFFERED="1")
    ffmpeg = os.path.join(os.path.dirname(_metal_backend_dir()), "ffmpeg")   # bundled fallback
    if os.path.isfile(os.path.join(ffmpeg, "ffmpeg")):
        env["PATH"] += os.pathsep + ffmpeg
    if st.get("server.hfToken"):
        env["HF_TOKEN"] = st["server.hfToken"]

    main = st.get("server.mainModelSelection") or "mlx-community/whisper-small-asr-fp16"
    env["MAIN_TRANSCRIBER_MODEL"] = main
    live_sel = st.get("server.liveModelSelection") or ""
    if live_sel in ("None (Disabled)", "__none__"):
        live = ""
    elif not live_sel or live_sel == "Same as Main Transcriber":
        live = main
    elif live_sel == "Custom (HuggingFace repo)":
        live = st.get("server.liveCustomModel") or main
    else:
        live = live_sel
    if live and re.search("mlx", live, re.I):
        live = "Systran/faster-whisper-medium"       # live mode can't run MLX models
    if live:
        env["LIVE_TRANSCRIBER_MODEL"] = live
    diar_sel = st.get("server.diarizationModelSelection") or ""
    if diar_sel == "Custom (HuggingFace repo)":
        diar = (st.get("server.diarizationCustomModel") or "").strip()
    elif diar_sel in ("", "CAM++ (fast, built-in)", "Sortformer (Metal; ≤ 4 speakers)", "Auto (best available)"):
        diar = ""                                    # let the server pick
    elif diar_sel.lower() == "pyannote/speaker-diarization-community-1":
        diar = ""                                    # the server-config overlay decides
    else:
        diar = diar_sel
    if diar:
        env["DIARIZATION_MODEL"] = diar
    return env


def _metal_pid():
    """PID of a Metal server we started (possibly in an earlier run), or None."""
    with contextlib.suppress(Exception):
        with open(METAL_PIDFILE) as f:
            pid = int(f.read().split()[0])
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True).stdout
        if "server.api.main" in cmd:                 # guard against a recycled pid
            return pid
    return None


_metal_proc = None


def _metal_up():
    global _metal_proc
    backend = _metal_backend_dir()
    if not backend:
        raise RuntimeError("TranscriptionSuite's Metal server isn't installed. Install the "
                           "arm64-mac-metal build of TranscriptionSuite in /Applications "
                           "(or set TRANSCRIPTION_SUITE_APP).")
    log = open(METAL_LOG, "a", buffering=1, encoding="utf-8")
    log.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} starting Metal server =====\n")
    _metal_proc = subprocess.Popen(
        [os.path.join(backend, ".venv", "bin", "python3"), "-m", "uvicorn", "server.api.main:app",
         "--host", "127.0.0.1", "--port", str(urllib.parse.urlparse(SERVER).port or 9786)],
        # Run from Resources/: the packaged backend lives in site-packages, so the
        # server only finds its bundled defaults (Resources/config.yaml) through
        # its current-directory fallback.
        cwd=os.path.dirname(backend), env=_metal_env(),
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True)                     # its own process group; we stop it explicitly
    log.close()
    with open(METAL_PIDFILE, "w") as f:
        f.write(f"{_metal_proc.pid}\n")


def _metal_failed():
    """Why a Metal server we're starting has already died, else None."""
    if _metal_proc is None or _metal_proc.poll() is None:
        return None
    tail = ""
    with contextlib.suppress(Exception), open(METAL_LOG, encoding="utf-8", errors="replace") as f:
        tail = "\n".join(f.read().strip().splitlines()[-6:])
    return f"Metal server exited with code {_metal_proc.returncode}:\n{tail}\n(full log: {METAL_LOG})"


def _metal_down(timeout=SERVER_STOP_TIMEOUT):
    global _metal_proc
    pid = _metal_proc.pid if _metal_proc and _metal_proc.poll() is None else _metal_pid()
    if pid is None:
        # Not ours: an explicit --server-down still stops a running native server.
        out = subprocess.run(["pgrep", "-f", "uvicorn server.api.main:app"],
                             capture_output=True, text=True).stdout.split()
        pid = int(out[0]) if out else None
    if pid is not None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.5)
        if _pid_alive(pid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
    if _metal_proc is not None:
        with contextlib.suppress(Exception):
            _metal_proc.wait(5)                     # reap it
    _metal_proc = None
    with contextlib.suppress(FileNotFoundError):
        os.remove(METAL_PIDFILE)


def _pid_alive(pid):
    if _metal_proc is not None and _metal_proc.pid == pid:
        return _metal_proc.poll() is None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _backend_up():
    if server_mode() == "metal":
        _metal_up()
    else:
        _compose("up", "-d", timeout=300)


def _backend_down():
    if server_mode() == "metal":
        _metal_down()
    else:
        _compose("down", timeout=SERVER_STOP_TIMEOUT)


_owned = False      # True while THIS process is the one keeping the stack up


def _disown():
    """Leave a server we started running on purpose (--server-up, --keep-server):
    it's no longer ours to stop, nor a leftover to adopt next time."""
    global _owned
    _owned = False
    with contextlib.suppress(FileNotFoundError):
        os.remove(METAL_PIDFILE)


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
    global _owned
    if server_healthy():
        if server_mode() == "metal" and _metal_pid():
            # Left behind by an earlier run of ours that died mid-job: adopt it,
            # so it's stopped again like any server we started.
            say("Reusing the transcription server a previous run left behind.")
            _owned = True
            return True
        say("Transcription server already running — leaving it alone.")
        return False
    say("Starting transcription server…")
    _backend_up()
    _owned = True
    deadline = time.monotonic() + timeout
    last = 0.0
    while time.monotonic() < deadline:
        if server_healthy():
            say("Transcription server is up.")
            return True
        failed = _metal_failed() if server_mode() == "metal" else None
        if failed:
            with contextlib.suppress(Exception):
                stop_server()
            raise RuntimeError(failed)
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
    _backend_down()
    _owned = False
    say("Transcription server stopped.")


@contextlib.contextmanager
def transcription_server(progress=None, manage=True, keep_up=False):
    """Own the server only for the duration of the block.

    manage=False leaves the server entirely alone (already handled elsewhere).
    keep_up=True starts it if needed but never stops it."""
    if not manage:
        yield False
        return
    started = start_server(progress)
    try:
        yield started
    finally:
        if keep_up:
            _disown()                       # deliberately left running
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


# ------------------------------------------------------- tray job (both trays)
def stamp_from_wav(wav):
    """Use the WAV's own filename (which is a recording timestamp) as the base
    for the transcript files, so text stays associated with its audio."""
    base = os.path.splitext(os.path.basename(wav))[0]
    return base or _ts()


def transcription_job(wav, title, speakers, diar, lang, progress):
    """The tray's whole "Transcribe" action: start the server, transcribe, stop
    it again, write the transcript. Never raises. Returns the tray's next UI step:
      ("result", {ok, title, body, folder})  a failure to show, or
      ("name",   {txt, then})                the who-is-who dialog, then `then`."""
    # The server stack only exists for the length of this function: started here,
    # torn down in the context manager's finally — including on every failure path
    # below, which is why they all live inside the `with`.
    try:
        with transcription_server(progress):
            try:
                progress("Transcribing…")
                result = transcribe_file(wav, language=lang, diarization=diar,
                                         expected_speakers=speakers)
            except requests.exceptions.ConnectionError:
                return ("result", {"ok": False, "title": "Server unreachable",
                    "body": "The server came up but then dropped the connection on :9786.\n\n"
                            "Your audio is safe — retry via\n"
                            "the tray menu → “Transcribe an existing WAV…”:\n" + wav,
                    "folder": AUDIO_DIR})
            except (requests.exceptions.ReadTimeout, TimeoutError):
                return ("result", {"ok": False, "title": "Transcription timed out",
                    "body": "The server didn't finish in time.\n\nYour audio is safe:\n" + wav,
                    "folder": AUDIO_DIR})
            except Exception as e:
                return ("result", {"ok": False, "title": "Transcription failed",
                    "body": f"{type(e).__name__}: {e}\n\nYour audio is safe:\n" + wav,
                    "folder": AUDIO_DIR})
    except Exception as e:
        # only reachable from the bring-up itself; teardown failures are swallowed
        return ("result", {"ok": False, "title": "Server didn't start",
            "body": f"{type(e).__name__}: {e}\n\nYour audio is safe — retry via\n"
                    "the tray menu → “Transcribe an existing WAV…”:\n" + wav,
            "folder": AUDIO_DIR})

    # a 200 with no words is a failure, not a success — don't write blank files
    text = (result.get("text") or "").strip()
    segs = result.get("segments") or []
    if not text and not segs:
        return ("result", {"ok": False, "title": "Empty transcript",
            "body": "The server returned no words (silence, or wrong language?).\n"
                    "Nothing was written.\n\nAudio:\n" + wav, "folder": AUDIO_DIR})

    try:
        label = ("_" + title.replace(" ", "-")) if title else ""
        base = os.path.join(OUTPUT_DIR, f"{stamp_from_wav(wav)}{label}")
        txt = write_outputs(result, base)
    except Exception as e:
        return ("result", {"ok": False, "title": "Could not write transcript",
            "body": f"{type(e).__name__}: {e}"})

    spk = result.get("num_speakers")
    done = {"ok": True, "title": "Transcript ready",
        "body": f"{os.path.basename(txt)}\n{len(segs)} segments · "
                f"{spk} speaker(s) · {result.get('language')} · {result.get('duration')}s",
        "folder": OUTPUT_DIR}
    return ("name", {"txt": txt, "then": done})


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
        start_server()
        _disown()               # explicit "up" means leave it up after we exit
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
