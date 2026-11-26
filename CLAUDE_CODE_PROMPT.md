# Prompt for Claude Code: working on "VTeam Screen Recorder"

Open Claude Code **in the folder that holds `VTeamScreenRecorder.py`**, then paste everything below the line.

---

You are working on **VTeam Screen Recorder**, my free Windows screen recorder (v1.0.0). It records the full screen as one MP4 with PC sound, mic and an optional camera bubble / cutout, with pause. **Every feature is free.** People can optionally **tip ₹20–₹300 by UPI**; tips unlock nothing. It's published on GitHub (`souldraft/vteam-screen-recorder`, all rights reserved) with the .exe on the Releases page and a website in `docs/` (GitHub Pages). The recorder is the same code as in my other app, Video Downloader (`souldraft/video-downloader`); a fix to the recorder usually belongs in both. I'm not a programmer: do the work yourself, run the commands yourself, and explain results in plain English.

## Rules (read first)
- **`VTeamScreenRecorder.py` is the starting point. Never rewrite it from scratch.** Read it, change only what's needed, keep its structure: one file, the recorder engine (`ScreenRecorder`, `AudioCapture`, `CameraFeed`…) separate from the GUI (`App`), helpers as plain functions so `--selftest` can test them.
- Don't add features I didn't ask for. Ask before changing the look of anything.
- Never add DRM circumvention.
- **Never put a secret in the app or the repo**: no API keys, no tokens, no passwords. Anything in the .exe can be read by anyone. `SUPPORT_APP_KEY` is deliberately not a secret, and a UPI ID is public by design.
- **Never lock a feature behind a donation.** Donations must stay voluntary; a paid unlock would make it a sale, which brings payment-provider rules back.
- **Don't run `build.bat` yourself**: it opens the app and waits for a key press at the end. Ask me to run it, or run the PyInstaller command from it directly.
- After every change: `python VTeamScreenRecorder.py --selftest` must print `ALL TESTS PASSED`. Bump `APP_VERSION` and add a dated `CHANGELOG.md` entry (plain English: New / Changed / Fixed / Tested / Not yet tested).

## My PC
Windows 10/11 64-bit (HP laptop), Python 3.12, `.venv` created by `build.bat`. FFmpeg comes from winget. The app calls the installed `ffmpeg.exe` / `ffprobe.exe`; never bundle them.

