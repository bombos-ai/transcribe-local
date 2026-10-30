#!/usr/bin/env python3
"""
meeting_tray_mac.py — the macOS menu-bar version of meeting_tray.py.

Click the menu-bar icon to START recording; click again to STOP. Right-click
(or Control-click) for the menu. On stop, a small window asks for a title and
the number of speakers, then the audio is sent to the TranscriptionSuite
server and the diarized transcript is written to the output folder (see
meeting_recorder.OUTPUT_DIR), followed by the "who is who?" naming window.

The icon is an outline when idle (it follows the light/dark menu bar), red
with a running clock while recording, and amber while transcribing.

Native AppKit through PyObjC: no Tk, no Dock icon. Run it with:
    .venv/bin/python meeting_tray_mac.py      (meeting_tray.py also forwards here)

Deps: pip install -r requirements.txt
"""
import os, sys, time, threading, subprocess, traceback, fcntl

# --- make this importable no matter where it's launched from, and give the
#     (console-less) launchd process somewhere for prints to go BEFORE we import
#     meeting_recorder, which writes to sys.stdout at import time. ------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if not sys.stdout or not sys.stdout.isatty():
    try:
        _logf = open(os.path.join(_HERE, "tray.log"), "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = _logf
        sys.stderr = _logf
    except Exception:
        pass

# --- single instance: an exclusive lock the kernel drops when we exit, however
#     we exit, so a crash can never leave a stale lock behind. ----------------
_lockf = open(os.path.join(_HERE, "tray.lock"), "a")
try:
    fcntl.flock(_lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(0)

import objc
from AppKit import (
    NSApp, NSApplication, NSApplicationActivationPolicyAccessory, NSBezierPath, NSButton,
    NSColor, NSComboBox, NSEvent, NSEventModifierFlagControl, NSEventTypeApplicationDefined,
    NSEventTypeRightMouseUp, NSEventMaskLeftMouseUp, NSEventMaskRightMouseUp, NSFloatingWindowLevel,
    NSFont, NSFontAttributeName, NSForegroundColorAttributeName, NSBackgroundColorAttributeName,
    NSGridCell, NSGridView, NSImage, NSImageLeft, NSImageOnly, NSImageView, NSLayoutAttributeLeading, NSMenu,
    NSMenuItem, NSMutableAttributedString, NSAttributedString, NSOpenPanel, NSPopUpButton,
    NSScrollView, NSStackView, NSStackViewGravityTrailing, NSStatusBar, NSTextField, NSTextView, NSUserInterfaceLayoutOrientationVertical,
    NSUserInterfaceLayoutOrientationHorizontal, NSVariableStatusItemLength, NSWindow,
    NSWindowStyleMaskClosable, NSWindowStyleMaskResizable, NSWindowStyleMaskTitled,
    NSBackingStoreBuffered, NSModalResponseOK, NSWindowOcclusionStateVisible, NSGridCellPlacementTrailing, NSEdgeInsetsMake,
)
from Foundation import NSObject, NSMakeRect, NSMakeRange, NSMakePoint, NSMakeSize, NSURL, NSTimer, NSUserDefaults
from PyObjCTools import AppHelper

import meeting_recorder as mr


# ---------------------------------------------------------------- shared state
state = {"recording": False, "stop_event": None, "rec_thread": None,
         "wav": None, "started": 0.0, "busy": 0, "status": ""}   # busy = transcriptions running
lock = threading.Lock()
status_item = None
controller = None


def ui(fn, *args):
    """Run fn on the main (AppKit) thread. One bad dialog must never take the
    event loop down with it, and with it every later click."""
    def safe():
        try:
            fn(*args)
        except Exception as e:
            print(f"ui: {getattr(fn, '__name__', fn)} failed: {type(e).__name__}: {e}")
            traceback.print_exc()
    AppHelper.callAfter(safe)


def _activate():
    """We're an accessory app (no Dock icon): pull our windows to the front."""
    app = NSApp()
    if hasattr(app, "activate"):
        app.activate()
    else:
        app.activateIgnoringOtherApps_(True)


# ---------------------------------------------------------------- icon art
def _icon(kind):
    """18pt menu-bar glyph. Idle is a template image (macOS tints it to match a
    light or dark menu bar); red/amber keep their colour on purpose."""
    def draw(rect):
        if kind == "idle":
            NSColor.blackColor().set()
            ring = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(2.5, 2.5, 13, 13))
            ring.setLineWidth_(1.8)
            ring.stroke()
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(6.5, 6.5, 5, 5)).fill()
        else:
            (NSColor.systemRedColor() if kind == "rec" else NSColor.systemOrangeColor()).set()
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(2, 2, 14, 14)).fill()
        return True
    img = NSImage.imageWithSize_flipped_drawingHandler_(NSMakeSize(18, 18), False, draw)
    img.setTemplate_(kind == "idle")
    return img


