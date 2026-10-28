#!/usr/bin/env python3
"""
meeting_tray.py — a system-tray button for meeting_recorder.py.

Left-click the tray icon to START recording; left-click again to STOP.
On stop, a small popup asks for a title and the number of speakers, then the
audio is sent to the TranscriptionSuite server and the diarized transcript is
written to the output folder (see meeting_recorder.OUTPUT_DIR) — no terminal,
no typing python commands.

The icon is grey when idle, red while recording and amber while transcribing.

Run it with pythonw.exe so there's no console window:
    pythonw meeting_tray.py

Deps: pip install soundcard numpy requests pystray pillow   (tkinter ships with Python)
"""
import os, sys, time, threading, queue

if sys.platform == "darwin":          # the macOS menu-bar app is its own module
    import runpy
    runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "meeting_tray_mac.py"),
                   run_name="__main__")
    sys.exit(0)

# --- make this importable no matter where it's launched from, and give the
#     (console-less) pythonw process somewhere for prints to go BEFORE we import
#     meeting_recorder, which writes to sys.stdout at import time. ------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    _logf = open(os.path.join(_HERE, "tray.log"), "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = _logf
    sys.stderr = _logf
except Exception:
    pass

# --- single instance: a named mutex. (Was a localhost port bind, but Windows
#     reserves random port ranges for Hyper-V/Docker at boot; when 49731 landed in
#     one the bind failed and the tray silently exited as if already running.)
import ctypes
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = ctypes.c_void_p
_guard = _k32.CreateMutexW(None, False, "Local\\MeetingRecorderTray")
if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
    sys.exit(0)

import tkinter as tk
from tkinter import ttk, filedialog
import pystray
from PIL import Image, ImageDraw
import meeting_recorder as mr


# ---------------------------------------------------------------- shared state
state = {"recording": False, "stop_event": None, "rec_thread": None,
         "wav": None, "started": 0.0, "busy": 0, "status": ""}   # busy = transcriptions running
lock = threading.Lock()
ui_q = queue.Queue()          # tray thread -> tk main thread requests
icon = None
root = None


# ---------------------------------------------------------------- tray icon art
def make_image(recording, busy=False):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if recording:
        d.ellipse((10, 10, 54, 54), fill=(224, 48, 48, 255))          # red disc
    elif busy:
        d.ellipse((10, 10, 54, 54), fill=(232, 150, 20, 255))         # amber: working
    else:
        d.ellipse((12, 12, 52, 52), outline=(150, 158, 168, 255), width=5)
        d.ellipse((26, 26, 38, 38), fill=(150, 158, 168, 255))         # grey mic dot
    return img


def refresh_icon():
    if icon is None:
        return
    rec, busy = state["recording"], state["busy"]
    icon.icon = make_image(rec, busy)
    if rec:
        icon.title = "Meeting recorder — RECORDING"
    elif busy:
        icon.title = "Meeting recorder — " + (state["status"] or "working…")
    else:
        icon.title = "Meeting recorder — idle"
    try:
        icon.update_menu()
    except Exception:
        pass


def notify(title, msg):
    try:
        icon.notify(msg, title)
    except Exception:
        pass


# ---------------------------------------------------------------- record control
def start_recording():
    with lock:
        if state["recording"]:
            return
        os.makedirs(mr.AUDIO_DIR, exist_ok=True)
        wav = os.path.join(mr.AUDIO_DIR, f"{mr._ts()}.wav")
        ev = threading.Event()
        th = threading.Thread(target=record_thread_main, args=(wav, ev), daemon=True)
        state.update(recording=True, stop_event=ev, rec_thread=th, wav=wav, started=time.monotonic())
    th.start()
    refresh_icon()
    notify("Recording started", "Click the tray icon again to stop.")


def record_thread_main(wav, ev):
    """Run the capture. On ANY crash, reset state and surface a dialog instead of
    dying silently and leaving the icon stuck red. On a clean stop, hand the audio
    (with its peak levels) to the tk thread for the title/speakers popup."""
    try:
        wav_, secs, peak_mic, peak_sys = mr.record_session(wav, ev)   # blocks until ev set
    except Exception as e:
        with lock:
            state["recording"] = False
        refresh_icon()
        ui_q.put(("result", {"ok": False, "title": "Recording crashed",
                             "body": f"{type(e).__name__}: {e}\n\nAudio may be incomplete:\n{wav}"}))
        return
    with lock:
        state["recording"] = False
    refresh_icon()
    ui_q.put(("ask", {"wav": wav_, "secs": secs, "peak_mic": peak_mic, "peak_sys": peak_sys}))