## Where things live in `VTeamScreenRecorder.py`
- **Top constants:** `APP_VERSION`, `GITHUB_URL`, `SUPPORT_EMAIL = "vteam732@gmail.com"`, `SUPPORT_URL` (Google Apps Script `/exec` URL, empty = email fallback), `SUPPORT_APP_KEY = "vd-support-v1"`, `DONATE_UPI_ID` (empty = the Donate dialog points to the website), `DONATE_PAYEE`, `DONATE_AMOUNTS = (20, 50, 100, 200, 300)`, `DONATE_DEFAULT`, `DONATE_PAGE`, `DONATE_NOTE` ("VTeam Screen Recorder voluntary donation"), `CAM_SIZES`, `CAM_STYLES`, mic thresholds (`MIC_VOICE_DB = -45`, `MIC_LONG_TALK_S = 2.5`, `MIC_SILENCE_PEAK`), `BAR_*` colours, `PAUSE_AMBER`.
- **Data:** `recorder-settings.json` and `tools\` sit next to the .exe (`data_dir()`), falling back to `%LOCALAPPDATA%\VTeamScreenRecorder` when that folder isn't writable. The per-PC **support ID** (`make_support_id`, like `7F3K-92QD`) is stored there.
- **Donate:** `is_upi_id`, `donate_amount` (clamps to ₹20–₹300), `upi_link` (`upi://pay?pa=…&pn=…&am=…&cu=INR&tn=…`), `upi_qr_png` (segno). The website's QR images are made from the same functions by `scripts/make_donate_qr.py <upi-id>`.
- **Update check:** `UPDATE_REPO`, `fetch_latest_release` (GitHub `releases/latest`, public, no key, never raises), `parse_version`, `is_newer`, `safe_release_page` (only this app's own releases page is ever opened); App `_maybe_check_updates` (4 s after start, at most once a day, cached in settings `update_last` / `update_latest` / `update_page`), the green `btn_newver` pill in the top bar, and the on/off checkbox (`update_check`) in the FFMPEG dropdown. Releases must be tagged `vX.Y.Z` (pre-releases are ignored) for the check to see them.
- **Support:** `build_support_payload`, `send_support` (returns ok, user message, detail; never raises), `support_mailto`, `normalize_screenshot` (re-encodes as JPEG ≤2560 px, which also proves it's a picture). The relay is `support-server/Code.gs` (setup in `support-server/SETUP.md`): MailApp to my Gmail, CacheService flood guard, app key check.
- **Recorder:** `ScreenRecorder` (`_launch`, `pause`, `resume`, `_finish_capture`), `AudioCapture`, `MicMeter`, `probe_span` / `span_from_probe`, `build_capture_cmd`, `build_mux_cmd_pieces`.
- **Camera:** `CameraFeed` (FFmpeg DirectShow → rawvideo pipe), `parse_dshow_cameras`, `list_cameras`, `camera_error_text`, `Segmenter` (ONNX), `clean_mask`.
- **GUI (`App`):** header, FFMPEG dropdown (`_toggle_tools_menu`), support ID label (click = copy), `☕ Tip the developer` (`_show_donate`), `💬 Support` (`_show_support`), SAVE TO, the big `⏺ Record screen` button (`btn_record`, styles `REC_BTN_IDLE` / `REC_BTN_ACTIVE`), result card (`_done`), error card, log, recorder bar (`_show_rec_bar`, `_rec_make_bar`, `_rec_*`, `_mic_*`, `_cam_*`), `destroy()` override that closes the camera and mic meter.

## Look
White theme, Segoe UI, red brand colour `#CF222E` (title, Record button), green `#1A7F37` for OK/primary actions, light-grey panels `#F6F8FA` with `#D0D7DE` borders, monospace labels and log. Window 820×660 (min 760×600). Header: "⏺ VTeam Screen Recorder" + version, **❔ How to use**, **`</> GitHub`**. Row under it: **FFMPEG ▾**, the support ID, and on the right **☕ Tip the developer**, **💬 Support**.

## Donations
- **☕ Tip the developer window ("Buy me a chai"):** amount buttons ₹20 / ₹50 / ₹100 / ₹200 / ₹300 (default ₹50), the consent tick box (`DONATE_CONSENT`, exact wording agreed with me; **QR, UPI ID and Copy stay off until it's ticked; the tick is never saved**, same on the website), payment note `DONATE_NOTE = "VTeam Screen Recorder voluntary donation"`, a UPI QR code for the chosen amount, the UPI ID with a Copy button, a line that donations are voluntary and unlock nothing, and a link to the website's donate box. Without `segno` it shows the UPI ID and amount as text; with an empty `DONATE_UPI_ID` it says donations open soon.
- Payments go straight to my bank account by UPI: no payment company, no licence keys, no server.
- If a payment provider is ever added, check its policy first (Dodo Payments, for example, bans donations without a product).

## Screen recorder (v2 behaviour to keep)
- **Compact dark bar** at the top centre (`overrideredirect`, always on top, draggable, black like the Snipping Tool), icons only with hover tooltips: **● Start / ■ Stop**, **pause** (icon only, right next to Start), timer, **mic**, **PC sound**, **camera ▾**, **⚙**, **✕**. Icons come from Segoe Fluent Icons / Segoe MDL2 Assets (`\uE720` mic, `\uEC54` mic off, `\uE767` volume, `\uE74F` mute, `\uE722` camera, `\uE70D` chevron, `\uE713` gear, `\uE711` close, `\uE769` pause, `\uE768` play) with emoji fallback. Tooltips must close on their own (pointer check every 200 ms) and never show in recordings.
- The bar and main window are excluded from capture: `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE=0x11)` via `wm_frame()`. The camera bubble **is** recorded.
- **Mic icon is a live meter** (listen-only `MicMeter` before Start, `AudioCapture` levels during): grey when quiet, green (brighter when louder) above `MIC_VOICE_DB`, red after `MIC_LONG_TALK_S` of continuous talking or when very loud. Log the mic device name. A silent mic track → "⚠ The microphone recorded only silence" + what to check (mute key, Sound > Input, mic privacy).
- **Camera ▾:** Bubble (round, `-transparentcolor` key `#FE00FE`) or **Cutout (no background)**, sizes Small/Medium/Large/Extra large (180/240/320/420 px at 100%), which camera. Picking a style turns the camera on. Cutout = MediaPipe selfie segmentation ONNX (`selfie_segmentation.onnx`, Apache 2.0) via onnxruntime, autocontrast on the model input, motion-adaptive smoothing, `clean_mask` (opening + closing). If onnxruntime or the model is missing, say so and use the bubble. Always kill the camera FFmpeg process when the camera goes off, the bar closes, a recording ends or the app quits.
- **Capture:** pick the pipeline once and cache it: encoders `h264_nvenc` → `h264_qsv` → `h264_amf` → `libx264`; capture `ddagrab` → `gdigrab`. One command builder for the probe and the real recording. Never put `-thread_queue_size` on the input (broke FFmpeg 7+). Screen input uses `-use_wallclock_as_timestamps 1` and `-copyts`; stop gracefully by writing `q` to stdin.
- **Sound:** `soundcard` (WASAPI loopback + mic, 48 kHz). Import it only inside audio threads via `load_soundcard`; **don't call `CoInitializeEx` before soundcard's first import** (that's what broke sound in v1.2.0/1.2.1). Fill gaps >100 ms with silence; drop samples if the device clock runs >100 ms fast.
- **Pause:** each stretch is its own video piece. On save, the pieces are joined with the concat demuxer (with `duration` lines), and every audio track is cut to the same clock spans (`asplit` / `atrim` / `apad=whole_dur` / `atrim=end`). Matroska's format "duration" with `-copyts` is the absolute **end** time, so `span_from_probe` subtracts the start. Elapsed time excludes pauses. If capture won't restart after a pause, save what was recorded.
- Output: `Screen Recording YYYY-MM-DD HH-MM-SS.mp4` (never overwrite), video copied, AAC 192k, `+faststart`. If the MP4 fails, keep the raw MKV and say where it is.

## FFmpeg
Find FFmpeg / ffprobe in `tools\`, PATH (re-read from the registry), WinGet Links/Packages, Scoop, Chocolatey, `C:fmpegin`. Missing → amber banner + **Install automatically** (winget silent first, then the official GitHub build into `tools\`, extracting only ffmpeg.exe and ffprobe.exe).

## Build and release
- `build.bat` → `.venv` → `pip install customtkinter pillow soundcard segno pyinstaller` (+ optional `onnxruntime>=1.17`) → `--selftest` → PyInstaller `--onefile --windowed --name VTeamScreenRecorder` with `icon.ico`, customtkinter data, the PIL hidden imports, soundcard, `--hidden-import segno`, and (when onnxruntime is installed) `--collect-binaries onnxruntime --add-data selfie_segmentation.onnx;. --add-data selfie_segmentation.LICENSE.txt;.` → installs to `%LOCALAPPDATA%\Programs\VTeamScreenRecorder` + shortcuts. No UAC prompt by default; `build.bat admin` adds `--uac-admin`. The .exe is about 50 MB (onnxruntime is about 30 MB of that).
- **Release:** bump `APP_VERSION`, update `CHANGELOG.md`, build, commit, tag `vX.Y.Z`, then create the GitHub release with `dist\VTeamScreenRecorder.exe` attached and the changelog entry, the SHA-256 of the .exe (`Get-FileHash -Algorithm SHA256`) and the SmartScreen note ("More info → Run anyway" until the app is code-signed).
- **Never commit:** `.venv/ build/ dist/ *.spec __pycache__/ settings.json tools/ recordings, .write-test-*, *.bak`, or any secret. `.gitignore` already covers these.

## Testing before you say it's done
1. `--selftest` passes.
2. Record 20 s with PC sound + mic + a flash-and-beep (or clap) sync check; once with a pause in the middle (paused part gone, sound still in sync); the bar not visible in the video; camera bubble and cutout visible; camera light off afterwards.
3. The tip window opens unticked; QR, UPI ID and Copy stay off until ticked; reopening starts unticked.
4. If you built the .exe: it opens without a console or UAC prompt, the guide shows, and one recording works from the .exe.
5. Tell me in plain English what you tested, what passed, what you couldn't test, and what you changed.
