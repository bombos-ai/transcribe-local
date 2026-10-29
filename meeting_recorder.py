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
    python meeting_recorder.py --selftest [S]  # record S (default 5) s and report mic/system levels
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

Settings come from environment variables or a .env file next to this script
(see .env.example).

Deps:  pip install -r requirements.txt   (plus the `docker` CLI on PATH for Docker mode)
"""
import argparse, atexit, collections, contextlib, ctypes, datetime, os, re, signal, subprocess, sys, threading, time, urllib.parse, wave, json, warnings
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
CAPTURE_SR  = 48000          # WASAPI native; the macOS helper resamples to it
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
MIC_NAME    = None           # substring to pin a mic; None = the meeting app's mic, else the default input

# The transcription server is a Docker Compose stack that we only keep running
# while we actually need it (it holds a GPU and several GB of RAM once the
# models load). COMPOSE_DIR is TranscriptionSuite's compose directory: the
# dashboard's own (whose .env pins COMPOSE_FILE and COMPOSE_PROJECT_NAME), or a
# manual checkout. TRANSCRIPTION_COMPOSE_FILES (separated by ';' on Windows)
# picks the -f files explicitly, for a checkout whose .env doesn't pin them.
MACOS       = sys.platform == "darwin"
# The dashboard's app-data folder (Electron userData).
TS_DATA_DIR = (os.path.expanduser("~/Library/Application Support/TranscriptionSuite") if MACOS
               else os.path.join(os.environ.get("APPDATA", ""), "TranscriptionSuite"))
COMPOSE_DIR = os.environ.get("TRANSCRIPTION_COMPOSE_DIR") or next(
    (d for d in (os.path.join(TS_DATA_DIR, "docker"),
                 os.path.join(os.path.expanduser("~"), "TranscriptionSuite", "docker"))
     if os.path.isdir(d)),
    os.path.join(TS_DATA_DIR, "docker"))
COMPOSE_FILES = [f for f in (os.environ.get("TRANSCRIPTION_COMPOSE_FILES") or "").split(os.pathsep) if f]
SERVER_START_TIMEOUT = 900   # first start bootstraps a venv + models; be generous
SERVER_STOP_TIMEOUT  = 180   # stop_grace_period is 130s
# CPU-only servers are slow on long meetings, so allow plenty of time.
SERVER_TIMEOUT = 4 * 3600

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


# Keep the machine awake WHILE recording and WHILE transcribing, so a
# lock-then-sleep can't cut the capture off, and Modern Standby can't throttle
# the server's Docker VM to a crawl mid-transcription (a 2.5 h file once spent
# 3+ hours of its timeout in standby). The screen may still turn off and lock —
# we only block system sleep, not the display. A recording and a transcription
# running side by side mustn't clear each other: on Windows the flag is per
# thread; on macOS one process-wide `caffeinate` is shared, so it's counted.
_ES_CONTINUOUS       = 0x80000000
_ES_SYSTEM_REQUIRED  = 0x00000001
_caffeinate = None           # macOS: a `caffeinate` child holding the assertion
_awake_users = 0             # macOS: callers currently wanting it held
_awake_lock = threading.Lock()
def _keep_awake(on):
    global _caffeinate, _awake_users
    if MACOS:
        with _awake_lock:
            _awake_users = _awake_users + 1 if on else max(_awake_users - 1, 0)
            if _awake_users and _caffeinate is None:
                with contextlib.suppress(Exception):
                    # -i: no idle sleep; -w: let go by itself if we die
                    _caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            elif not _awake_users and _caffeinate is not None:
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
    devs = sc.all_microphones(include_loopback=True)
    busy = _windows_busy_endpoints() or set()
    defaults = set()
    for get in (sc.default_microphone, sc.default_speaker):
        with contextlib.suppress(Exception):
            defaults.add(get().id)

    def show(d):
        print(f"  {d.name}" + ("  [default]" if d.id in defaults else "")
              + ("  [in use by another app]" if d.id in busy else ""))
    print("=== Microphones (recorded: MIC_NAME if set, else the one another app uses, else the default) ===")
    for d in devs:
        if not d.isloopback:
            show(d)
    print("\n=== Outputs (all recorded as 'system', via loopback; Hands-Free ones only while in use) ===")
    for d in devs:
        if d.isloopback:
            show(d)


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

    def recorder(self, samplerate, channels, blocksize=None):
        # blocksize is soundcard's WASAPI buffer size; the helper's pipe buffers for us
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


# ---------------------------------------------------------------- capture
# Devices come and go during a meeting: a headset connects or drops out, the
# meeting app moves to another mic, a monitor with speakers goes to sleep. So
# nothing is fixed when recording starts. Every stream (a mic, one output's
# loopback, a macOS helper) runs in its own thread and adds its audio into a
# shared timeline at the wall-clock time it was captured, and a supervisor
# starts and stops streams as devices appear and disappear. The WAV is written
# from that timeline a few seconds behind real time, so a stream that stalls,
# restarts or switches device leaves at most a short silence in its own
# channel, and mic and system stay aligned throughout.
#
# What gets recorded:
#   system  Windows: the loopback of EVERY output, summed, so it doesn't matter
#           which device Teams/Discord/WhatsApp plays through. (A Bluetooth
#           headset's "Hands-Free" output only while something plays to it.)
#           macOS: the helper's process tap, which already covers all output.
#   mic     MIC_NAME if that mic is present; else the mic another app (the
#           meeting) is recording from, whichever device that is; else the
#           system default input (e.g. recording a room, no meeting).
TIMELINE_LAG = 3.0           # s the WAV trails real time, so late blocks still land in place
SYNC_SKEW    = 0.1           # s a stream may drift from its wall-clock position before it's re-aligned
SYNC_WINDOW  = 10            # blocks (1 s) of arrival delays to judge that on
STALL_SECS   = 5.0           # s without a block before a running stream counts as hung
DEVICE_POLL  = 1.0           # s between device checks
MIC_SETTLE   = 2.0           # s a new mic choice must hold before we switch (apps probe devices briefly)
MIC, SYSTEM  = 0, 1          # timeline / WAV channels
_CH_NAMES    = ("mic", "system")


class _Timeline:
    """Two channels (MIC, SYSTEM) at TARGET_SR, indexed by frames since t0 on
    the monotonic clock. Streams add into it; the writer pops finished frames
    off the front. Wherever nothing was added is silence."""

    def __init__(self, t0):
        self.t0 = t0
        self.lock = threading.Lock()
        self.base = 0                             # frame index of buf[0]
        self.buf = np.zeros((0, 2), np.float32)
        self.end = 0                              # one past the last frame anything wrote

    def frame(self, t):
        return int(round((t - self.t0) * TARGET_SR))

    def add(self, ch, pos, data):
        with self.lock:
            if pos < self.base:                   # partly older than what's already written
                data, pos = data[self.base - pos:], self.base
            if not len(data) or pos - self.base > 60 * TARGET_SR:
                return
            i, j = pos - self.base, pos - self.base + len(data)
            if j > len(self.buf):
                self.buf = np.concatenate(
                    [self.buf, np.zeros((max(j - len(self.buf), TARGET_SR), 2), np.float32)])
            self.buf[i:j, ch] += data
            self.end = max(self.end, pos + len(data))

    def pop(self, upto):
        """Frames [base, upto)."""
        with self.lock:
            n = upto - self.base
            if n <= 0:
                return np.zeros((0, 2), np.float32)
            out = self.buf[:n]
            if len(out) < n:
                out = np.concatenate([out, np.zeros((n - len(out), 2), np.float32)])
            self.buf = self.buf[n:]
            self.base = upto
            return out


class _Source:
    """One capture stream in its own thread. Each block goes on the timeline
    where it was captured: its arrival time minus its length. Arrival can only
    be late, never early, so a block that arrives before its slot means the
    stream sits too late (drop the overlap), and a stream whose blocks ALL
    arrived late for a whole SYNC_WINDOW has lost samples (skip ahead).
    Ordinary jitter is left alone, so the audio stays contiguous."""

    def __init__(self, timeline, ch, label, device, channels):
        self.tl, self.ch, self.label = timeline, ch, label
        self.device, self.channels = device, channels
        self.stop = threading.Event()
        self.error = None
        self.rec = None
        self.pos = None                           # timeline frame for the next block
        self.last = None                          # when the last block arrived
        self.delays = collections.deque(maxlen=SYNC_WINDOW)
        self.started_at = time.monotonic()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            with self.device.recorder(samplerate=CAPTURE_SR, channels=self.channels,
                                      blocksize=CAPTURE_BUFFER) as rec:
                self.rec = rec
                while not self.stop.is_set():
                    block = rec.record(numframes=BLOCK)
                    if not self.stop.is_set():
                        self._place(block, time.monotonic())
        except Exception as e:                    # device gone, helper died, …: the supervisor decides
            if not self.stop.is_set():
                self.error = e

    def _place(self, block, arrived):
        self.last = arrived
        x = block.mean(axis=1)
        n = (len(x) // DECIM) * DECIM
        x = x[:n].reshape(-1, DECIM).mean(axis=1)
        end = self.tl.frame(arrived)
        if self.pos is None:
            self.pos = end - len(x)
        delay = end - (self.pos + len(x))
        skew = int(SYNC_SKEW * TARGET_SR)
        if delay < -skew:
            x = x[-delay:]
            self.delays.clear()
        else:
            self.delays.append(delay)
            if len(self.delays) == SYNC_WINDOW and min(self.delays) > skew:
                self.pos += min(self.delays)
                self.delays.clear()
        self.tl.add(self.ch, self.pos, x)
        self.pos += len(x)


class _Capture:
    """Keeps one _Source running per wanted stream. `discover()` says what's
    wanted right now, as {key: (channel, label, device, channels)}; a stream
    whose device has gone is stopped, a new device gets a stream, and one that
    died or hung is retried for as long as its device is still wanted."""

    def __init__(self, timeline, discover):
        self.tl, self.discover = timeline, discover
        self.sources = {}                         # key -> _Source
        self.retired = []                         # stopped, possibly still winding down
        self.retry = {}                           # key -> (not before, backoff s)
        self.wanted = {}

    def poll(self, rediscover=True):
        now = time.monotonic()
        ended = []
        for key, src in list(self.sources.items()):
            if src.thread.is_alive():
                if src.last is None or now - src.last < STALL_SECS:
                    continue
                src.error = TimeoutError(f"no audio for {now - src.last:.0f}s")
                src.stop.set()
                self.retired.append(src)
            del self.sources[key]
            ended.append((key, src))
        if rediscover or ended:           # a stream that ended may mean its device is gone: look now
            try:
                self.wanted = self.discover()
            except Exception as e:
                print(f"  ! device check failed ({type(e).__name__}: {e}); keeping the current devices")
        for key, src in ended:
            why = f"{type(src.error).__name__}: {src.error}"
            if key not in self.wanted:
                print(f"  - {_CH_NAMES[src.ch]}: {src.label} (gone: {why})")
                continue
            # One that ran a while is restarted at once; one that keeps
            # failing to open backs off, up to 10 s.
            wait = (0.0 if now - src.started_at > 10
                    else min(max(self.retry.get(key, (0, 0.0))[1], 0.5) * 2, 10.0))
            self.retry[key] = (now + wait, wait)
            print(f"  ! {_CH_NAMES[src.ch]} {src.label} stopped ({why}); "
                  + (f"retrying in {wait:g}s" if wait else "restarting"))
        for key, (ch, label, device, channels) in self.wanted.items():
            if key not in self.sources and self.retry.get(key, (0,))[0] <= now:
                self.sources[key] = _Source(self.tl, ch, label, device, channels)
                print(f"  + {_CH_NAMES[ch]}: {label}")
        # A replaced mic keeps recording until its successor delivers, so a
        # switch never leaves a hole; anything else no longer wanted stops now.
        mic_wanted = any(w[0] == MIC for w in self.wanted.values())
        mic_live = any(s.pos is not None for k, s in self.sources.items()
                       if s.ch == MIC and k in self.wanted)
        for key, src in list(self.sources.items()):
            if key in self.wanted or (src.ch == MIC and mic_wanted and not mic_live):
                continue
            src.stop.set()
            del self.sources[key]
            self.retired.append(src)
            print(f"  - {_CH_NAMES[src.ch]}: {src.label}")
        for key in [k for k in self.retry if k not in self.wanted]:
            del self.retry[key]
        self.retired = [s for s in self.retired if s.thread.is_alive()]

    def close(self):
        everything = list(self.sources.values()) + self.retired
        for src in everything:
            src.stop.set()
        deadline = time.monotonic() + 2.0
        for src in everything:
            src.thread.join(max(0.0, deadline - time.monotonic()))
            if src.thread.is_alive() and isinstance(src.rec, _HelperRecorder):
                src.rec.__exit__(None, None, None)    # unblock a helper stuck mid-read
        self.sources, self.retired = {}, []


class _MicPicker:
    """Which mic to record: MIC_NAME when that mic is present; else the mic
    another app is recording from (the meeting app's, whichever device it was
    set to; the default if that's among them); else the default input. A new
    choice must hold for MIC_SETTLE s before it's taken, unless the current
    mic has disappeared."""

    def __init__(self):
        self.cur = self.pending = None
        self.since = 0.0

    def pick(self, names, default, busy):
        """names: {id: name} of the mics present now; default: the default
        input's id, or None; busy: ids another process is recording from.
        Returns the id to record, or None when there's no mic at all."""
        want = None
        if MIC_NAME:
            want = next((i for i, n in names.items() if MIC_NAME.lower() in n.lower()), None)
        if want is None:
            in_use = sorted(i for i in names if i in busy)
            want = next((i for i in (self.cur, default, *in_use) if i in in_use), None)
        if want is None:
            want = default if default in names else next(iter(sorted(names)), None)
        now = time.monotonic()
        if want == self.cur or self.cur not in names:
            self.cur, self.pending = want, None
        elif want != self.pending:
            self.pending, self.since = want, now
        elif now - self.since >= MIC_SETTLE:
            self.cur, self.pending = want, None
        return self.cur


# ------------------------------------------------- Windows device discovery
def _windows_busy_endpoints():
    """Endpoint ids of the devices, inputs and outputs, that another process
    is streaming on right now (in practice: the meeting app's mic and
    speaker), from the WASAPI audio sessions on each device. None if that
    can't be determined."""
    from ctypes import POINTER, byref, c_int, c_uint, c_void_p, c_wchar_p

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16),
                    ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]

    def guid(s):
        g = GUID()
        ctypes.oledll.ole32.CLSIDFromString(s, byref(g))
        return g

    def call(obj, index, argtypes, *args):        # COM method by vtable slot; failures raise OSError
        fn = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0][index]
        return ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(fn)(obj, *args)

    def release(obj):
        if obj:
            fn = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0][2]
            ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(fn)(obj)

    CLSCTX_ALL, eAll, DEVICE_STATE_ACTIVE, AudioSessionStateActive = 23, 2, 1, 1
    ole32 = ctypes.windll.ole32
    ole32.CoTaskMemFree.argtypes = [c_void_p]
    hr = ole32.CoInitializeEx(None, 0)            # MTA, like soundcard; S_FALSE / changed mode are fine
    busy, held = set(), []
    try:
        enum, coll, count = c_void_p(), c_void_p(), c_uint()
        ctypes.oledll.ole32.CoCreateInstance(
            byref(guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")), None, CLSCTX_ALL,
            byref(guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")), byref(enum))
        held.append(enum)
        call(enum, 3, (c_uint, c_uint, POINTER(c_void_p)), eAll, DEVICE_STATE_ACTIVE, byref(coll))
        held.append(coll)
        call(coll, 3, (POINTER(c_uint),), byref(count))
        iid_mgr2 = guid("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
        iid_ctl2 = guid("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
        for i in range(count.value):
            objs = []
            try:
                dev, idp = c_void_p(), c_wchar_p()
                call(coll, 4, (c_uint, POINTER(c_void_p)), i, byref(dev))
                objs.append(dev)
                call(dev, 5, (POINTER(c_wchar_p),), byref(idp))
                dev_id = idp.value
                ole32.CoTaskMemFree(ctypes.cast(idp, c_void_p))
                mgr, sessions, n = c_void_p(), c_void_p(), c_int()
                call(dev, 3, (POINTER(GUID), c_uint, c_void_p, POINTER(c_void_p)),
                     byref(iid_mgr2), CLSCTX_ALL, None, byref(mgr))
                objs.append(mgr)
                call(mgr, 5, (POINTER(c_void_p),), byref(sessions))
                objs.append(sessions)
                call(sessions, 3, (POINTER(c_int),), byref(n))
                for j in range(n.value):
                    ctl, ctl2, state, pid = c_void_p(), c_void_p(), c_int(), c_uint()
                    try:
                        call(sessions, 4, (c_int, POINTER(c_void_p)), j, byref(ctl))
                        call(ctl, 3, (POINTER(c_int),), byref(state))
                        if state.value != AudioSessionStateActive:
                            continue
                        call(ctl, 0, (POINTER(GUID), POINTER(c_void_p)), byref(iid_ctl2), byref(ctl2))
                        call(ctl2, 14, (POINTER(c_uint),), byref(pid))
                        if pid.value not in (0, os.getpid()):
                            busy.add(dev_id)
                    finally:
                        release(ctl2)
                        release(ctl)
            except OSError:
                pass                              # one odd device mustn't hide the others
            finally:
                for o in reversed(objs):
                    release(o)
        return busy
    except OSError:
        return None
    finally:
        for o in reversed(held):
            release(o)
        if hr in (0, 1):
            ole32.CoUninitialize()


def _windows_discover():
    import soundcard as sc
    picker = _MicPicker()

    def default_id(get):
        try:
            return get().id
        except Exception:                         # no such device at all right now
            return None

    def discover():
        devs = sc.all_microphones(include_loopback=True)
        busy = _windows_busy_endpoints() or set()
        mics = {d.id: d for d in devs if not d.isloopback}
        mic = picker.pick({i: d.name for i, d in mics.items()}, default_id(sc.default_microphone), busy)
        wanted = {("mic", mic): (MIC, mics[mic].name, mics[mic], 1)} if mic is not None else {}
        # The loopback of every output, whichever one the meeting plays
        # through. Except a Bluetooth headset's Hands-Free output, opened only
        # while something plays to it: that output is the headset's call mode.
        speaker = default_id(sc.default_speaker)
        for d in devs:
            if d.isloopback and ("hands-free" not in d.name.lower() or d.id in busy or d.id == speaker):
                wanted[("system", d.id)] = (SYSTEM, f"{d.name} (loopback)", d, 2)
        return wanted
    return discover


# --------------------------------------------------- macOS device discovery
class _CAAddress(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


def _mac_inputs():
    """({device id: name} of the input devices, the default input's id, the ids
    another process is recording from), straight from Core Audio. The process
    objects (macOS 14+) are what say which mic the meeting app is using; our
    own capture helper is left out of that."""
    ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    u32, P = ctypes.c_uint32, ctypes.POINTER
    ca.AudioObjectGetPropertyDataSize.argtypes = [u32, P(_CAAddress), u32, ctypes.c_void_p, P(u32)]
    ca.AudioObjectGetPropertyData.argtypes = [u32, P(_CAAddress), u32, ctypes.c_void_p, P(u32), ctypes.c_void_p]
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, u32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]

    def get(obj, sel, ctype, scope=b"glob", many=False):
        addr = _CAAddress(int.from_bytes(sel, "big"), int.from_bytes(scope, "big"), 0)
        size = u32(0)
        if ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)):
            return [] if many else None
        n = size.value // ctypes.sizeof(ctype)
        buf = (ctype * max(n, 1))()
        if not n or ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size),
                                                  ctypes.cast(buf, ctypes.c_void_p)):
            return [] if many else None
        return list(buf[:n]) if many else buf[0]

    def name(dev):
        ref = get(dev, b"lnam", ctypes.c_void_p)
        if not ref:
            return f"device {dev}"
        s = ctypes.create_string_buffer(1024)
        cf.CFStringGetCString(ref, s, len(s), 0x08000100)      # kCFStringEncodingUTF8
        cf.CFRelease(ref)
        return s.value.decode("utf-8", "replace")

    def is_helper(pid):
        path = ctypes.create_string_buffer(4096)
        n = libproc.proc_pidpath(pid, path, len(path))
        return n > 0 and os.path.realpath(path.value.decode("utf-8", "replace")) == os.path.realpath(AUDIOCAP)

    SYS = 1                                       # kAudioObjectSystemObject
    names = {d: name(d) for d in get(SYS, b"dev#", u32, many=True)
             if get(d, b"stm#", u32, scope=b"inpt", many=True)}
    names = {d: n for d, n in names.items() if not n.startswith("transcribe-local")}
    default = get(SYS, b"dIn ", u32)
    busy = set()
    for proc in get(SYS, b"prs#", u32, many=True):              # kAudioHardwarePropertyProcessObjectList
        if not get(proc, b"piri", u32):                          # kAudioProcessPropertyIsRunningInput
            continue
        pid = get(proc, b"ppid", ctypes.c_int32)
        if pid is None or pid == os.getpid() or is_helper(pid):
            continue
        busy.update(d for d in get(proc, b"pdv#", u32, scope=b"inpt", many=True) if d in names)
    return names, default, busy


def _mac_discover():
    picker = _MicPicker()
    system = _HelperSource("system", "everything the Mac plays (process tap)")

    def discover():
        try:
            names, default, busy = _mac_inputs()
            # "Whatever the default input is" counts as a device of its own
            # (id 0, never a real one), so a change of default isn't a change
            # of mic: the helper follows the default itself, instantly.
            busy = {0 if d == default else d for d in busy}
            mic = picker.pick({0: "", **names}, 0, busy)
            device = names[mic] if mic else None
        except Exception:                         # can't inspect devices: the helper's own choice
            device = MIC_NAME
        label = device or "default input"
        return {("system",): (SYSTEM, system.name, system, 2),
                ("mic", device): (MIC, label, _HelperSource("mic", label, device), 1)}
    return discover


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
    """Record the mic and all system audio to a 16 kHz 16-bit STEREO WAV:
    left = mic, right = system, both unprocessed. Levels are balanced later, at
    transcription time (see mix_for_upload), so the raw recording can always be
    remixed. Devices may come and go at any point (see the capture section
    above). Memory stays flat: the WAV is written as the timeline fills."""
    _count_dropouts()
    discover = _mac_discover() if MACOS else _windows_discover()
    os.makedirs(os.path.dirname(wav_path), exist_ok=True)
    tl = _Timeline(time.monotonic())
    cap = _Capture(tl, discover)
    peak = [0.0, 0.0]
    frames = 0
    wf = wave.open(wav_path, "wb")
    wf.setnchannels(2); wf.setsampwidth(2); wf.setframerate(TARGET_SR)

    def write(st):
        nonlocal frames
        if not len(st):
            return
        for ch in (MIC, SYSTEM):
            peak[ch] = max(peak[ch], float(np.abs(st[:, ch]).max()))
        np.clip(st, -1.0, 1.0, out=st)
        wf.writeframes((st * 32767.0).astype("<i2").tobytes())
        frames += len(st)

    try:
        _keep_awake(True)                 # block system sleep for the duration
        print("● Recording…  (press Enter to stop)" if selftest_seconds is None
              else f"● Self-test: capturing {selftest_seconds:g}s…")
        next_check = 0.0
        while not stop_event.is_set():
            now = time.monotonic()
            if selftest_seconds is not None and now - tl.t0 >= selftest_seconds:
                break
            try:
                cap.poll(rediscover=now >= next_check)
            except Exception as e:        # never let device handling end a recording
                print(f"  ! capture supervisor: {type(e).__name__}: {e}")
            if now >= next_check:
                next_check = now + DEVICE_POLL
            write(tl.pop(tl.frame(now - TIMELINE_LAG)))
            stop_event.wait(0.1)
    finally:
        end = tl.frame(time.monotonic())
        cap.close()
        write(tl.pop(max(end, tl.end)))
        wf.close()
        _keep_awake(False)                # allow sleep again
    secs = frames / TARGET_SR
    peak_mic, peak_sys = peak
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


# Several transcriptions can run at once in the tray, so the server is
# reference-counted: the first user brings it up, the last one takes it down —
# and only if this process started it.
_srv_lock = threading.Lock()
_srv_users = 0              # transcriptions in flight in this process
_owned = False              # True while THIS process is the one keeping the server up
_stopping = None            # an in-flight background teardown (Popen or Thread)


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
        env["PATH"] = env.get("PATH", "") + os.pathsep + ffmpeg
    # The dashboard's token wins; else HF_TOKEN / HUGGINGFACE_TOKEN from our env or .env.
    tok = st.get("server.hfToken") or os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    if tok:
        env["HF_TOKEN"] = tok

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
        raise ServerStartError("TranscriptionSuite's Metal server isn't installed. Install the "
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


def _metal_down(timeout=SERVER_STOP_TIMEOUT, explicit=False):
    """Stop the Metal server we started: its whole process group (it runs in a
    session of its own). Only an explicit --server-down also stops one we didn't
    start (e.g. the dashboard's) — and then just that process, never its group,
    which may be the dashboard app's own."""
    global _metal_proc
    pid = _metal_proc.pid if _metal_proc and _metal_proc.poll() is None else _metal_pid()
    group = pid is not None
    if pid is None and explicit:
        out = subprocess.run(["pgrep", "-f", "uvicorn server.api.main:app"],
                             capture_output=True, text=True).stdout.split()
        pid = int(out[0]) if out else None

    def _signal(sig):
        with contextlib.suppress(ProcessLookupError):
            if group:
                os.killpg(os.getpgid(pid), sig)
            else:
                os.kill(pid, sig)

    if pid is not None:
        _signal(signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.5)
        if _pid_alive(pid):
            _signal(signal.SIGKILL)
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


def _backend_down(wait=True, explicit=False):
    """Stop the server. wait=False runs it in the background and remembers it in
    _stopping, so the next start_server waits for it to finish."""
    global _stopping
    if server_mode() == "metal":
        if wait:
            _metal_down(explicit=explicit)
        else:
            _stopping = threading.Thread(target=_metal_down, daemon=True)
            _stopping.start()
    elif wait:
        _compose("down", timeout=SERVER_STOP_TIMEOUT)
    else:
        _stopping = subprocess.Popen(_compose_cmd("down"), cwd=COMPOSE_DIR, env=_compose_env(),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=_NO_WINDOW)


def _disown():
    """Leave a server we started running on purpose (--server-up, --keep-server):
    it's no longer ours to stop, nor a leftover to adopt next time."""
    global _owned
    _owned = False
    with contextlib.suppress(FileNotFoundError):
        os.remove(METAL_PIDFILE)


def _stop_if_owned():
    """Last line of defence: if the process exits mid-transcription (tray Quit,
    Ctrl-C), don't leave the server — and the GPU — occupied. A background Metal
    teardown is a daemon thread that dies with us, so finish it first (a
    background `compose down` is a process of its own and carries on alone)."""
    if isinstance(_stopping, threading.Thread):
        _wait_for_stop()
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
            if isinstance(_stopping, threading.Thread):
                _stopping.join(timeout=SERVER_STOP_TIMEOUT)
            else:
                _stopping.wait(timeout=SERVER_STOP_TIMEOUT)
        _stopping = None


def start_server(progress=None, timeout=SERVER_START_TIMEOUT):
    """Bring the transcription server up and wait until /health answers.

    Returns True if WE started it (so the caller should stop it again), False if
    it was already running — someone else's session, e.g. the Electron
    dashboard, must not be torn down underneath them. Raises ServerStartError."""
    global _owned
    say = progress or (lambda m: print(m))
    _wait_for_stop()
    if server_healthy():
        if not _owned and server_mode() == "metal" and _metal_pid():
            # Left behind by an earlier run of ours that died mid-job: adopt it,
            # so it's stopped again like any server we started.
            say("Reusing the transcription server a previous run left behind.")
            _owned = True
            return True
        if not _owned:
            say("Transcription server already running — leaving it alone.")
        return False
    say("Starting transcription server…")
    try:
        _backend_up()
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
        failed = _metal_failed() if server_mode() == "metal" else None
        if failed:
            with contextlib.suppress(Exception):
                stop_server()
            raise ServerStartError(failed)
        now = time.monotonic()
        if now - last >= 30:
            last = now
            say(f"Waiting for the server to become healthy… ({int(deadline - now)}s left)")
        time.sleep(2)
    # Don't leave a half-started server behind on a failed bring-up.
    with contextlib.suppress(Exception):
        stop_server()
    raise ServerStartError(f"Transcription server did not become healthy within {timeout}s.")


def stop_server(progress=None, wait=True, explicit=False):
    """Stop the server. wait=False fires it off in the background: the server
    drains gracefully (up to ~130 s) and nothing needs to sit through that; the
    next start_server waits for it to finish. explicit=True (--server-down) also
    stops a native server this tool didn't start."""
    global _owned
    say = progress or (lambda m: print(m))
    say("Stopping transcription server…")
    _owned = False
    _backend_down(wait=wait, explicit=explicit)
    if wait:
        say("Transcription server stopped.")


@contextlib.contextmanager
def transcription_server(progress=None, manage=True, keep_up=False):
    """Have the server running for the duration of the block.

    manage=False leaves the server entirely alone (already handled elsewhere).
    keep_up=True starts it if needed but never stops it."""
    global _srv_users
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
                _disown()                       # deliberately left running
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


def inbox_copy_line(txt_path, title=""):
    """Write the inbox copy (if one is configured) for a tray; returns a line for
    its result dialog, or None when there's nothing to say."""
    if not INBOX_DIR:
        return None
    try:
        path = write_inbox_copy(txt_path, title)
    except Exception as e:
        return f"⚠ Inbox copy failed: {type(e).__name__}: {e}"
    return f"Inbox: {os.path.basename(path)}" if path else "Inbox: skipped (unreachable or no speakers)"


# ------------------------------------------------------- tray job (both trays)
def stamp_from_wav(wav):
    """Use the WAV's own filename (which is a recording timestamp) as the base
    for the transcript files, so text stays associated with its audio."""
    base = os.path.splitext(os.path.basename(wav))[0]
    return base or _ts()


def transcription_job(wav, title, speakers, diar, lang, progress):
    """The tray's whole "Transcribe" action: transcribe (transcribe_file starts
    the server if needed and takes it down again on every exit path), then write
    the transcript. Never raises. Returns the tray's next UI step:
      ("result", {ok, title, body, folder})      a failure to show, or
      ("name",   {txt, then, title, new=True})   the who-is-who dialog, then `then`."""
    audio_dir = os.path.dirname(wav)
    try:
        result = transcribe_file(wav, language=lang, diarization=diar,
                                 expected_speakers=speakers, progress=progress)
    except ServerStartError as e:
        return ("result", {"ok": False, "title": "Server didn't start",
            "body": f"{e}\n\nYour audio is safe — retry via\n"
                    "the tray menu → “Transcribe an existing recording…”:\n" + wav,
            "folder": audio_dir})
    except requests.exceptions.ConnectionError:
        return ("result", {"ok": False, "title": "Server unreachable",
            "body": "Lost the connection to the transcription server on :9786\n"
                    "(did it crash or run out of memory?).\n\nYour audio is safe — retry via\n"
                    "the tray menu → “Transcribe an existing recording…”:\n" + wav,
            "folder": audio_dir})
    except (requests.exceptions.ReadTimeout, TimeoutError):
        return ("result", {"ok": False, "title": "Transcription timed out",
            "body": "The server didn't finish in time.\n\nYour audio is safe:\n" + wav,
            "folder": audio_dir})
    except Exception as e:
        return ("result", {"ok": False, "title": "Transcription failed",
            "body": f"{type(e).__name__}: {e}\n\nYour audio is safe:\n" + wav,
            "folder": audio_dir})

    # a 200 with no words is a failure, not a success — don't write blank files
    text = (result.get("text") or "").strip()
    segs = result.get("segments") or []
    if not text and not segs:
        return ("result", {"ok": False, "title": "Empty transcript",
            "body": "The server returned no words (silence, or wrong language?).\n"
                    "Nothing was written.\n\nAudio:\n" + wav, "folder": audio_dir})

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
    return ("name", {"txt": txt, "then": done, "title": title, "new": True})


def main():
    ap = argparse.ArgumentParser(description="Record a meeting and transcribe via the TranscriptionSuite server.")
    ap.add_argument("--title", default="", help="label for the output filenames")
    ap.add_argument("--file", help="transcribe an existing recording (WAV, MP3, …) instead of recording")
    ap.add_argument("--language", default=LANGUAGE)
    ap.add_argument("--speakers", type=int, default=None, help="expected number of speakers (hint for diarization)")
    ap.add_argument("--no-diarization", action="store_true")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--selftest", nargs="?", const=5.0, type=float, metavar="SECONDS",
                    help="record SECONDS (default 5) and report mic/system levels")
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
        start_server()
        _disown()               # explicit "up" means leave it up after we exit
        return
    if args.server_down:
        stop_server(explicit=True); return

    if args.list_devices:
        list_devices(); return
    os.makedirs(OUTPUT_DIR, exist_ok=True); os.makedirs(AUDIO_DIR, exist_ok=True)
    label = ("_" + args.title.replace(" ", "-")) if args.title else ""

    if args.selftest is not None:
        wav = os.path.join(AUDIO_DIR, f"selftest_{_ts()}.wav")
        record_session(wav, threading.Event(), selftest_seconds=args.selftest); return

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