_ICONS = {}


def _refresh_icon():
    if status_item is None:
        return
    rec, busy = state["recording"], state["busy"]
    kind = "rec" if rec else "busy" if busy else "idle"
    if kind not in _ICONS:
        _ICONS[kind] = _icon(kind)
    btn = status_item.button()
    btn.setImage_(_ICONS[kind])
    if rec:
        secs = int(time.monotonic() - state["started"])
        btn.setTitle_(f" {secs // 3600}:{secs // 60 % 60:02d}:{secs % 60:02d}" if secs >= 3600
                      else f" {secs // 60}:{secs % 60:02d}")
        btn.setImagePosition_(NSImageLeft)
        btn.setToolTip_("Meeting recorder — RECORDING (click to stop)")
    else:
        btn.setTitle_("")
        btn.setImagePosition_(NSImageOnly)
        btn.setToolTip_("Meeting recorder — " + ((state["status"] or "working…") if busy
                                                 else "idle (click to record)"))


def refresh_icon():
    ui(_refresh_icon)


def notify(title, msg):
    """Best-effort Notification Center banner; the windows are the real UI."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.Popen(["osascript", "-e", f'display notification "{esc(msg)}" with title "{esc(title)}"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def open_path(path):
    if path and os.path.exists(path):
        subprocess.Popen(["open", path])


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
    notify("Recording started", "Click the menu-bar icon again to stop.")


def record_thread_main(wav, ev):
    """Run the capture. On ANY crash, reset state and surface a window instead of
    dying silently and leaving the icon stuck red. On a clean stop, hand the audio
    (with its peak levels) to the main thread for the title/speakers window."""
    try:
        wav_, secs, peak_mic, peak_sys = mr.record_session(wav, ev)   # blocks until ev set
    except Exception as e:
        traceback.print_exc()
        with lock:
            state["recording"] = False
        refresh_icon()
        ui(show_result, False, "Recording crashed",
           f"{type(e).__name__}: {e}\n\nAudio may be incomplete:\n{wav}", mr.AUDIO_DIR)
        return
    with lock:
        state["recording"] = False
    refresh_icon()
    if not state.get("quitting"):
        ui(show_transcribe_dialog, wav_, secs, peak_mic, peak_sys)


def stop_recording():
    """Signal the capture thread to finish; record_thread_main opens the window
    when it returns (and flips the icon back to idle)."""
    with lock:
        ev = state["stop_event"] if state["recording"] else None
    if ev:
        ev.set()


# ---------------------------------------------------------------- window plumbing
_open_dialogs = set()       # PyObjC objects die with their last Python reference


class _Target(NSObject):
    """Target/action trampoline into a Python callable."""
    def fire_(self, sender):
        try:
            self.fn()
        except Exception as e:
            print(f"action failed: {type(e).__name__}: {e}")
            traceback.print_exc()


class _Window(NSWindow):
    def cancelOperation_(self, sender):            # Escape
        self.dialog.escape()


class _WindowDelegate(NSObject):
    def windowWillClose_(self, note):
        self.dialog._closed_by_window()


class Dialog:
    """A plain floating window built in code; on_close runs when the user closes
    it with the title-bar button (Escape is wired to a button instead)."""
    def __init__(self, title, resizable=False):
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        if resizable:
            style |= NSWindowStyleMaskResizable
        self.win = _Window.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 400, 200), style, NSBackingStoreBuffered, False)
        self.win.dialog = self
        self.win.setTitle_(title)
        self.win.setReleasedWhenClosed_(False)
        self.win.setLevel_(NSFloatingWindowLevel)          # "topmost", like the Windows tray
        self.delegate = _WindowDelegate.alloc().init()
        self.delegate.dialog = self
        self.win.setDelegate_(self.delegate)
        self.targets = []
        self.on_close = None
        self.closed = False
        _open_dialogs.add(self)

    def target(self, fn):
        t = _Target.alloc().init()
        t.fn = fn
        self.targets.append(t)
        return t

    def button(self, title, fn, key=None):
        b = NSButton.buttonWithTitle_target_action_(title, self.target(fn), "fire:")
        if key:
            b.setKeyEquivalent_(key)
        return b

    def show(self, content, first_responder=None, min_size=None):
        content.setTranslatesAutoresizingMaskIntoConstraints_(False)
        cv = self.win.contentView()
        cv.addSubview_(content)
        for a, b in ((content.leadingAnchor(), cv.leadingAnchor()), (content.trailingAnchor(), cv.trailingAnchor()),
                     (content.topAnchor(), cv.topAnchor()), (content.bottomAnchor(), cv.bottomAnchor())):
            a.constraintEqualToAnchor_(b).setActive_(True)
        self.win.setContentSize_(content.fittingSize())
        if min_size:
            self.win.setContentMinSize_(min_size)
        self.win.center()
        _activate()
        self.win.makeKeyAndOrderFront_(None)
        self.win.orderFrontRegardless()
        if first_responder is not None:
            self.win.makeFirstResponder_(first_responder)

    def escape(self):
        """Escape acts like the title-bar close button."""
        if not self.closed:
            self.close()
            if self.on_close:
                self.on_close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.win.close()
            _open_dialogs.discard(self)

    def _closed_by_window(self):
        if not self.closed:
            self.closed = True
            _open_dialogs.discard(self)
            if self.on_close:
                self.on_close()


def _label(text, bold=False, size=None, color=None, wrap=False, selectable=False):
    f = NSTextField.wrappingLabelWithString_(text) if wrap else NSTextField.labelWithString_(text)
    size = size or NSFont.systemFontSize()
    f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    if color is not None:
        f.setTextColor_(color)
    if selectable:
        f.setSelectable_(True)
    if wrap:
        f.setPreferredMaxLayoutWidth_(420)
    return f


def _stack(views, vertical=True, spacing=8, insets=None, align_leading=True):
    s = NSStackView.stackViewWithViews_(views)
    s.setOrientation_(NSUserInterfaceLayoutOrientationVertical if vertical
                      else NSUserInterfaceLayoutOrientationHorizontal)
    s.setSpacing_(spacing)
    if vertical and align_leading:
        s.setAlignment_(NSLayoutAttributeLeading)
    if insets:
        s.setEdgeInsets_(NSEdgeInsetsMake(*insets))
    return s


def _buttons_row(*buttons):
    """Right-aligned row of buttons (pin it full width with _full_width)."""
    row = _stack([], vertical=False, spacing=8)
    for b in buttons:
        row.addView_inGravity_(b, NSStackViewGravityTrailing)
    return row


def _full_width(stack, *views):
    for v in views:
        v.leadingAnchor().constraintEqualToAnchor_constant_(stack.leadingAnchor(), stack.edgeInsets().left).setActive_(True)
        v.trailingAnchor().constraintEqualToAnchor_constant_(stack.trailingAnchor(), -stack.edgeInsets().right).setActive_(True)


# ---------------------------------------------------------------- result window
def show_result(ok, title, body, folder=None):
    """A visible, guaranteed outcome window (notification banners are easy to
    miss). Success auto-closes after 15 s; failures stay until dismissed."""
    d = Dialog(title)
    sym = "checkmark.circle.fill" if ok else "exclamationmark.triangle.fill"
    img = NSImageView.imageViewWithImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_(sym, None))
    img.setSymbolConfiguration_(objc.lookUpClass("NSImageSymbolConfiguration").configurationWithPointSize_weight_(28, 0))
    img.setContentTintColor_(NSColor.systemGreenColor() if ok else NSColor.systemRedColor())
    text = _stack([_label(title, bold=True, size=14),
                   _label(body, wrap=True, selectable=True, color=NSColor.secondaryLabelColor())], spacing=4)
    top = _stack([img, text], vertical=False, spacing=14)
    top.setAlignment_(3)                     # NSLayoutAttributeTop
    btns = [d.button("OK", d.close, "\r")]
    if folder:
        btns.insert(0, d.button("Open folder", lambda: open_path(folder)))
    row = _buttons_row(*btns)
    root = _stack([top, row], spacing=14, insets=(18, 20, 16, 20))
    _full_width(root, row)
    d.show(root)
    if ok:
        AppHelper.callLater(15, d.close)
    notify(title, body.splitlines()[0] if body else "")


# ---------------------------------------------------------------- transcribe window
LANGS = mr.LANGS


def show_transcribe_dialog(wav, secs=None, peak_mic=None, peak_sys=None):
    if not wav or not os.path.exists(wav) or os.path.getsize(wav) < 1024:
        show_result(False, "No audio",
                    "The recording is missing or empty — nothing to transcribe.\n\n"
                    f"{wav}")
        return

    d = Dialog("Transcribe recording")
    dur = f"  ({int(secs) // 60}m {int(secs) % 60}s)" if secs is not None else ""
    head = [_label(f"Recording saved{dur}", bold=True, size=14),
            _label(os.path.basename(wav), color=NSColor.secondaryLabelColor())]
    warns = []
    if secs is not None and secs < 2:
        warns.append("⚠ Very short recording — did capture start in time?")
    if peak_mic is not None and peak_mic < 0.001:
        warns.append("⚠ Your mic was silent — your own voice isn't in this recording.")
    if peak_sys is not None and peak_sys < 0.001:
        warns.append("⚠ System was silent — no remote audio (fine if nobody else spoke).")
    head += [_label(w, color=NSColor.systemRedColor(), wrap=True) for w in warns]

    title_f = NSTextField.textFieldWithString_("")
    title_f.setPlaceholderString_("e.g. Weekly sync")
    title_f.widthAnchor().constraintEqualToConstant_(260).setActive_(True)
    spk = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 200, 26), False)
    spk.addItemsWithTitles_(["Let the model decide"] + [str(n) for n in range(1, 13)])
    diar = NSButton.checkboxWithTitle_target_action_("Diarize (label who said what)", None, None)
    diar.setState_(1)
    lang = NSComboBox.alloc().initWithFrame_(NSMakeRect(0, 0, 80, 26))
    lang.addItemsWithObjectValues_(LANGS if mr.LANGUAGE in LANGS else [mr.LANGUAGE] + LANGS)
    lang.setStringValue_(mr.LANGUAGE)
    lang.widthAnchor().constraintEqualToConstant_(80).setActive_(True)

    grid = NSGridView.gridViewWithViews_([
        [_label("Title:"), title_f],
        [_label("Speakers:"), spk],
        [NSGridCell.emptyContentView(), diar],
        [_label("Language:"), lang],
    ])
    grid.setRowSpacing_(10)
    grid.setColumnSpacing_(8)
    grid.columnAtIndex_(0).setXPlacement_(NSGridCellPlacementTrailing)

    def submit():
        title = title_f.stringValue().strip()
        speakers = spk.indexOfSelectedItem() or None
        diarize = bool(diar.state())
        language = lang.stringValue().strip() or mr.LANGUAGE
        d.close()
        threading.Thread(target=transcribe_worker,
                         args=(wav, title, speakers, diarize, language), daemon=True).start()
        notify("Transcribing…", "Starting the server if needed, then transcribing.")

    def later():
        d.close()
        show_result(True, "Saved for later",
                    f"Audio kept:\n{wav}\n\nRight-click the menu-bar icon → "
                    "“Transcribe an existing recording…” when you want the transcript.",
                    folder=os.path.dirname(wav))

    d.on_close = later
    row = _buttons_row(d.button("Transcribe later", later), d.button("Transcribe", submit, "\r"))
    root = _stack([_stack(head, spacing=3), grid, row], spacing=16, insets=(18, 20, 16, 20))
    _full_width(root, row)
    d.show(root, first_responder=title_f)


def transcribe_worker(wav, title, speakers, diar, lang):
    def progress(msg):
        print(msg)
        state["status"] = msg
        refresh_icon()

    with lock:
        state["busy"] += 1
    refresh_icon()
    try:
        kind, payload = mr.transcription_job(wav, title, speakers, diar, lang, progress)
        if kind == "name":
            ui(show_name_dialog, payload["txt"], payload.get("then"),
               payload.get("title", ""), payload.get("new", False))
        else:
            ui(show_result, payload["ok"], payload["title"], payload["body"], payload.get("folder"))
    finally:
        with lock:
            state["busy"] -= 1
            if not state["busy"]:
                state["status"] = ""
        refresh_icon()


# ---------------------------------------------------------------- speaker names
def _palette():
    return [NSColor.systemBlueColor(), NSColor.systemRedColor(), NSColor.systemGreenColor(),
            NSColor.systemOrangeColor(), NSColor.systemPurpleColor(), NSColor.systemTealColor(),
            NSColor.systemPinkColor(), NSColor.systemGrayColor()]


def show_name_dialog(txt, then=None, title="", new=False):
    """Show the transcript with every speaker coloured plus a name box each; on
    Save, replace the [speaker N] labels in the .txt. `then` is a result payload
    to show once this window closes. The inbox copy (if configured) is written
    after Save — and after Skip too for a `new` transcript, so every meeting
    lands in the inbox once."""
    def finish(saved):
        line = mr.inbox_copy_line(txt, title) if (saved or new) else None
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

    d = Dialog("Who is who?", resizable=True)
    palette = _palette()
    color = {spk: palette[n % len(palette)] for n, spk in enumerate(speakers)}
    body_font = NSFont.systemFontOfSize_(13)
    bold_font = NSFont.boldSystemFontOfSize_(13)

    # the whole transcript, remembering where each speaker's turns sit
    doc = NSMutableAttributedString.alloc().init()
    sections = {spk: [] for spk in speakers}
    for spk, text in turns:
        start = doc.length()
        doc.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            f"[{spk}] ", {NSFontAttributeName: bold_font, NSForegroundColorAttributeName: color[spk]}))
        doc.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: body_font, NSForegroundColorAttributeName: NSColor.labelColor()}))
        sections[spk].append(NSMakeRange(start, doc.length() - start))
        doc.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            "\n\n", {NSFontAttributeName: body_font}))

    scroll = NSTextView.scrollableTextView()
    view = scroll.documentView()
    view.setEditable_(False)
    view.setTextContainerInset_(NSMakeSize(8, 8))
    view.textStorage().setAttributedString_(doc)
    scroll.setBorderType_(1)                            # NSLineBorder
    scroll.widthAnchor().constraintGreaterThanOrEqualToConstant_(760).setActive_(True)
    scroll.heightAnchor().constraintGreaterThanOrEqualToConstant_(380).setActive_(True)
    scroll.setContentHuggingPriority_forOrientation_(1, 1)
    hl = NSColor.systemYellowColor().colorWithAlphaComponent_(0.22)   # every turn of the chosen speaker
    cur = NSColor.systemYellowColor().colorWithAlphaComponent_(0.6)   # the turn we just jumped to

    pos = {"spk": None, "i": -1}
    counters = {}

    def jump(spk):
        """Highlight all of `spk`'s turns and step to the next one (wrapping)."""
        ts = view.textStorage()
        ts.removeAttribute_range_(NSBackgroundColorAttributeName, NSMakeRange(0, ts.length()))
        if pos["spk"] != spk:
            if pos["spk"] in counters:
                counters[pos["spk"]].setStringValue_("")
            pos.update(spk=spk, i=-1)
        for r in sections[spk]:
            ts.addAttribute_value_range_(NSBackgroundColorAttributeName, hl, r)
        pos["i"] = (pos["i"] + 1) % len(sections[spk])
        r = sections[spk][pos["i"]]
        ts.addAttribute_value_range_(NSBackgroundColorAttributeName, cur, r)
        view.scrollRangeToVisible_(r)
        counters[spk].setStringValue_(f"{pos['i'] + 1}/{len(sections[spk])}")

    rows, fields = [], {}
    for n, spk in enumerate(speakers):
        lab = _label(spk + ":", bold=True, color=color[spk])
        f = NSTextField.textFieldWithString_("")
        f.setPlaceholderString_("name")
        f.widthAnchor().constraintEqualToConstant_(240).setActive_(True)
        fields[spk] = f
        k = len(sections[spk])
        nxt = d.button(f"Next ▸  ({k} turn{'s' if k != 1 else ''})", lambda s=spk: jump(s))
        counters[spk] = _label("", color=NSColor.secondaryLabelColor())
        counters[spk].widthAnchor().constraintEqualToConstant_(60).setActive_(True)
        rows.append([lab, f, nxt, counters[spk]])
    grid = NSGridView.gridViewWithViews_(rows)
    grid.setRowSpacing_(6)
    grid.setColumnSpacing_(8)
    grid.columnAtIndex_(0).setXPlacement_(NSGridCellPlacementTrailing)
    grid.setContentHuggingPriority_forOrientation_(750, 0)     # keep it at its natural width, left

    def save():
        names = {k: v.stringValue() for k, v in fields.items()}
        d.close()
        try:
            mr.rename_speakers(txt, names)
        except Exception as e:
            show_result(False, "Could not rename speakers", f"{type(e).__name__}: {e}\n\n{txt}")
            return
        finish(True)

    def skip():
        d.close()
        finish(False)

    d.on_close = skip
    row = _buttons_row(d.button("Skip", skip), d.button("Save names", save, "\r"))
    root = _stack([
        _stack([_label("Name the speakers", bold=True, size=14),
                _label(os.path.basename(txt), color=NSColor.secondaryLabelColor())], spacing=2),
        scroll, grid,
        _label("Leave a box empty to keep that label.", color=NSColor.secondaryLabelColor()),
        row], spacing=10, insets=(16, 18, 14, 18))
    _full_width(root, scroll, row)
    d.show(root, first_responder=fields[speakers[0]], min_size=NSMakeSize(640, 420))