def stop_recording():
    """Signal the capture thread to finish; record_thread_main pops the dialog
    when it returns (and flips the icon back to idle)."""
    with lock:
        ev = state["stop_event"] if state["recording"] else None
    if ev:
        ev.set()


# ---------------------------------------------------------------- result dialog
def show_result(ok, title, body, folder=None):
    """A visible, guaranteed outcome dialog (tray balloons are unreliable here).
    Success auto-closes after 15 s; failures stay until dismissed."""
    win = tk.Toplevel(root)
    win.title(title)
    win.resizable(False, False)
    win.attributes("-topmost", True)
    glyph = "✓" if ok else "⚠"
    color = "#137333" if ok else "#c5221f"
    tk.Label(win, text=glyph, font=("Segoe UI", 22), fg=color).grid(
        row=0, column=0, rowspan=2, padx=(16, 8), pady=16)
    tk.Label(win, text=title, font=("Segoe UI", 11, "bold")).grid(
        row=0, column=1, sticky="w", pady=(16, 0), padx=(0, 16))
    tk.Label(win, text=body, justify="left", foreground="#333").grid(
        row=1, column=1, sticky="w", pady=(2, 10), padx=(0, 16))
    btns = ttk.Frame(win)
    btns.grid(row=2, column=1, sticky="e", padx=16, pady=(0, 14))
    if folder:
        ttk.Button(btns, text="Open folder",
                   command=lambda: (os.startfile(folder) if os.path.isdir(folder) else None)).pack(side="left", padx=4)
    ttk.Button(btns, text="OK", command=win.destroy).pack(side="left", padx=4)
    win.bind("<Return>", lambda e: win.destroy())
    win.bind("<Escape>", lambda e: win.destroy())
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    win.geometry(f"+{(win.winfo_screenwidth() - w) // 2}+{(win.winfo_screenheight() - h) // 3}")
    win.focus_force()
    if ok:
        win.after(15000, lambda: win.winfo_exists() and win.destroy())
    notify(title, body.splitlines()[0])   # best-effort balloon too; harmless if it no-ops


