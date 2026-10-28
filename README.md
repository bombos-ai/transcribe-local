# transcribe-local

Record a meeting on your own Windows PC or Mac and get a speaker-labelled transcript
back, without anything leaving the machine.

- Captures **your microphone and the system output** (the other people on the call)
  at the same time, as a 16 kHz stereo WAV (left = mic, right = system).
- Saves the WAV **first**, so the audio survives whatever goes wrong afterwards.
- Levels both sides automatically before transcribing, so a quiet laptop mic isn't
  drowned out by loud call audio, and overlapping speech stays audible.
- Sends it to a local [TranscriptionSuite](https://github.com/homelab-00/TranscriptionSuite)
  server (Whisper + diarization, in Docker or natively on Apple Silicon) and writes a
  `.txt` with one line per speaker turn: `[speaker 0] …`, `[speaker 1] …`.
- Starts the server only for the transcription and stops it again, so it isn't
  holding your GPU and several GB of RAM between meetings.
- A **system-tray / menu-bar button** to start/stop, a popup for title and speaker
  count, and a "who is who?" dialog to replace `speaker N` with real names.
- Optionally drops a clean Markdown copy of each diarized transcript into a second
  folder, such as a notes app's or knowledge base's inbox.

| | Windows | macOS |
|---|---|---|
| System audio | WASAPI loopback (`soundcard`) | Core Audio process tap (Swift helper in `macos/`) |
| Tray | system tray (`pystray` + Tk) | menu bar (native AppKit via PyObjC) |
| Server | Docker Compose | native Metal/MLX server on Apple Silicon; Docker on Intel |
| Start at login + watchdog | Startup shortcut + `watchdog.vbs` | launchd agent + `macos/watchdog.sh` |

The Windows setup comes first below; **on a Mac, jump to [macOS](#macos).** Command
line options, output, server management and privacy are the same on both.

## Requirements

- Windows 10 or 11
- Python 3 from [python.org](https://www.python.org/downloads/windows/) (includes
  `tkinter`; developed on 3.13 and 3.14). Either tick **Add python.exe to PATH** in the
  installer, or use the `py` / `pyw` launcher it always installs instead of
  `python` / `pythonw` in the commands below.
- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- An NVIDIA GPU is strongly recommended. TranscriptionSuite has a CPU profile, but
  long meetings get slow.

## Install

### 1. Get the code and the Python packages

```powershell
git clone https://github.com/bombos-ai/transcribe-local.git
cd transcribe-local
pip install -r requirements.txt
```

### 2. Set up the transcription server (once)

This tool doesn't ship its own model server; it drives the TranscriptionSuite one.

1. Install the TranscriptionSuite dashboard from its
   [releases page](https://github.com/homelab-00/TranscriptionSuite/releases).
2. Open it and run its server setup: pick GPU or CPU and let it pull the image and
   download the models. Give it a Hugging Face token when it asks; the diarization
   model needs one.
3. Make one test transcription in the dashboard so you know the server works.
4. Check that `%APPDATA%\TranscriptionSuite\docker\.env` now exists. That folder is the
   Docker Compose project this tool starts and stops.
5. Close the dashboard. From here on the recorder starts the server itself.

Running TranscriptionSuite from a manual checkout instead of the dashboard also works:
point `TRANSCRIPTION_COMPOSE_DIR` at its `docker` folder, and if that folder's `.env`
doesn't set `COMPOSE_FILE`, list the compose files in `TRANSCRIPTION_COMPOSE_FILES`.
Put your Hugging Face token in `HF_TOKEN`; it is passed to the server on start.

The server listens on `http://localhost:9786`.

### 3. Configure

Everything has a default. To change something, copy `.env.example` to `.env` next to
the scripts and fill in what you need (environment variables work too, and win over
`.env`). Restart the tray after editing.

| Variable | Default | What it does |
|---|---|---|
| `TRANSCRIPTION_OUTPUT_DIR` | `%USERPROFILE%\Transcriptions` | Where transcripts go. WAVs go in its `recordings\` subfolder. |
| `TRANSCRIPTION_INBOX_DIR` | *(off)* | Also write each diarized transcript here as `YYYY-MM-DD-transcript-<title>.md`. Never overwrites. |
| `TRANSCRIPTION_LANGUAGE` | `nl` | Language code sent to the server. The tray popup lets you change it per recording. |
| `TRANSCRIPTION_COMPOSE_DIR` | `%APPDATA%\TranscriptionSuite\docker`, else `%USERPROFILE%\TranscriptionSuite\docker` | TranscriptionSuite's Compose folder. |
| `TRANSCRIPTION_COMPOSE_FILES` | *(let the folder's `.env` decide)* | `;`-separated compose files to pass as `-f`, for a checkout whose `.env` doesn't pin `COMPOSE_FILE`. |
| `HF_TOKEN` | *(none)* | Hugging Face token handed to the server as `HUGGINGFACE_TOKEN` (diarization needs it). |

Two more settings live at the top of `meeting_recorder.py`:
`MIC_NAME` (pick a specific mic by part of its name; default is the Windows default
input) and `MIC_GAIN` (an extra multiplier on top of the automatic levelling; also
available as `--mic-gain`).

### 4. Check that both sides are captured

Do this before relying on it for a real meeting. Play some audio and talk:

```powershell
python meeting_recorder.py --selftest
```

It records 5 seconds and prints peak levels:

- `mic=…` must be above 0 while you speak: your own voice is captured.
- `system=…` must be above 0 while audio plays: the other side is captured.

If either stays near 0, fix that source first (`--list-devices` shows what's available).
Capture always uses the **default** output device's loopback, so whatever you hear the
call through has to be the Windows default output.

## Use it

### Tray app (the normal way)

```powershell
pythonw meeting_tray.py
```

- **Left-click the icon** to start recording (icon turns red).
- **Left-click again** to stop. A popup asks for a title, the number of speakers
  (0 = let the model decide), diarization on/off and the language.
- The icon turns amber while the server starts (first cold start: ~60–90 s) and the
  audio is transcribed. Then a "who is who?" dialog shows the transcript with a name
  box per speaker; leave a box empty to keep `speaker N`. Closing it (Save or Skip)
  writes the inbox copy, if one is configured.
- **Transcribe later** keeps the WAV and skips the server.

Right-click menu:

- **Transcribe an existing recording…** re-runs any saved WAV, or transcribes an
  MP3/M4A/OGG/FLAC from elsewhere. This is also the recovery path when a transcription
  fails: the audio is always kept.
- **Name speakers in a transcript…** opens the naming dialog for an older `.txt`.
  Saving writes a fresh inbox copy (`-2`, `-3`, …).
- **Quit**

Only one tray instance can run; launching it again does nothing. It logs to `tray.log`
next to the script.

### Command line

```powershell
python meeting_recorder.py --title "Weekly sync" --speakers 3
```

Press **Enter** to stop recording. Other options:

| Option | What it does |
|---|---|
| `--file some.wav` | Transcribe an existing recording (WAV, MP3, …) instead of recording. |
| `--speakers N` | Hint for diarization. |
| `--no-diarization` | Plain transcript, no speaker labels. |
| `--language xx` | Override the language for this run. |
| `--mic-gain N` | Extra multiplier for your own voice on top of the automatic levelling (default 1). |
| `--list-devices` | Show microphones and loopback devices. |
| `--server-up` / `--server-down` | Start the server and leave it up / stop it. |
| `--keep-server` | Start the server if needed and leave it running afterwards. |
| `--no-manage-server` | Don't touch Docker; assume the server is already running. |

## Start at login, with a watchdog

Run these in PowerShell from the repo folder.

Start the tray at login:

```powershell
$repo = (Get-Location).Path
$lnk  = Join-Path ([Environment]::GetFolderPath('Startup')) 'Meeting Recorder.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = (Get-Command pyw.exe).Source
$s.Arguments = "`"$repo\meeting_tray.py`""
$s.WorkingDirectory = $repo
$s.Save()
```

Optional watchdog: `watchdog.vbs` relaunches the tray if it has crashed, or if it's
alive but hung (its heartbeat file is more than 3 minutes old). It never kills a tray
that was recording. Run it every 5 minutes:

```powershell
$repo = (Get-Location).Path
Register-ScheduledTask -TaskName MeetingRecorderWatchdog `
  -Action (New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$repo\watchdog.vbs`"") `
  -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)) `
  -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew)
```

The watchdog launches the tray with `pyw.exe` (the Python launcher, always on `PATH`
after a python.org install); to pin a specific interpreter, set a full `pythonw.exe`
path in `PYTHONW` at the top of `watchdog.vbs`. It logs to `watchdog.log`.

To undo: delete **Meeting Recorder** from `shell:startup` (Win+R → `shell:startup`) and
run `Unregister-ScheduledTask -TaskName MeetingRecorderWatchdog`.

## macOS

Everything above works on a Mac too; only setup, capture and autostart differ.

### Requirements

- macOS 14.2 or later (Sonoma, Sequoia, Tahoe); Apple Silicon recommended
- Xcode command line tools, for the small audio helper: `xcode-select --install`
- Python 3.10+ (e.g. `brew install python`); tkinter is **not** needed
- TranscriptionSuite: on **Apple Silicon** the `…-arm64-mac-metal.dmg` from its
  [releases page](https://github.com/homelab-00/TranscriptionSuite/releases) (no Docker
  needed); on an **Intel Mac** Docker Desktop plus the regular build

### Install

```bash
git clone https://github.com/bombos-ai/transcribe-local.git
cd transcribe-local
macos/install.sh
```

That creates `.venv`, installs the packages, builds the audio helper
(`macos/build/audiocap`) and installs a **login item**: the menu-bar icon appears now
and after every login, launchd restarts it if it crashes, and a watchdog restarts it
if it hangs (never while it's recording). Use `macos/install.sh --no-login` to skip
the login item and start it yourself with `.venv/bin/python meeting_tray.py`.

Set up the server once:

1. **Apple Silicon:** install TranscriptionSuite in `/Applications` (it's unsigned:
   `xattr -dr com.apple.quarantine /Applications/TranscriptionSuite.app`), open it, pick
   the **Metal** runtime and your models in the Server tab, give it a Hugging Face
   token if you want PyAnnote diarization, start it once so it downloads the models,
   and make one test transcription. Then stop the server and close the dashboard.
   The recorder starts the same server itself from then on, with the dashboard's model
   choices and token.
2. **Intel:** as in [step 2](#2-set-up-the-transcription-server-once) above; the
   Compose folder is `~/Library/Application Support/TranscriptionSuite/docker`.

### Allow the audio permissions

Run the self-test once from a terminal, with some audio playing, and talk:

```bash
.venv/bin/python meeting_recorder.py --selftest
```

macOS asks twice, both times for **"audiocap"**: *System Audio Recording* (the other
side of the call) and *Microphone* (you). Allow both. They're remembered for the helper
whatever starts it: terminal, menu bar or login item. If you clicked Don't Allow, turn
it on in **System Settings › Privacy & Security › Screen & System Audio Recording**
(the "System Audio Recording Only" list) and **› Microphone**.

The tap records **everything the Mac plays**, through whichever output is active, and
keeps following it when you switch to AirPods mid-call. There's no "default output"
to set up, and no BlackHole needed. The mic follows the system's default input;
set `MIC_NAME` to pin one (`--list-devices` shows them).

### Menu bar

- **Click the icon** to start recording: it turns red and shows a running clock.
- **Click again** to stop. The same title / speakers / diarization / language window
  and "who is who?" window as on Windows follow. The icon is amber while the server
  starts and transcribes.
- **Right-click** (or Control-click) for the menu: *Transcribe an existing recording…*,
  *Name speakers in a transcript…*, *Open transcripts folder*, *Quit*.

On a MacBook with a notch, a crowded menu bar can hide icons behind the notch;
⌘-drag the icon further right if you don't see it.

### Configure

The same `TRANSCRIPTION_*` variables as above, plus:

| Variable | Default | What it does |
|---|---|---|
| `TRANSCRIPTION_SERVER_MODE` | `auto` | `metal`, `docker`, or `auto` (metal when TranscriptionSuite's Metal backend is installed). |
| `TRANSCRIPTION_SUITE_APP` | `/Applications/TranscriptionSuite.app` | Where TranscriptionSuite is installed. |

The easiest place for them is the `.env` file next to the scripts (see `.env.example`):
it's read on every start, by the menu-bar app and the command line alike, so a change
only needs an app restart. Variables exported in your shell also work; the login item
gets a copy of the ones set when you run `macos/install.sh`, so re-run it after
changing one of those.

### Logs and undo

`tray.log`, `watchdog.log` and `metal-server.log` (the native server's output) are
written next to the scripts. `macos/install.sh --uninstall` removes the login item
and watchdog.

## How the server is managed

- **Docker mode** (Windows, Intel Macs): transcription runs `docker compose up -d` in the
  Compose folder, waits for `/health`, transcribes, then runs `docker compose down` in
  the background (the server drains for up to ~2 minutes; the next start waits for that
  to finish).
- **Metal mode** (Apple Silicon): it starts TranscriptionSuite's own server process
  (`python -m uvicorn server.api.main:app` from the app bundle, on `127.0.0.1:9786`)
  with the dashboard's model choices, waits for `/health`, transcribes, then stops it.
- If the server was **already running** (for example you opened the dashboard), it's
  used as-is and left running. Only a server this tool started gets stopped.
- Several transcriptions can run at once from the tray; the server stays up until the
  last one is done.
- If the process dies mid-job, an exit hook still stops the server. A native server
  that outlived a hard crash is recognised by its pid file and stopped after the next
  transcription.
- The computer is kept from sleeping while recording and while transcribing (the screen
  can still lock), so standby can't stall a long job.
- By default no `-f`/`-p` flags are passed; the dashboard's `.env` pins `COMPOSE_FILE`
  and `COMPOSE_PROJECT_NAME`, so this tool and the dashboard always agree on the stack.
  `TRANSCRIPTION_COMPOSE_FILES` overrides that for a manual checkout.

## Output

In `TRANSCRIPTION_OUTPUT_DIR`:

- `recordings\2026-10-28_1422.wav`: the raw audio (stereo: left = you, right = the
  call), saved before transcription starts. It sounds unbalanced when played back;
  levelling happens at transcription time, so it can always be re-run.
- `2026-10-28_1422_Weekly-sync.txt`: one line per speaker turn

With `TRANSCRIPTION_INBOX_DIR` set, also `2026-10-28-transcript-weekly-sync.md` there.

## Troubleshooting

- **Your own voice is missing.** Run `--selftest`. If `mic=` is ~0, the wrong input is
  the Windows default; change it or set `MIC_NAME`.
- **The other side is missing.** `system=` is ~0: the call is playing through a device
  that isn't the Windows default output.
- **"Capture dropouts" warning.** Short gaps in the audio, usually because the machine
  was overloaded (low on RAM) during the call.
- **"Server didn't start".** Is Docker Desktop running? Does
  `TRANSCRIPTION_COMPOSE_DIR` point at a folder with a `.env`? Try
  `python meeting_recorder.py --server-up` in a terminal to see the error.
- **Transcription failed or timed out.** The WAV is kept. Use **Transcribe an existing
  recording…** or `--file`.
- **Tray icon gone.** Check `tray.log`. With the watchdog installed it comes back within
  5 minutes.
- **Mac: `system=` or `mic=` stays 0.** The "audiocap" permission is off; see
  [Allow the audio permissions](#allow-the-audio-permissions). Rebuilding the helper
  after it has *changed* (e.g. a `git pull` that touched `macos/audiocap.swift`) makes
  macOS ask again.
- **Mac: "Metal server exited".** The last lines of `metal-server.log` are in the
  message; usually the models aren't downloaded yet. Start the server once from the
  TranscriptionSuite dashboard.

## Privacy

Audio and transcripts stay on your machine; this tool's only network call is to the
server on `localhost`. (The server itself downloads its models from Hugging Face the
first time it starts.) You are recording other people: tell them, and follow the consent rules
that apply to you.

## License

MIT, see [LICENSE](LICENSE).