# ---------------------------------------------------------------- file pickers
AUDIO_TYPES = ["wav", "mp3", "m4a", "ogg", "opus", "flac", "webm", "aac"]


def _pick(title, directory, exts):
    panel = NSOpenPanel.openPanel()
    panel.setTitle_(title)
    panel.setMessage_(title)
    panel.setCanChooseFiles_(True)
    panel.setCanChooseDirectories_(False)
    panel.setAllowsMultipleSelection_(False)
    panel.setAllowedFileTypes_(exts)
    if directory and os.path.isdir(directory):
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(directory))
    _activate()
    panel.setLevel_(NSFloatingWindowLevel)
    if panel.runModal() != NSModalResponseOK:
        return None
    return panel.URL().path()


def pick_and_transcribe():
    path = _pick("Pick a recording to transcribe", mr.AUDIO_DIR, AUDIO_TYPES)
    print(f"pick: {path!r}")
    if path:
        show_transcribe_dialog(os.path.normpath(path))


def pick_transcript_to_name():
    path = _pick("Pick a transcript to name speakers in", mr.OUTPUT_DIR, ["txt"])
    if path:
        path = os.path.normpath(path)
        if not mr.read_turns(path):
            show_result(False, "No speaker labels", f"No [speaker] lines found in:\n{path}")
        else:
            show_name_dialog(path, title=mr.title_from_txt(path))