# ---------------------------------------------------------------- popup dialog
def show_transcribe_dialog(wav, secs=None, peak_mic=None, peak_sys=None):
    if not wav or not os.path.exists(wav) or os.path.getsize(wav) < 1024:
        show_result(False, "No audio",
                    "The recording is missing or empty — nothing to transcribe.\n\n"
                    f"{wav}")
        return

    win = tk.Toplevel(root)
    win.title("Transcribe recording")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    pad = {"padx": 12, "pady": 6}

    heading = "Transcribe existing recording"
    if secs is not None:
        heading = f"Recording saved  ({int(secs) // 60}m {int(secs) % 60}s)"
    ttk.Label(win, text=heading, font=("Segoe UI", 10, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", **pad)
    head = ttk.Frame(win)
    head.grid(row=1, column=0, columnspan=2, sticky="w", padx=12)
    ttk.Label(head, text=os.path.basename(wav), foreground="#666").pack(anchor="w")
    warns = []
    if secs is not None and secs < 2:
        warns.append("⚠ Very short recording — did capture start in time?")
    if peak_mic is not None and peak_mic < 0.001:
        warns.append("⚠ Your mic was silent — your own voice isn't in this recording.")
    if peak_sys is not None and peak_sys < 0.001:
        warns.append("⚠ System was silent — no remote audio (fine if nobody else spoke).")
    for w in warns:
        tk.Label(head, text=w, foreground="#c5221f", justify="left").pack(anchor="w")

    ttk.Label(win, text="Title:").grid(row=2, column=0, sticky="e", **pad)
    title_var = tk.StringVar()
    title_entry = ttk.Entry(win, textvariable=title_var, width=34)
    title_entry.grid(row=2, column=1, sticky="w", **pad)

    ttk.Label(win, text="Speakers:").grid(row=3, column=0, sticky="e", **pad)
    spk_var = tk.IntVar(value=0)
    spk = ttk.Spinbox(win, from_=0, to=12, textvariable=spk_var, width=6)
    spk.grid(row=3, column=1, sticky="w", **pad)
    ttk.Label(win, text="0 = let the model decide", foreground="#666").grid(
        row=4, column=1, sticky="w", padx=12)

    diar_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(win, text="Diarize (label who said what)", variable=diar_var).grid(
        row=5, column=1, sticky="w", **pad)

    ttk.Label(win, text="Language:").grid(row=6, column=0, sticky="e", **pad)
    lang_var = tk.StringVar(value=mr.LANGUAGE)
    ttk.Entry(win, textvariable=lang_var, width=6).grid(row=6, column=1, sticky="w", **pad)

    btns = ttk.Frame(win)
    btns.grid(row=7, column=0, columnspan=2, sticky="e", padx=12, pady=(10, 12))

    def submit():
        title = title_var.get().strip()
        speakers = spk_var.get() or None
        diar = diar_var.get()
        lang = lang_var.get().strip() or mr.LANGUAGE
        win.destroy()
        threading.Thread(target=transcribe_worker,
                         args=(wav, title, speakers, diar, lang), daemon=True).start()
        notify("Transcribing…", "Starting the server if needed (a minute or two), then transcribing.")

    def later():
        win.destroy()
        show_result(True, "Saved for later",
                    f"Audio kept:\n{wav}\n\nRight-click the tray icon → "
                    "“Transcribe an existing recording…” when you want the transcript.",
                    folder=os.path.dirname(wav))

    ttk.Button(btns, text="Transcribe later", command=later).pack(side="left", padx=4)
    ttk.Button(btns, text="Transcribe", command=submit).pack(side="left", padx=4)

    win.bind("<Return>", lambda e: submit())
    win.bind("<Escape>", lambda e: later())
    win.update_idletasks()
    # center on screen
    w, h = win.winfo_width(), win.winfo_height()
    x = (win.winfo_screenwidth() - w) // 2
    y = (win.winfo_screenheight() - h) // 3
    win.geometry(f"+{x}+{y}")
    win.focus_force()
    title_entry.focus_set()


def transcribe_worker(wav, title, speakers, diar, lang):
    def progress(msg):
        print(msg)
        state["status"] = msg
        refresh_icon()

    with lock:
        state["busy"] += 1
    refresh_icon()
    try:
        ui_q.put(mr.transcription_job(wav, title, speakers, diar, lang, progress))
    finally:
        with lock:
            state["busy"] -= 1
            if not state["busy"]:
                state["status"] = ""
        refresh_icon()


# ---------------------------------------------------------------- speaker names
def _inbox_copy(txt, title):
    """Write the inbox copy (if one is configured); returns a line for the result
    dialog, or None when there's nothing to say."""
    if not mr.INBOX_DIR:
        return None
    try:
        path = mr.write_inbox_copy(txt, title)
    except Exception as e:
        return f"⚠ Inbox copy failed: {type(e).__name__}: {e}"
    return f"Inbox: {os.path.basename(path)}" if path else "Inbox: skipped (unreachable or no speakers)"


def show_name_dialog(txt, then=None, title="", new=False):
    """Show sample context for every speaker plus a name box each; on Save,
    replace the [speaker N] labels in the .txt. `then` is a result payload to
    show once this dialog closes. The inbox copy (if configured) is written
    after Save — and after Skip too for a `new` transcript, so every meeting
    lands in the inbox once."""
    def finish(saved):
        line = _inbox_copy(txt, title) if (saved or new) else None
        if then:
            body = then["body"] + (f"\n{line}" if line else "")
            show_result(then["ok"] and not (line or "").startswith("⚠"),
                        then["title"], body, then.get("folder"))
        elif saved:
            show_result(not (line or "").startswith("⚠"), "Speakers renamed",
                        os.path.basename(txt) + (f"\n{line}" if line else ""),
                        folder=os.path.dirname(txt))

    turns = mr.read_turns(txt)
    speakers = sorted(dict.fromkeys(s for s, _ in turns),
                      key=lambda s: (len(s), s))   # speaker 2 before speaker 10
    if not speakers:
        finish(False); return

    win = tk.Toplevel(root)
    win.title("Who is who?")
    win.attributes("-topmost", True)
    win.columnconfigure(0, weight=1)
    win.rowconfigure(2, weight=1)
    ttk.Label(win, text="Name the speakers", font=("Segoe UI", 10, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 0))
    ttk.Label(win, text=os.path.basename(txt), foreground="#666").grid(
        row=1, column=0, columnspan=2, sticky="w", padx=12)

    frame = ttk.Frame(win)
    frame.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=12, pady=8)
    view = tk.Text(frame, width=100, height=26, wrap="word", font=("Segoe UI", 10),
                   relief="solid", borderwidth=1, padx=8, pady=6)
    sb = ttk.Scrollbar(frame, orient="vertical", command=view.yview)
    view.configure(yscrollcommand=sb.set)
    view.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    palette = ["#1a73e8", "#c5221f", "#137333", "#b06000", "#8430ce", "#007b83",
               "#d01884", "#5f6368"]
    for n, spk in enumerate(speakers):
        view.tag_configure(spk, foreground=palette[n % len(palette)],
                           font=("Segoe UI", 10, "bold"))
    view.tag_configure("hl", background="#fff4c2")     # every turn of the chosen speaker
    view.tag_configure("cur", background="#ffd54f")    # the turn we just jumped to
    view.tag_raise("cur", "hl")

    # the whole transcript, remembering where each speaker's turns sit
    sections = {spk: [] for spk in speakers}
    for spk, text in turns:
        start = view.index("end-1c")
        view.insert("end", f"[{spk}] ", spk)
        view.insert("end", text)
        sections[spk].append((start, view.index("end-1c")))
        view.insert("end", "\n\n")
    view.configure(state="disabled")

    pos = {"spk": None, "i": -1}
    counters = {}

    def jump(spk):
        """Highlight all of `spk`'s turns and step to the next one (wrapping)."""
        if pos["spk"] != spk:
            view.tag_remove("hl", "1.0", "end")
            for a, b in sections[spk]:
                view.tag_add("hl", a, b)
            if pos["spk"] in counters:
                counters[pos["spk"]].configure(text="")
            pos.update(spk=spk, i=-1)
        pos["i"] = (pos["i"] + 1) % len(sections[spk])
        a, b = sections[spk][pos["i"]]
        view.tag_remove("cur", "1.0", "end")
        view.tag_add("cur", a, b)
        view.see(b)
        view.see(a)
        counters[spk].configure(text=f"{pos['i'] + 1}/{len(sections[spk])}")

    boxes = ttk.Frame(win)
    boxes.grid(row=3, column=0, columnspan=2, sticky="w", padx=12)
    vars_ = {}
    for n, spk in enumerate(speakers):
        tk.Label(boxes, text=spk + ":", fg=palette[n % len(palette)],
                 font=("Segoe UI", 10, "bold")).grid(row=n, column=0, sticky="e", pady=3)
        v = tk.StringVar()
        e = ttk.Entry(boxes, textvariable=v, width=30)
        e.grid(row=n, column=1, sticky="w", padx=8, pady=3)
        if n == 0:
            e.focus_set()
        vars_[spk] = v
        ttk.Button(boxes, text=f"Next ▸  ({len(sections[spk])} turns)",
                   command=lambda s=spk: jump(s)).grid(row=n, column=2, sticky="w", pady=3)
        counters[spk] = ttk.Label(boxes, text="", foreground="#666", width=8)
        counters[spk].grid(row=n, column=3, sticky="w", padx=6)
    ttk.Label(win, text="Leave a box empty to keep that label.", foreground="#666").grid(
        row=4, column=0, columnspan=2, sticky="w", padx=12, pady=(4, 0))

    def save():
        names = {k: v.get() for k, v in vars_.items()}
        win.destroy()
        try:
            mr.rename_speakers(txt, names)
        except Exception as e:
            show_result(False, "Could not rename speakers", f"{type(e).__name__}: {e}\n\n{txt}")
            return
        finish(True)

    def skip():
        win.destroy()
        finish(False)

    btns = ttk.Frame(win)
    btns.grid(row=5, column=0, columnspan=2, sticky="e", padx=12, pady=(8, 12))
    ttk.Button(btns, text="Skip", command=skip).pack(side="left", padx=4)
    ttk.Button(btns, text="Save names", command=save).pack(side="left", padx=4)
    win.bind("<Return>", lambda e: save())
    win.bind("<Escape>", lambda e: skip())
    win.protocol("WM_DELETE_WINDOW", skip)
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    win.geometry(f"+{(win.winfo_screenwidth() - w) // 2}+{max(0, (win.winfo_screenheight() - h) // 4)}")
    win.focus_force()


def pick_transcript_to_name():
    parent = tk.Toplevel(root)
    parent.overrideredirect(True)
    parent.geometry("1x1+0+0")
    parent.attributes("-topmost", True)
    parent.update()
    try:
        path = filedialog.askopenfilename(
            parent=parent, title="Pick a transcript to name speakers in",
            initialdir=mr.OUTPUT_DIR,
            filetypes=[("Transcript", "*.txt"), ("All files", "*.*")])
    finally:
        parent.destroy()
    if path:
        path = os.path.normpath(path)
        if not mr.read_turns(path):
            show_result(False, "No speaker labels", f"No [speaker] lines found in:\n{path}")
        else:
            show_name_dialog(path, title=mr.title_from_txt(path))


# ---------------------------------------------------------------- tray menu
def on_toggle(icon_, item):
    if state["recording"]:
        stop_recording()
    else:
        start_recording()


def on_pick_file(icon_, item):
    ui_q.put(("pick", {}))


def on_name_file(icon_, item):
    ui_q.put(("pick_names", {}))


def on_quit(icon_, item):
    if state["recording"]:
        try:
            state["stop_event"].set()
        except Exception:
            pass
    ui_q.put(("quit", {}))


def build_menu():
    return pystray.Menu(
        pystray.MenuItem(
            lambda item: "■  Stop recording" if state["recording"] else "●  Start recording",
            on_toggle, default=True),
        pystray.MenuItem("Transcribe an existing recording…", on_pick_file),
        pystray.MenuItem("Name speakers in a transcript…", on_name_file),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )


# ---------------------------------------------------------------- health / heartbeat
# On 2026-10-01 the icon vanished from the taskbar while the process stayed alive,
# so the watchdog (which only checked for the process) saw nothing wrong. Now the
# tk thread re-adds a missing icon itself, and writes a heartbeat the watchdog uses
# to spot a hung tray: "<pid> <recording 0|1>", refreshed every HEALTH_EVERY seconds.
HEARTBEAT = os.path.join(_HERE, "tray.heartbeat")
HEALTH_EVERY = 30
_last_health = 0.0


def _icon_present():
    """NIM_MODIFY fails when the shell has no icon for our (hwnd, id)."""
    from pystray._util import win32 as w
    hwnd = getattr(icon, "_hwnd", None)
    if not hwnd:
        return True       # tray thread not up yet; nothing to judge
    return bool(w.Shell_NotifyIcon(w.NIM_MODIFY, w.NOTIFYICONDATAW(
        cbSize=ctypes.sizeof(w.NOTIFYICONDATAW), hWnd=hwnd, hID=id(icon),
        uFlags=w.NIF_TIP, szTip=icon.title)))


def health_tick():
    global _last_health
    now = time.monotonic()
    if now - _last_health < HEALTH_EVERY:
        return
    _last_health = now
    try:
        if not _icon_present():
            print(time.strftime("%Y-%m-%d %H:%M:%S"), "tray icon missing — re-adding it")
            icon._show()
    except Exception as e:
        print(f"health: icon check failed: {type(e).__name__}: {e}")
    try:
        with open(HEARTBEAT, "w") as f:
            f.write(f"{os.getpid()} {int(bool(state['recording']))}\n")
    except Exception:
        pass


# ---------------------------------------------------------------- tk pump loop
def pump():
    health_tick()
    try:
        while True:
            kind, payload = ui_q.get_nowait()
            if kind == "quit":
                try:
                    icon.stop()
                except Exception:
                    pass
                root.quit()
                return
            try:
                if kind == "ask":
                    show_transcribe_dialog(payload["wav"], payload.get("secs"),
                                           payload.get("peak_mic"), payload.get("peak_sys"))
                elif kind == "result":
                    show_result(payload["ok"], payload["title"], payload["body"], payload.get("folder"))
                elif kind == "pick":
                    pick_and_transcribe()
                elif kind == "name":
                    show_name_dialog(payload["txt"], payload.get("then"),
                                     payload.get("title", ""), payload.get("new", False))
                elif kind == "pick_names":
                    pick_transcript_to_name()
            except Exception as e:
                # never let one bad dialog kill the pump (and with it every later click)
                import traceback
                print(f"pump: {kind} handler failed: {type(e).__name__}: {e}")
                traceback.print_exc()
    except queue.Empty:
        pass
    root.after(150, pump)


def pick_and_transcribe():
    # The root is withdrawn, so a bare file dialog opens behind other windows and
    # looks like "nothing happened". Give it a visible, topmost parent.
    parent = tk.Toplevel(root)
    parent.overrideredirect(True)
    parent.geometry("1x1+0+0")
    parent.attributes("-topmost", True)
    parent.update()
    try:
        path = filedialog.askopenfilename(
            parent=parent,
            title="Pick a recording to transcribe",
            initialdir=mr.AUDIO_DIR if os.path.isdir(mr.AUDIO_DIR) else None,
            filetypes=[("Audio", "*.wav *.mp3 *.m4a *.ogg *.opus *.flac *.webm *.aac"),
                       ("All files", "*.*")])
    finally:
        parent.destroy()
    print(f"pick: {path!r}")
    if path:
        show_transcribe_dialog(os.path.normpath(path))


def main():
    global icon, root
    root = tk.Tk()
    root.withdraw()                       # no main window; we live in the tray
    icon = pystray.Icon("meeting_recorder", make_image(False), "Meeting recorder — idle", build_menu())
    icon.run_detached()                   # tray in its own thread
    root.after(150, pump)                 # tk owns the main thread (dialogs need it)
    root.mainloop()


if __name__ == "__main__":
    main()
