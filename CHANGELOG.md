# Changelog

## v1.0.0 (2026-11-26)

**First release: the recorder from Video Downloader v2.1.0, as its own small app.**

- **Main window:** SAVE TO folder (`Videos\Screen Recordings` at first), a big **⏺ Record screen** button, the result of the last recording (**▶ Play video**, **Show in folder**), and a log. FFmpeg status at the top, with **Install automatically** if it's missing.
- **Compact dark recording bar** (like the Windows Snipping Tool, icons only): Start/Stop, **pause / resume**, timer, **mic** (live level: grey / green / red), **PC sound**, **camera ▾** (Bubble or Cutout, four sizes, which camera), **⚙** (30/60 fps, Standard/High, mouse pointer, minimise while recording). The bar and the window never show in the video.
- **Sound and picture in sync**, also across pauses; a silent mic is flagged after recording.
- **☕ Tip the developer** ("Buy me a chai"): ₹20–₹300 by UPI, with the voluntary-gift tick box. The QR code, UPI ID and Copy only appear after ticking; the tick is never saved. Payment note "VTeam Screen Recorder voluntary donation". Tips unlock nothing.
- **💬 Support** form (same relay as Video Downloader; messages start with "VTeam Screen Recorder:").
- **Update notifications:** once a day the app asks GitHub for the newest release and shows a green **⬆ New version v…** button when there is one (opens the release page). Nothing personal is sent; turn it off under **FFMPEG ▾**.
- No downloader code: only FFmpeg is needed.

**Tested:** the self-test (recorder commands, names, camera through FFmpeg with a test picture, cutout mask clean-up, mic meter, pause joining, support form with a local relay, UPI tips), all passing. 9 update-check self-tests (including a local fake GitHub) and 9 window checks for the ⬆ New version button. 18/18 window checks: FFmpeg missing / found, tip window gating, guide, recording bar, a real recording with a pause in the middle (2 flashes and 2 beeps kept, the paused one cut out, sound lined up within 63 ms), result card, Record button back to normal. Not yet tested: the Windows .exe, a real webcam and microphone, the Windows icon font.