# ---------------------------------------------------------------- menu
def on_toggle():
    if state["recording"]:
        stop_recording()
    else:
        start_recording()


def on_quit():
    state["quitting"] = True
    ev, th = state["stop_event"], state["rec_thread"]
    if state["recording"] and ev:
        ev.set()
        th.join(10)          # let the WAV be finalised; it's the one thing we can't lose
    NSApp().stop_(None)
    # stop: only takes effect after the next event; post one.
    NSApp().postEvent_atStart_(NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
        NSEventTypeApplicationDefined, NSMakePoint(0, 0), 0, 0, 0, None, 0, 0, 0), True)


def _status_line():
    if state["recording"]:
        secs = int(time.monotonic() - state["started"])
        return f"Recording — {secs // 60}m {secs % 60:02d}s"
    if state["busy"]:
        return state["status"] or "Working…"
    return "Idle"


class Controller(NSObject):
    def clicked_(self, sender):
        ev = NSApp().currentEvent()
        if ev is not None and (ev.type() == NSEventTypeRightMouseUp
                               or ev.modifierFlags() & NSEventModifierFlagControl):
            self.showMenu()
        else:
            on_toggle()

    def showMenu(self):
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        status = menu.addItemWithTitle_action_keyEquivalent_(_status_line(), None, "")
        status.setEnabled_(False)
        menu.addItem_(NSMenuItem.separatorItem())
        for title, sel in (("Stop recording" if state["recording"] else "Start recording", "toggle:"),
                           ("Transcribe an existing recording…", "pickWav:"),
                           ("Name speakers in a transcript…", "pickTxt:"),
                           ("Open transcripts folder", "openFolder:"),
                           (None, None),
                           ("Quit", "quit:")):
            if title is None:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            item = menu.addItemWithTitle_action_keyEquivalent_(title, sel, "")
            item.setTarget_(self)
        status_item.setMenu_(menu)
        status_item.button().performClick_(None)       # pops the menu up under the icon
        status_item.setMenu_(None)                     # so a plain click toggles again

    def toggle_(self, sender):
        on_toggle()

    def pickWav_(self, sender):
        ui(pick_and_transcribe)

    def pickTxt_(self, sender):
        ui(pick_transcript_to_name)

    def openFolder_(self, sender):
        os.makedirs(mr.OUTPUT_DIR, exist_ok=True)
        open_path(mr.OUTPUT_DIR)

    def quit_(self, sender):
        on_quit()

    def tick_(self, timer):
        if state["recording"]:
            _refresh_icon()           # the running clock
        health_tick()


