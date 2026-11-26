# Third-party notices

VTeam Screen Recorder is © 2026 Shiva Kaja (see [LICENSE](LICENSE)). It is built on the open-source work below. Each component keeps its own licence. The versions listed are the ones used to build the v1.x releases; the licences were read from each installed package's metadata.

## Packed into `VTeamScreenRecorder.exe`

| Component | Version | Licence |
|---|---|---|
| Python runtime | 3.12 | Python Software Foundation License |
| Tcl/Tk (via Python's tkinter) | 8.6 | Tcl/Tk License (BSD-style) |
| [customtkinter](https://github.com/TomSchimansky/CustomTkinter) | 6.0.0 | MIT |
| [darkdetect](https://github.com/albertosottile/darkdetect) | 0.8.0 | BSD-3-Clause |
| [packaging](https://github.com/pypa/packaging) | 26.4 | Apache-2.0 OR BSD-2-Clause |
| [Pillow](https://python-pillow.org/) | 13.0.0 | MIT-CMU |
| [SoundCard](https://github.com/bastibe/SoundCard) | 0.4.6 | BSD-3-Clause |
| [segno](https://github.com/heuer/segno) (QR codes for the Tip window) | 1.6.6 | BSD-3-Clause |
| [cffi](https://cffi.readthedocs.io/) | 2.1.1 | MIT-0 |
| [pycparser](https://github.com/eliben/pycparser) | 3.12 | BSD-3-Clause |
| [NumPy](https://numpy.org/) | 2.5.5 | BSD-3-Clause (with 0BSD, MIT, Zlib, CC0-1.0 parts) |
| [ONNX Runtime](https://onnxruntime.ai/) | 1.32.0 | MIT |
| [FlatBuffers](https://github.com/google/flatbuffers) (ONNX Runtime dependency) | 25.12.19 | Apache-2.0 |
| [Protocol Buffers](https://github.com/protocolbuffers/protobuf) (ONNX Runtime dependency) | 7.36.2 | BSD-3-Clause |
| MediaPipe Selfie Segmentation model (`selfie_segmentation.onnx`), by Google, ONNX conversion by [onnx-community](https://huggingface.co/onnx-community/mediapipe_selfie_segmentation) | - | Apache-2.0. Full text: [selfie_segmentation.LICENSE.txt](selfie_segmentation.LICENSE.txt) |
| [PyInstaller](https://pyinstaller.org/) bootloader | 6.22.3 | GPL-2.0-or-later **with an exception** that allows distributing programs built with it, including commercial ones |

## Not packed in: installed on your PC from its official source

| Component | Licence | How the app uses it |
|---|---|---|
| [FFmpeg](https://ffmpeg.org/) | LGPL-2.1-or-later / GPL (depends on the build) | Runs the `ffmpeg.exe` installed on your PC (winget `Gyan.FFmpeg`, or an official build from GitHub) to capture the screen, read the camera and save the MP4 |

The app calls FFmpeg as a separate program; it doesn't include or modify its code.
