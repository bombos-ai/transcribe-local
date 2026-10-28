# transcribe-local

Record a meeting on your own PC and get a speaker-labelled transcript back, without
anything leaving the machine.

- Captures **your microphone and the system output** (the other people on the call)
  at the same time, mixed into one 16 kHz mono WAV.
- Saves the WAV **first**, so the audio survives whatever goes wrong afterwards.
- Sends it to a local [TranscriptionSuite](https://github.com/homelab-00/TranscriptionSuite)
  server (Whisper + diarization in Docker) and writes a `.txt` with one line per
  speaker turn: `[speaker 0] …`, `[speaker 1] …`.
- Starts the Docker server only for the transcription and stops it again, so it isn't
  holding your GPU and several GB of RAM between meetings.
- A **system-tray button** to start/stop, a popup for title and speaker count, and a
  "who is who?" dialog to replace `speaker N` with real names.

Windows only: audio capture uses WASAPI loopback, and the tray and watchdog use Win32.

## Requirements

- Windows 10 or 11
- Python 3 from [python.org](https://www.python.org/downloads/windows/) (includes
  `tkinter`; developed on 3.13). Tick **Add python.exe to PATH** in the installer.
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

The server listens on `http://localhost:9786`.

### 3. Configure

Everything has a default; set environment variables only for what you want to change.

| Variable | Default | What it does |
|---|---|---|
| `TRANSCRIPTION_OUTPUT_DIR` | `%USERPROFILE%\Transcriptions` | Where transcripts go. WAVs go in its `recordings\` subfolder. |
| `TRANSCRIPTION_LANGUAGE` | `nl` | Language code sent to the server. The tray popup lets you change it per recording. |
| `TRANSCRIPTION_COMPOSE_DIR` | `%APPDATA%\TranscriptionSuite\docker` | TranscriptionSuite's Compose folder. |

```powershell
setx TRANSCRIPTION_OUTPUT_DIR "D:\Transcriptions"
setx TRANSCRIPTION_LANGUAGE "en"
```

`setx` only affects programs started afterwards, so open a new terminal (and restart the
tray if it's running).

Two more settings live at the top of `meeting_recorder.py`:
`MIC_NAME` (pick a specific mic by part of its name; default is the Windows default
input) and `MIC_GAIN` (also available as `--mic-gain`).

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
  box per speaker; leave a box empty to keep `speaker N`.
- **Transcribe later** keeps the WAV and skips the server.

Right-click menu:

- **Transcribe an existing WAV…** re-runs any saved recording. This is also the
  recovery path when a transcription fails: the audio is always kept.
- **Name speakers in a transcript…** opens the naming dialog for an older `.txt`.
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
| `--file some.wav` | Transcribe an existing WAV instead of recording. |
| `--speakers N` | Hint for diarization. |
| `--no-diarization` | Plain transcript, no speaker labels. |
| `--language xx` | Override the language for this run. |
| `--mic-gain N` | Boost your own voice in the mix (default 3; headset mics run quiet). Lower it if your lines clip. |
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
$s.TargetPath = (Get-Command pythonw.exe).Source
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

The watchdog finds `pythonw.exe` via `PATH`; if it isn't on `PATH`, set the full path in
`PYTHONW` at the top of `watchdog.vbs`. It logs to `watchdog.log`.

To undo: delete **Meeting Recorder** from `shell:startup` (Win+R → `shell:startup`) and
run `Unregister-ScheduledTask -TaskName MeetingRecorderWatchdog`.

## How the server is managed

- Transcription runs `docker compose up -d` in the Compose folder, waits for `/health`,
  transcribes, then runs `docker compose down`.
- If the server was **already running** (for example you opened the dashboard), it's
  used as-is and left running. Only a server this tool started gets stopped.
- If the process dies mid-job, an exit hook still stops the server.
- No `-f`/`-p` flags are passed; the dashboard's `.env` pins `COMPOSE_FILE` and
  `COMPOSE_PROJECT_NAME`, so this tool and the dashboard always agree on the stack.

## Output

In `TRANSCRIPTION_OUTPUT_DIR`:

- `recordings\2026-10-28_1422.wav`: the audio, saved before transcription starts
- `2026-10-28_1422_Weekly-sync.txt`: one line per speaker turn

## Troubleshooting

- **Your own voice is missing.** Run `--selftest`. If `mic=` is ~0, the wrong input is
  the Windows default; change it or set `MIC_NAME`.
- **The other side is missing.** `system=` is ~0: the call is playing through a device
  that isn't the Windows default output.
- **"Server didn't start".** Is Docker Desktop running? Does
  `TRANSCRIPTION_COMPOSE_DIR` point at a folder with a `.env`? Try
  `python meeting_recorder.py --server-up` in a terminal to see the error.
- **Transcription failed or timed out.** The WAV is kept. Use **Transcribe an existing
  WAV…** or `--file`.
- **Tray icon gone.** Check `tray.log`. With the watchdog installed it comes back within
  5 minutes.

## Privacy

Audio and transcripts stay on your machine; this tool's only network call is to the
server on `localhost`. You are recording other people: tell them, and follow the consent rules
that apply to you.

## License

MIT, see [LICENSE](LICENSE).