def _edit_menu():
    """Accessory apps show no menu bar, but key equivalents still route through
    the main menu: without an Edit menu, ⌘C/⌘V/⌘A don't work in text fields."""
    main = NSMenu.alloc().init()
    app_item = main.addItemWithTitle_action_keyEquivalent_("", None, "")
    app_menu = NSMenu.alloc().init()
    app_menu.addItemWithTitle_action_keyEquivalent_("Close Window", "performClose:", "w")
    app_item.setSubmenu_(app_menu)
    edit_item = main.addItemWithTitle_action_keyEquivalent_("Edit", None, "")
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for title, sel, key in (("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("Cut", "cut:", "x"),
                            ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
                            ("Select All", "selectAll:", "a")):
        edit.addItemWithTitle_action_keyEquivalent_(title, sel, key)
    edit_item.setSubmenu_(edit)
    return main


# ---------------------------------------------------------------- heartbeat
# The watchdog (macos/watchdog.sh) uses this to spot a hung menu-bar app:
# "<pid> <recording 0|1>", refreshed every HEALTH_EVERY seconds from the main
# thread, so a frozen event loop stops refreshing it.
HEARTBEAT = os.path.join(_HERE, "tray.heartbeat")
HEALTH_EVERY = 30
_last_health = 0.0


_icon_seen = None


def _log_icon_visibility():
    """Log when the menu-bar icon becomes visible or hidden. macOS hides icons
    that don't fit (on a notched MacBook: behind the notch) without telling the
    app; this at least leaves a trace in tray.log."""
    global _icon_seen
    win = status_item.button().window() if status_item else None
    if win is None:
        return
    visible = bool(win.occlusionState() & NSWindowOcclusionStateVisible) and win.isVisible()
    if visible != _icon_seen:
        _icon_seen = visible
        f = win.frame()
        print(time.strftime("%Y-%m-%d %H:%M:%S"),
              f"menu-bar icon {'visible' if visible else 'HIDDEN (menu bar full / behind the notch?)'}"
              f" at x={f.origin.x:.0f}")


def health_tick():
    global _last_health
    now = time.monotonic()
    if now - _last_health < HEALTH_EVERY:
        return
    _last_health = now
    if _icon_seen is not None:           # the first check runs once macOS has placed the icon
        _log_icon_visibility()
    try:
        with open(HEARTBEAT, "w") as f:
            f.write(f"{os.getpid()} {int(bool(state['recording']))}\n")
    except Exception:
        pass


# ---------------------------------------------------------------- main
STATUS_ITEM_NAME = "MeetingRecorder"
STATUS_ITEM_POSITION = 300      # points from the right screen edge: just left of the clock & co.


def main():
    global status_item, controller
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)   # menu bar only, no Dock icon
    app.setMainMenu_(_edit_menu())

    controller = Controller.alloc().init()
    # A new status item is added at the far left of the other icons, which on a
    # notched MacBook with a full menu bar is behind the notch: invisible. Unless
    # the user has placed it (⌘-drag stores this key), start near the right.
    defaults = NSUserDefaults.standardUserDefaults()
    if defaults.objectForKey_(f"NSStatusItem Preferred Position {STATUS_ITEM_NAME}") is None:
        defaults.setDouble_forKey_(STATUS_ITEM_POSITION, f"NSStatusItem Preferred Position {STATUS_ITEM_NAME}")
    status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    status_item.setAutosaveName_(STATUS_ITEM_NAME)
    btn = status_item.button()
    btn.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(NSFont.systemFontSize(), 0))
    btn.setTarget_(controller)
    btn.setAction_("clicked:")
    btn.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
    _refresh_icon()

    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1.0, controller, "tick:", None, True)
    health_tick()
    AppHelper.callLater(3, _log_icon_visibility)
    print(time.strftime("%Y-%m-%d %H:%M:%S"), f"menu-bar app up (pid {os.getpid()}, server mode {mr.server_mode()})")
    app.run()
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "quit")
    # atexit (meeting_recorder._stop_if_owned) now stops a server we started mid-job.


if __name__ == "__main__":
    main()
