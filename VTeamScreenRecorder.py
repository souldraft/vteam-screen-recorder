#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VTeam Screen Recorder - a free Windows screen recorder (white theme).

Records the full screen as ONE MP4 with PC sound and/or your microphone, kept in sync,
with an optional webcam bubble or background-free cutout, and pause/resume.
A compact dark bar (like the Windows Snipping Tool) controls it, and never shows in the video.

  * Uses the installed ffmpeg.exe (the app installs it for you if missing)
  * Encodes on the graphics card when possible (NVENC / Quick Sync / AMF), CPU as fallback
  * Free; an optional "Tip the developer" window takes UPI tips (they unlock nothing)

Run:        python VTeamScreenRecorder.py
Self-test:  python VTeamScreenRecorder.py --selftest
"""

from __future__ import annotations

import atexit
import base64
import ctypes
import glob
import json
import os
import platform
import posixpath
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from dataclasses import dataclass
from datetime import datetime

APP_NAME = "VTeam Screen Recorder"
APP_VERSION = "1.0.0"
APP_ID = "Shiva.VTeamScreenRecorder"
IS_WIN = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0

# In a windowed .exe there is no console: make print() safe.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


# =============================================================================
# Paths and settings
# =============================================================================
def app_dir() -> str:
    """Folder that holds the .exe (or this .py file)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name: str) -> str:
    """Files bundled inside the one-file .exe (icon)."""
    return os.path.join(getattr(sys, "_MEIPASS", app_dir()), name)


def _is_writable(folder: str) -> bool:
    try:
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, f".write-test-{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def data_dir() -> str:
    """settings.json and tools/ live next to the app; fall back to LocalAppData if that folder is read-only."""
    here = app_dir()
    if _is_writable(here):
        return here
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    folder = os.path.join(base, "VTeamScreenRecorder")
    os.makedirs(folder, exist_ok=True)
    return folder


DATA_DIR = data_dir()
SETTINGS_FILE = os.path.join(DATA_DIR, "recorder-settings.json")
TOOLS_DIR = os.path.join(DATA_DIR, "tools")


def known_folder(guid_text: str, fallback: str) -> str:
    """A Windows known folder (works even if it was moved to another drive or OneDrive)."""
    if IS_WIN:
        try:
            guid = uuid.UUID(guid_text)
            buf = (ctypes.c_ubyte * 16).from_buffer_copy(guid.bytes_le)
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(buf), 0, None, ctypes.byref(out)) == 0:
                path = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                if path:
                    return path
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), fallback)


def downloads_folder() -> str:
    return known_folder("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads")  # FOLDERID_Downloads


def videos_folder() -> str:
    return known_folder("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "Videos")  # FOLDERID_Videos


def default_save_dir() -> str:
    return os.path.join(videos_folder(), "Screen Recordings")


class Settings:
    def __init__(self, path: str):
        self.path = path
        self.data: dict = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data = loaded
        except (OSError, ValueError):
            self.data = {}

    @property
    def first_run(self) -> bool:
        return not self.data.get("save_dir")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


# =============================================================================
# Small helpers
# =============================================================================
def decode(b) -> str:
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    return b.decode("utf-8", errors="replace")


def _popen_kwargs() -> dict:
    """Hidden background processes: no black console windows."""
    kw: dict = {}
    if IS_WIN:
        kw["creationflags"] = NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    else:
        kw["start_new_session"] = True
    return kw


def run_hidden(cmd: list, timeout: float | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           stdin=subprocess.DEVNULL, timeout=timeout, **_popen_kwargs())
        return p.returncode, decode(p.stdout), decode(p.stderr)
    except subprocess.TimeoutExpired as e:
        return -9, decode(e.stdout), decode(e.stderr) + "\nTimed out"
    except OSError as e:
        return -1, "", str(e)


def kill_tree(proc: subprocess.Popen):
    """Stop a process and everything it started (otherwise ffmpeg keeps files locked)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=15, **_popen_kwargs())
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def remove_tree(path: str, tries: int = 12) -> bool:
    """Delete a folder, retrying while Windows releases file locks."""
    for _ in range(tries):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return True
        time.sleep(0.5)
    return False


def remove_file(path: str, tries: int = 10) -> bool:
    for _ in range(tries):
        try:
            if os.path.exists(path):
                os.remove(path)
            return True
        except OSError:
            time.sleep(0.5)
    return not os.path.exists(path)


def hide_folder(path: str):
    if IS_WIN:
        try:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)  # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass


def is_admin() -> bool:
    if not IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def exclude_from_capture(window, on: bool = True) -> bool:
    """Hide a window from screen recordings (Windows 10 2004+). Returns True if Windows accepted it."""
    if not IS_WIN:
        return False
    try:
        hwnd = int(window.wm_frame(), 16)
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11 if on else 0))  # WDA_EXCLUDEFROMCAPTURE
    except Exception:
        return False


def human_size(n: float | None) -> str:
    if not n:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def file_ext(name: str) -> str:
    base = posixpath.basename((name or "").replace("\\", "/"))
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Make any title safe as a Windows file name."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    name = re.sub(r"(?i)\.(mp4|mp3|mkv|webm|m4a|mov)$", "", name).strip()
    if name.split(".")[0].strip().upper() in WIN_RESERVED:
        name = "_" + name
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name or "video"


def unique_base(folder: str, base: str, ext: str) -> str:
    """Never overwrite: 'name', 'name (2)', 'name (3)' ..."""
    candidate, n = base, 2
    while os.path.exists(os.path.join(folder, f"{candidate}.{ext}")):
        candidate = f"{base} ({n})"
        n += 1
    return candidate


# =============================================================================
# Files
# =============================================================================

EXT_KINDS = {
    "a Windows program": {"exe", "msi", "msix", "msixbundle", "appx", "appxbundle", "com", "scr", "pif",
                          "cpl", "dll", "sys", "drv", "ocx", "efi", "application", "gadget"},
    "a script": {"bat", "cmd", "ps1", "psm1", "psd1", "vbs", "vbe", "js", "jse", "wsf", "wsh", "hta",
                 "msc", "reg", "inf", "lnk", "url", "scf", "jar", "py", "pyw", "sh", "chm",
                 "library-ms", "settingcontent-ms"},
    "an archive": {"zip", "rar", "7z", "cab", "iso", "img", "vhd", "vhdx", "tar", "gz", "tgz", "bz2",
                   "xz", "arj", "ace", "dmg"},
    "a document with macros": {"docm", "xlsm", "pptm", "dotm", "xlam"},
    "an app installer": {"apk", "xapk", "pkg", "deb", "rpm", "appimage"},
}
DANGEROUS_EXTS = set().union(*EXT_KINDS.values())
MEDIA_EXTS = {"mp4", "m4a", "m4v", "mp3", "mkv", "webm", "mov", "avi", "ts", "aac", "opus", "ogg", "wav", "flac"}


def ext_kind(ext: str) -> str:
    for kind, exts in EXT_KINDS.items():
        if ext in exts:
            return kind
    return "not a video"


# =============================================================================
# Check that a saved file is real video
# =============================================================================

BINARY_SIGNATURES = [
    (b"MZ", "a Windows program (.exe/.dll)"),
    (b"\x7fELF", "a Linux program"),
    (b"\xcf\xfa\xed\xfe", "a Mac program"),
    (b"\xce\xfa\xed\xfe", "a Mac program"),
    (b"\xca\xfe\xba\xbe", "a Java/Mac program"),
    (b"PK\x03\x04", "a ZIP archive"),
    (b"PK\x05\x06", "a ZIP archive"),
    (b"Rar!\x1a\x07", "a RAR archive"),
    (b"7z\xbc\xaf\x27\x1c", "a 7-Zip archive"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "an installer or Office file (.msi/.doc)"),
    (b"MSCF", "a CAB archive"),
    (b"%PDF", "a PDF document"),
    (b"\x1f\x8b", "a compressed archive (.gz)"),
    (b"#!", "a script"),
    (b"{\\rtf", "an RTF document"),
]
TEXT_SIGNATURES = [
    ("<!doctype html", "a web page"), ("<html", "a web page"), ("<script", "a script"),
    ("@echo", "a Windows batch script"), ("powershell", "a PowerShell script"),
    ("on error resume next", "a VBScript"), ("wscript.", "a script"), ("<?php", "a script"),
]


def sniff_signature(head: bytes) -> str | None:
    """Return what the bytes really are if they're NOT media, else None."""
    if not head:
        return None
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    for sig, kind in BINARY_SIGNATURES:
        if head.startswith(sig):
            return kind
    text = head[:512].decode("latin-1", errors="ignore").lstrip().lower()
    for sig, kind in TEXT_SIGNATURES:
        if text.startswith(sig):
            return kind
    return None




def verify_media_file(path: str, ffprobe: str | None) -> tuple[bool, str, dict]:
    """Prove the saved file is real video/audio (not a program wearing a .mp4 name)."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError as e:
        return False, f"the saved file can't be read ({e})", {}
    threat = sniff_signature(head)
    if threat:
        return False, f"the server sent {threat} instead of a video", {}
    ext = file_ext(path)
    if ext in ("mp4", "m4a", "m4v", "mov") and b"ftyp" not in head[:64]:
        return False, "the file is not a real MP4 video", {}
    if ext == "mp3" and not (head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)):
        return False, "the file is not a real MP3", {}
    details: dict = {}
    if ffprobe:
        rc, out, _err = run_hidden([ffprobe, "-v", "error", "-print_format", "json",
                                    "-show_format", "-show_streams", path], timeout=90)
        if rc != 0:
            return False, "the file can't be opened as video or audio", {}
        try:
            data = json.loads(out or "{}")
        except ValueError:
            data = {}
        streams = data.get("streams") or []
        video = [s for s in streams if s.get("codec_type") == "video"
                 and not (s.get("disposition") or {}).get("attached_pic")]
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        if not video and not audio:
            return False, "the file has no video or audio inside", {}
        fmt = data.get("format") or {}
        try:
            duration = float(fmt.get("duration")) if fmt.get("duration") else None
        except ValueError:
            duration = None
        details = {
            "width": video[0].get("width") if video else None,
            "height": video[0].get("height") if video else None,
            "vcodec": video[0].get("codec_name") if video else None,
            "acodec": audio[0].get("codec_name") if audio else None,
            "duration": duration,
        }
    return True, "", details


# =============================================================================
# FFmpeg: find, install
# =============================================================================

TOOLS = {
    "ffmpeg": {
        "label": "FFmpeg", "winget": "Gyan.FFmpeg", "required": True,
        "url": "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "kind": "zip", "files": ["ffmpeg.exe", "ffprobe.exe"], "version_args": ["-version"],
    },
}


def winget_command(key: str) -> str:
    return f"winget install --id {TOOLS[key]['winget']} -e"


def refresh_path():
    """Pick up PATH changes made by winget without restarting the app."""
    if not IS_WIN:
        return
    try:
        import winreg
        extra = []
        for root, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, r"Environment")):
            try:
                with winreg.OpenKey(root, sub) as k:
                    value, _ = winreg.QueryValueEx(k, "Path")
                    extra += [os.path.expandvars(x.strip()) for x in str(value).split(";") if x.strip()]
            except OSError:
                pass
        current = [x for x in os.environ.get("PATH", "").split(os.pathsep) if x]
        seen, merged = set(), []
        for x in current + extra:
            key = os.path.normcase(os.path.normpath(x))
            if key not in seen:
                seen.add(key)
                merged.append(x)
        os.environ["PATH"] = os.pathsep.join(merged)
    except Exception:
        pass


def _extra_tool_dirs() -> list[str]:
    """Places tools often live when they aren't on PATH (winget, scoop, choco, pip)."""
    if not IS_WIN:
        return []
    home = os.path.expanduser("~")
    la = os.environ.get("LOCALAPPDATA", "")
    ad = os.environ.get("APPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pd = os.environ.get("ProgramData", r"C:\ProgramData")
    dirs = [os.path.join(la, "Microsoft", "WinGet", "Links"), os.path.join(pf, "WinGet", "Links"),
            os.path.join(home, "scoop", "shims"), os.path.join(pd, "chocolatey", "bin"),
            os.path.join(home, ".deno", "bin"), os.path.join(la, "Microsoft", "WindowsApps"),
            r"C:\ffmpeg\bin", os.path.join(pf, "ffmpeg", "bin")]
    dirs += glob.glob(os.path.join(la, "Programs", "Python", "Python3*", "Scripts"))
    dirs += glob.glob(os.path.join(ad, "Python", "Python3*", "Scripts"))
    return dirs


def find_tool(name: str) -> str | None:
    exe = name + (".exe" if IS_WIN else "")
    local = os.path.join(TOOLS_DIR, exe)
    if os.path.isfile(local):
        return local
    found = shutil.which(name)
    if found:
        return found
    if IS_WIN:
        la = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        for d in _extra_tool_dirs():
            cand = os.path.join(d, exe)
            if os.path.isfile(cand):
                return cand
        for root in (os.path.join(la, "Microsoft", "WinGet", "Packages"), os.path.join(pf, "WinGet", "Packages")):
            hits = (glob.glob(os.path.join(root, "*", exe)) + glob.glob(os.path.join(root, "*", "*", exe))
                    + glob.glob(os.path.join(root, "*", "*", "bin", exe)))
            if hits:
                return sorted(hits)[-1]
    return None


def find_ffprobe(ffmpeg: str | None) -> str | None:
    if ffmpeg:
        folder = os.path.dirname(ffmpeg)
        cand = os.path.join(folder, "ffprobe" + (".exe" if IS_WIN else ""))
        if os.path.isfile(cand):
            return cand
    return find_tool("ffprobe")


def tool_version(key: str, path: str) -> str:
    rc, out, err = run_hidden([path] + TOOLS[key]["version_args"], timeout=30)
    first = (out or err).strip().splitlines()[0] if (out or err).strip() else ""
    if key == "ffmpeg":
        m = re.search(r"version\s+(\S+)", first)
        v = m.group(1) if m else first
        num = re.match(r"n?(\d+\.\d+(?:\.\d+)?)", v)
        date = re.match(r"(\d{4}-\d{2}-\d{2})", v)
        return num.group(1) if num else (date.group(1) if date else v[:12])
    return first[:24]


def detect_tools() -> dict:
    refresh_path()
    tools = {key: find_tool(key) for key in TOOLS}
    tools["ffprobe"] = find_ffprobe(tools["ffmpeg"])
    return tools


def install_tool(key: str, log) -> bool:
    """winget first (so 'winget upgrade' keeps it updated); direct GitHub download as fallback."""
    spec = TOOLS[key]
    winget = shutil.which("winget") if IS_WIN else None
    if winget:
        log(f"Installing {spec['label']} with winget (about a minute)…")
        rc, out, err = run_hidden([winget, "install", "--id", spec["winget"], "-e", "--silent",
                                   "--accept-package-agreements", "--accept-source-agreements"], timeout=1200)
        refresh_path()
        if find_tool(key):
            log(f"{spec['label']} installed.", "ok")
            return True
        log(f"winget couldn't finish installing {spec['label']} (code {rc}). Trying a direct download…", "warn")
    if not IS_WIN:
        log("Automatic install only works on Windows.", "err")
        return False
    try:
        return _direct_download(key, log)
    except Exception as e:
        log(f"Couldn't download {spec['label']}: {e}", "err")
        return False


def _direct_download(key: str, log) -> bool:
    spec = TOOLS[key]
    os.makedirs(TOOLS_DIR, exist_ok=True)
    tmp = os.path.join(TOOLS_DIR, f".{key}.download")
    log(f"Downloading {spec['label']} from its official GitHub release…")
    req = urllib.request.Request(spec["url"], headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, -25
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                if pct >= last + 25:
                    log(f"  {spec['label']}: {pct}% of {human_size(total)}")
                    last = pct
    if spec["kind"] == "exe":
        os.replace(tmp, os.path.join(TOOLS_DIR, spec["files"][0]))
    else:
        with zipfile.ZipFile(tmp) as z:
            for member in z.namelist():
                base = posixpath.basename(member)
                if base in spec["files"]:  # extract only the files we need, never paths from the zip
                    with z.open(member) as src, open(os.path.join(TOOLS_DIR, base), "wb") as dst:
                        shutil.copyfileobj(src, dst)
        os.remove(tmp)
    ok = find_tool(key) is not None
    log(f"{spec['label']} installed into the app's tools folder." if ok else f"{spec['label']} install failed.",
        "ok" if ok else "err")
    return ok


# =============================================================================
# Support form + Tip the developer
# =============================================================================

GITHUB_URL = "https://github.com/souldraft?tab=repositories"  # the GitHub button: all my apps

# ---- Support form + Tip the developer ----
SUPPORT_EMAIL = "vteam732@gmail.com"
SUPPORT_URL = "https://script.google.com/macros/s/AKfycbzA3HB7hV-zmxkvKJ0OFz1NisE3Zg7Y-iAioJdbe_nyguZ_z8chWp63EAdrA4JVs0mASg/exec" # Google Apps Script web-app URL (ends in /exec), see support-server/SETUP.md. Empty = email fallback.
SUPPORT_APP_KEY = "vd-support-v1"  # not a secret, only stops random web bots; must match APP_KEY in support-server/Code.gs
# Donations: everything in the app is free; donating is optional. Paid by UPI (GPay, PhonePe, Paytm, BHIM...).
DONATE_UPI_ID = "shivaabhi7796-6@okicici"  # empty = the Donate dialog sends people to the website instead
DONATE_PAYEE = "VTeam Screen Recorder"  # shown in the UPI app next to the bank account's own name
DONATE_AMOUNTS = (20, 50, 100, 200, 300)  # rupees; fixed buttons keep every donation between ₹20 and ₹300
DONATE_DEFAULT = 50
DONATE_PAGE = "https://souldraft.github.io/vteam-screen-recorder/#donate"
DONATE_NOTE = "VTeam Screen Recorder voluntary donation"  # the payment note people see in their UPI app
# Must be ticked before the QR / UPI ID / Copy are shown. Never saved: it starts unticked every time.
DONATE_CONSENT = ("I'm giving this voluntarily, from my own bank account, to support the developer. I understand it "
                  "doesn't buy or unlock anything, and it's not refundable")
SUPPORT_SUBJECTS = ("Recording problem", "Camera or microphone", "Donation question", "Feature request",
                    "Something else")
SUPPORT_IMAGE_MAX_SIDE = 2560
SUPPORT_COOLDOWN_S = 60
_SID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O/1/I: easy to read out over the phone
_SID_RE = re.compile(r"^[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def make_support_id() -> str:
    """Random per-PC ID like 7F3K-92QD. Not linked to the person, only to this install."""
    raw = "".join(secrets.choice(_SID_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def is_support_id(value) -> bool:
    return isinstance(value, str) and bool(_SID_RE.match(value))


def normalize_screenshot(data: bytes) -> bytes:
    """Re-encode any picture as a JPEG (max 2560 px). Also proves the file really is a picture."""
    import io as _io
    from PIL import Image as _Image
    with _Image.open(_io.BytesIO(data)) as src:
        im = src.convert("RGB")
    im.thumbnail((SUPPORT_IMAGE_MAX_SIDE, SUPPORT_IMAGE_MAX_SIDE))
    out = _io.BytesIO()
    im.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def build_support_payload(subject, email, message, support_id, diagnostics="", image_jpeg=None) -> dict:
    payload = {"key": SUPPORT_APP_KEY, "subject": f"{APP_NAME}: {subject}"[:80], "email": str(email).strip()[:200],
               "message": str(message).strip()[:8000], "support_id": str(support_id)[:20],
               "app_version": APP_VERSION, "diagnostics": str(diagnostics)[-20000:]}
    if image_jpeg:
        payload["image_b64"] = base64.b64encode(image_jpeg).decode("ascii")
        payload["image_name"] = f"screenshot-{support_id}.jpg"
    return payload


def send_support(payload: dict, url=None, timeout: int = 45):
    """POST the Support form to the relay. Returns (ok, message_for_user, detail). Never raises."""
    url = SUPPORT_URL if url is None else url
    if not url:
        return False, "Sending from the app isn't switched on yet.", "SUPPORT_URL is empty"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    try:
        # Google Apps Script answers a POST with a redirect; urllib follows it with a GET to fetch the reply
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = decode(r.read(20000))
    except Exception as e:  # no internet, DNS, timeout, HTTP error
        return (False, "Couldn't reach the support server. Check your internet connection and try again.",
                f"{type(e).__name__}: {e}")
    try:
        reply = json.loads(body)
    except ValueError:
        return False, "The support server sent an unexpected reply.", body[:300]
    if reply.get("ok"):
        return True, "", ""
    return False, str(reply.get("error") or "The support server refused the message."), body[:300]


def support_mailto(subject: str, body: str) -> str:
    query = urllib.parse.urlencode({"subject": subject, "body": body[:1500]}, quote_via=urllib.parse.quote)
    return f"mailto:{SUPPORT_EMAIL}?{query}"


_UPI_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,255}@[A-Za-z][A-Za-z0-9.-]{1,63}$")


def is_upi_id(value) -> bool:
    """name@bank style UPI ID (virtual payment address)."""
    return isinstance(value, str) and bool(_UPI_ID_RE.match(value.strip()))


def donate_amount(amount) -> int:
    """Whole rupees, kept between the smallest and largest donation button (₹20 to ₹300)."""
    try:
        value = int(round(float(amount)))
    except (TypeError, ValueError):
        value = DONATE_DEFAULT
    return max(min(DONATE_AMOUNTS), min(max(DONATE_AMOUNTS), value))


def upi_link(upi_id: str, amount, payee: str = DONATE_PAYEE, note: str = DONATE_NOTE) -> str:
    """upi://pay link with the amount filled in; every UPI app (GPay, PhonePe, Paytm, BHIM) understands it."""
    if not is_upi_id(upi_id):
        raise ValueError("not a UPI ID")
    q = urllib.parse.quote
    return (f"upi://pay?pa={q(upi_id.strip(), safe='@.-_')}&pn={q(payee)}&am={donate_amount(amount)}.00"
            f"&cu=INR&tn={q(note)}")


def upi_qr_png(link: str, scale: int = 8) -> bytes:
    """QR code of a UPI link as PNG bytes. Needs the small 'segno' package (raises ImportError without it)."""
    import io as _io
    import segno
    buf = _io.BytesIO()
    segno.make(link, error="m").save(buf, kind="png", scale=scale, border=3, dark="#000000", light="#FFFFFF")
    return buf.getvalue()


# ---- Update check: once a day, ask GitHub for the newest release (public, no key; nothing personal is sent)
UPDATE_REPO = "souldraft/vteam-screen-recorder"
UPDATE_API = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
UPDATE_PAGE = f"https://github.com/{UPDATE_REPO}/releases/latest"
UPDATE_EVERY_S = 24 * 3600


def parse_version(text):
    """'v2.1.0' or '2.1' -> (2, 1, 0); anything else (pre-release tags, junk) -> None."""
    m = re.match(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?\s*$", str(text or ""))
    return tuple(int(x or 0) for x in m.groups()) if m else None


def is_newer(latest, current: str = APP_VERSION) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def safe_release_page(page) -> str:
    """Only ever open this app's own GitHub releases page."""
    page = str(page or "")
    return page if page.startswith(f"https://github.com/{UPDATE_REPO}/releases/") else UPDATE_PAGE


def fetch_latest_release(url: str = UPDATE_API, timeout: int = 10):
    """(version, releases page) of the newest published release, or None (offline, no release...). Never raises."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            info = json.loads(decode(r.read(300000)))
        tag = str(info.get("tag_name") or "")
        if info.get("draft") or info.get("prerelease") or not parse_version(tag):
            return None
        return tag.strip().lstrip("v"), safe_release_page(info.get("html_url"))
    except Exception:
        return None


class FriendlyError(Exception):
    def __init__(self, message: str, details: str = ""):
        super().__init__(message)
        self.message = message
        self.details = details


class Cancelled(Exception):
    pass


@dataclass
class Result:
    path: str
    size: int
    width: int | None
    height: int | None
    duration: float | None
    defender: str


# =============================================================================
# Screen recorder: capture, sound, camera, saving
# =============================================================================

REC_FPS = {"30 fps": 30, "60 fps": 60}
REC_QUALITY = {"Standard": False, "High": True}
CAM_SIZES = {"Small": 180, "Medium": 240, "Large": 320, "Extra large": 420}  # px at 100% Windows scaling
CAM_STYLES = ("Bubble", "Cutout (no background)")
SEG_MODEL_FILE = "selfie_segmentation.onnx"  # Google MediaPipe selfie segmentation (Apache 2.0), 0.5 MB
CAM_KEY = "#FE00FE"  # see-through colour around the round camera bubble (Windows); never part of the picture
_CAMERA_PROCS: set = set()


def hex_rgb(colour: str) -> tuple:
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


class Segmenter:
    """Finds the person in a camera frame and paints everything else in the see-through colour.
    No green screen needed. Runs in the camera thread, about 4 ms per frame on a laptop CPU."""

    def __init__(self, model_path: str, key_rgb: tuple):
        import numpy as np
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2  # leave the rest of the CPU for the recording
        opts.log_severity_level = 3
        self.np = np
        self.session = ort.InferenceSession(model_path, opts, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        self.key_rgb = key_rgb
        self.prev = None

    def cutout(self, frame: bytes, size: int) -> bytes:
        from PIL import Image as _Image, ImageFilter, ImageOps
        np = self.np
        bilinear = getattr(_Image, "Resampling", _Image).BILINEAR
        img = _Image.frombytes("RGB", (size, size), frame)
        small = ImageOps.autocontrast(img.resize((256, 256), bilinear), cutoff=1)  # helps the AI in dim rooms
        x = np.asarray(small, dtype=np.float32) / 255.0
        alpha = self.session.run(None, {self.input: x.transpose(2, 0, 1)[None]})[0][0, 0]
        if self.prev is not None and self.prev.shape == alpha.shape:
            # moving parts (hands) follow at once, so nothing trails behind them;
            # small flicker on still parts (hair, shoulders) is smoothed out
            w = np.clip(np.abs(alpha - self.prev) * 2.5, 0.45, 1.0)
            alpha = w * alpha + (1 - w) * self.prev
        self.prev = alpha
        mask = _Image.fromarray((np.clip(alpha, 0, 1) * 255).astype(np.uint8), "L")
        mask = clean_mask(mask.point(lambda v: 255 if v >= 110 else 0))
        mask = mask.resize((size, size), bilinear).filter(ImageFilter.GaussianBlur(1.0))
        mask = mask.point(lambda v: 255 if v >= 128 else 0)
        out = _Image.new("RGB", (size, size), self.key_rgb)
        out.paste(img, (0, 0), mask)
        return out.tobytes()


def clean_mask(mask):
    """Tidy a black/white person mask (256 px): drop small specks around you, close small holes inside you."""
    from PIL import ImageFilter
    m = mask.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))  # specks disappear
    return m.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))   # pinholes close up


MIC_SILENT = "#57606A"       # mic icon when it's quiet
MIC_VOICE_DB = -45.0         # louder than this counts as voice
MIC_LONG_TALK_S = 2.5        # this long without a pause turns the icon red
MIC_SILENCE_PEAK = 0.0015    # a whole recording quieter than this (about -56 dB) = the mic heard nothing


def mic_level_colour(db: float) -> str:
    """Mic icon colour for a sound level: grey when quiet, grey-to-bright-green with your voice, red when very loud."""
    if db >= -10:
        return "#FF4D4F"
    lo, hi = (0x57, 0x60, 0x6A), (0x3F, 0xE0, 0x6A)
    t = min(1.0, max(0.0, (db + 55.0) / 35.0))  # -55 dB = grey ... -20 dB = full green
    if t <= 0:
        return MIC_SILENT
    r, g, b = (int(a + (c - a) * t) for a, c in zip(lo, hi))
    return f"#{r:02X}{g:02X}{b:02X}"


def fmt_clock(seconds) -> str:
    """00:01:05 style timer for the recorder bar."""
    s = max(0, int(seconds or 0))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_dshow_cameras(text: str) -> list:
    """Webcam names from 'ffmpeg -list_devices true -f dshow -i dummy' (new and old FFmpeg layouts)."""
    names: list = []
    for m in re.finditer(r'"([^"]+)"\s*\(video\)', text):
        if m.group(1) not in names:
            names.append(m.group(1))
    if names:
        return names
    section = ""
    for line in text.splitlines():
        if "DirectShow video devices" in line:
            section = "video"
        elif "DirectShow audio devices" in line:
            section = "audio"
        elif section == "video" and "Alternative name" not in line:
            m = re.search(r'\]\s+"([^"]+)"\s*$', line)
            if m and m.group(1) not in names:
                names.append(m.group(1))
    return names


def list_cameras(ffmpeg: str) -> list:
    if not IS_WIN or not ffmpeg:
        return []
    _rc, out, err = run_hidden([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                               timeout=20)
    return parse_dshow_cameras(f"{out}\n{err}")


def camera_error_text(err: str, had_frames: bool = False) -> str:
    e = (err or "").lower()
    if had_frames:
        return "The camera stopped. Was it unplugged, or did another app take it?"
    if "could not find" in e or "no such" in e:
        return "That camera wasn't found. Pick another one under ⚙, or plug it in again."
    if ("access" in e and "denied" in e) or "permission" in e:
        return ("Windows blocked the camera. Turn on Settings > Privacy & security > Camera > "
                "'Let desktop apps access your camera'.")
    return ("Couldn't open the camera. If another app (Zoom, Teams, the Camera app) is using it, close that app. "
            "Also check Settings > Privacy & security > Camera > 'Let desktop apps access your camera'.")


class CameraFeed:
    """Webcam frames through FFmpeg (no extra libraries): square, mirrored like a selfie, raw RGB."""

    def __init__(self, ffmpeg: str, input_args: list, size: int, fps: int = 24, transform=None):
        self.size = size
        self.transform = transform  # e.g. background removal, run here so the window never waits for it
        self.cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", *input_args, "-an",
                    "-vf", f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size},hflip,fps={fps}",
                    "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        self.proc: subprocess.Popen | None = None
        self.frame: bytes | None = None
        self.frames = 0
        self.error = ""
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, **_popen_kwargs())
        _CAMERA_PROCS.add(self.proc)
        threading.Thread(target=self._read_frames, daemon=True).start()
        threading.Thread(target=self._read_errors, daemon=True).start()

    def _read_frames(self):
        need = self.size * self.size * 3
        while not self._stop.is_set():
            buf = self.proc.stdout.read(need)
            if not buf or len(buf) < need:
                break
            if self.transform:
                try:
                    buf = self.transform(buf)
                except Exception as e:  # keep the camera running, just without the effect
                    self.error = (self.error + f"background removal stopped: {e}\n")[-2000:]
                    self.transform = None
            self.frame = buf
            self.frames += 1

    def _read_errors(self):
        for raw in iter(self.proc.stderr.readline, b""):
            self.error = (self.error + decode(raw))[-2000:]

    def stop(self, wait: bool = True):
        """Release the camera right away; waiting for the process to exit can run in the background."""
        self._stop.set()
        p = self.proc
        if p is None:
            return
        _CAMERA_PROCS.discard(p)
        if p.poll() is not None:
            return
        try:
            p.kill()  # a preview has nothing to save, so stop it at once (frees the camera immediately)
        except OSError:
            pass

        def reap():
            try:
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except OSError:
                    pass
        if wait:
            reap()
        else:
            threading.Thread(target=reap, daemon=True).start()


def _kill_camera_procs():
    for p in list(_CAMERA_PROCS):  # never leave the webcam switched on after the app closes
        try:
            p.kill()
        except OSError:
            pass


atexit.register(_kill_camera_procs)
ENCODERS = ["h264_nvenc", "h264_qsv", "h264_amf", "libx264"]  # GPU first (cooler, faster), CPU last
AUDIO_RATE = 48000


def encoder_args(enc: str, fps: int, high: bool) -> tuple[list, str]:
    """FFmpeg arguments for a video encoder + the pixel format it wants."""
    q = 19 if high else 23
    gop = str(fps * 2)
    if enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(q), "-b:v", "0", "-g", gop], "yuv420p"
    if enc == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", str(q + 2), "-g", gop], "nv12"
    if enc == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q + 2),
                "-g", gop], "nv12"
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(q), "-g", gop], "yuv420p"


def capture_methods() -> list[str]:
    return ["ddagrab", "gdigrab"] if IS_WIN else ["x11grab"]


def capture_input(method: str, fps: int, mouse: bool) -> tuple[list, str]:
    """FFmpeg input arguments for screen capture + filters needed before encoding."""
    m = "1" if mouse else "0"
    if method == "ddagrab":   # Windows Desktop Duplication: fast, GPU based
        return ["-f", "lavfi", "-i", f"ddagrab=output_idx=0:framerate={fps}:draw_mouse={m}"], "hwdownload,format=bgra,"
    if method == "gdigrab":   # older Windows capture, works everywhere
        return ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", m, "-i", "desktop"], ""
    return ["-f", "x11grab", "-framerate", str(fps), "-draw_mouse", m, "-i", os.environ.get("DISPLAY", ":0")], ""


def video_filter(pre: str, pix: str) -> str:
    return f"{pre}crop=trunc(iw/2)*2:trunc(ih/2)*2,format={pix}"


def probe_encoder(ffmpeg: str, enc: str) -> bool:
    args, pix = encoder_args(enc, 30, False)
    rc, _, _ = run_hidden([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                           "-i", "color=c=black:s=640x360:r=30:d=0.5", "-vf", f"format={pix}"]
                          + args + ["-f", "null", "-"], timeout=40)
    return rc == 0


REC_PIPELINE_VERSION = 2  # bump when the recording command changes, so the PC is checked again


def build_capture_cmd(ffmpeg: str, method: str, enc: str, fps: int, high: bool, mouse: bool, output: list) -> list:
    """The screen-recording command. Used by the one-time check AND the real recording.
    Only options that every FFmpeg version accepts in these positions (FFmpeg 7+ made e.g.
    -thread_queue_size output-only, which broke the input side)."""
    inp, pre = capture_input(method, fps, mouse)
    args, pix = encoder_args(enc, fps, high)
    return ([ffmpeg, "-hide_banner", "-loglevel", "info", "-stats", "-use_wallclock_as_timestamps", "1"] + inp
            + ["-vf", video_filter(pre, pix)] + args + ["-copyts"] + output)


def probe_capture(ffmpeg: str, method: str, enc: str) -> bool:
    cmd = build_capture_cmd(ffmpeg, method, enc, 30, False, True, ["-t", "1", "-f", "null", "-"])
    rc, _, _ = run_hidden(cmd, timeout=40)
    return rc == 0


def choose_pipeline(ffmpeg: str, log=None) -> dict | None:
    """Find the best working (capture method, encoder) pair on this PC."""
    log = log or (lambda m, l="info": None)
    encoders = [e for e in ENCODERS if probe_encoder(ffmpeg, e)]
    if not encoders:
        return None
    for method in capture_methods():
        for enc in ([encoders[0], "libx264"] if encoders[0] != "libx264" else ["libx264"]):
            if enc in encoders and probe_capture(ffmpeg, method, enc):
                return {"capture": method, "encoder": enc, "ffmpeg": ffmpeg, "v": REC_PIPELINE_VERSION}
    return None


_SOUNDCARD_LOCK = threading.Lock()


def _com_init() -> bool:
    """Start COM (multithreaded) on the current thread. True = call _com_uninit() when the thread ends."""
    if not IS_WIN:
        return False
    try:
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0)
        return hr in (0, 1)  # S_OK or S_FALSE both need a matching CoUninitialize
    except Exception:
        return False


def _com_uninit():
    if IS_WIN:
        try:
            ctypes.windll.ole32.CoUninitialize()
        except Exception:
            pass


def load_soundcard() -> tuple:
    """Load the soundcard library inside an audio thread. Returns (module, must_uninit_com).

    soundcard starts COM itself when it is first imported, and treats Windows' "already started"
    answer (S_FALSE) as an error, so COM must NOT be started before that first import (that was the
    v1.2.0/1.2.1 "no sound" bug). Threads that come later start COM themselves."""
    with _SOUNDCARD_LOCK:
        if "soundcard" not in sys.modules:
            import soundcard  # first import: soundcard starts COM on this thread
            return soundcard, False
    return sys.modules["soundcard"], _com_init()


def default_audio_source(kind: str):
    """soundcard device: 'system' = what you hear (loopback), 'mic' = default microphone.
    Only called from an audio thread, after load_soundcard() (never on the Tk thread)."""
    sc = sys.modules["soundcard"]
    if kind == "system":
        return sc.get_microphone(id=str(sc.default_speaker().name), include_loopback=True)
    return sc.default_microphone()


class AudioCapture(threading.Thread):
    """Records one audio source to raw 16-bit PCM, keeping it in step with the system clock."""

    def __init__(self, kind: str, path: str, channels: int, source_factory=None):
        super().__init__(daemon=True)
        self.kind, self.path, self.channels = kind, path, channels
        self.factory = source_factory or default_audio_source
        self.t0: float | None = None     # system-clock time of the first sample
        self.frames = 0
        self.error: str | None = None
        self.level = -120.0   # latest loudness in dB (for the live mic icon)
        self.peak = 0.0       # loudest sample so far (0 = the mic heard nothing)
        self.device = ""
        self.started_ok = threading.Event()
        self._stop_ev = threading.Event()

    def stop(self):
        self._stop_ev.set()

    def run(self):
        com = False
        try:
            import warnings
            warnings.filterwarnings("ignore", message=".*discontinuity.*")
            if self.factory is default_audio_source:
                _, com = load_soundcard()
            import numpy as np
            source = self.factory(self.kind)
            self.device = str(getattr(source, "name", "") or "")
            block = AUDIO_RATE // 50  # 20 ms
            limit = AUDIO_RATE // 10  # correct drift beyond 100 ms
            with source.recorder(samplerate=AUDIO_RATE, channels=self.channels, blocksize=block) as rec, \
                    open(self.path, "wb") as f:
                while not self._stop_ev.is_set():
                    data = rec.record(numframes=block)
                    now = time.time()
                    n = len(data)
                    if self.t0 is None:
                        self.t0 = now - n / AUDIO_RATE
                        self.started_ok.set()
                    expected = int((now - self.t0) * AUDIO_RATE)
                    diff = expected - (self.frames + n)
                    if diff > limit:            # device stalled: fill the gap with silence
                        f.write(b"\x00" * (diff * self.channels * 2))
                        self.frames += diff
                    elif diff < -limit:         # device clock running fast: drop the extra
                        cut = min(n, -diff)
                        data = data[cut:]
                        n -= cut
                    if n:
                        arr = np.asarray(data, dtype="float32").reshape(n, -1)[:, :self.channels]
                        if arr.size:
                            self.peak = max(self.peak, float(np.max(np.abs(arr))))
                            rms = float(np.sqrt(np.mean(np.square(arr))))
                            self.level = float(20 * np.log10(max(rms, 1e-6)))
                        f.write((np.clip(arr, -1.0, 1.0) * 32767).astype("<i2").tobytes())
                        self.frames += n
        except Exception as e:
            self.error = str(e) or e.__class__.__name__
        finally:
            self.started_ok.set()
            if com:
                _com_uninit()


class MicMeter(threading.Thread):
    """Listens to the default microphone (nothing is saved) so the recorder bar can show your voice level."""

    def __init__(self, source_factory=None):
        super().__init__(daemon=True)
        self.factory = source_factory or default_audio_source
        self.level = -120.0
        self.device = ""
        self.error: str | None = None
        self._stop_ev = threading.Event()

    def stop(self):
        self._stop_ev.set()

    def run(self):
        com = False
        try:
            import warnings
            warnings.filterwarnings("ignore", message=".*discontinuity.*")
            if self.factory is default_audio_source:
                _, com = load_soundcard()
            import numpy as np
            source = self.factory("mic")
            self.device = str(getattr(source, "name", "") or "")
            block = AUDIO_RATE // 25  # 40 ms
            with source.recorder(samplerate=AUDIO_RATE, channels=1, blocksize=block) as rec:
                while not self._stop_ev.is_set():
                    data = np.asarray(rec.record(numframes=block), dtype="float32")
                    rms = float(np.sqrt(np.mean(np.square(data)))) if data.size else 0.0
                    self.level = float(20 * np.log10(max(rms, 1e-6)))
        except Exception as e:
            self.error = str(e) or e.__class__.__name__
        finally:
            if com:
                _com_uninit()


def build_mux_cmd(ffmpeg: str, video: str, v_start: float | None, tracks: list, out: str) -> list:
    """tracks: [(pcm_path, t0, channels)]. Lines each sound track up with the first video frame."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", video]
    chains, labels = [], []
    for i, (path, t0, ch) in enumerate(tracks, start=1):
        cmd += ["-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", str(ch), "-i", path]
        offset = (t0 - v_start) if (t0 is not None and v_start is not None) else 0.0
        if offset >= 0:
            shift = f"adelay=delays={int(round(offset * 1000))}:all=1"
        else:
            shift = f"atrim=start={-offset:.3f},asetpts=PTS-STARTPTS"
        stereo = ",pan=stereo|c0=c0|c1=c0" if ch == 1 else ""
        chains.append(f"[{i}:a]{shift}{stereo}[a{i}]")
        labels.append(f"[a{i}]")
    if tracks:
        if len(labels) > 1:
            chains.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0,apad[aout]")
        else:
            chains[-1] = chains[-1].replace(f"[a{len(tracks)}]", "") + ",apad[aout]"
        cmd += ["-filter_complex", ";".join(chains), "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    else:
        cmd += ["-map", "0:v", "-c:v", "copy"]
    return cmd + ["-movflags", "+faststart", out]


def probe_span(ffprobe: str | None, path: str):
    """(start, duration) of a recorded video piece, in system-clock seconds; None if unknown."""
    if not ffprobe:
        return None
    _rc, out, _ = run_hidden([ffprobe, "-v", "error", "-show_entries", "format=start_time,duration",
                              "-of", "default=nw=1", path], timeout=60)
    return span_from_probe(dict(line.split("=", 1) for line in out.splitlines() if "=" in line))


def span_from_probe(vals: dict):
    try:
        start, dur = float(vals["start_time"]), float(vals["duration"])
    except (KeyError, ValueError):
        return None
    if dur > start > 0:  # Matroska with clock timestamps reports the END time as "duration"
        dur -= start
    return start, dur


def build_mux_cmd_pieces(ffmpeg: str, videos: list, spans: list, tracks: list, out: str, workdir: str) -> list:
    """A paused recording: join the video pieces (no re-encoding) and cut every sound track to exactly the
    same clock spans, so sound and picture stay lined up after each pause."""
    listfile = os.path.join(workdir, "pieces.txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for v, sp in zip(videos, spans):
            f.write("file '" + v.replace("\\", "/").replace("'", "'\\''") + "'\n")
            if sp is not None:
                f.write(f"duration {sp[1]:.3f}\n")  # exact length, so each piece starts where the last ended
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", listfile]
    chains, labels = [], []
    known = all(sp is not None for sp in spans)
    for i, (path, t0, ch) in enumerate(tracks, start=1):
        cmd += ["-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", str(ch), "-i", path]
        stereo = ",pan=stereo|c0=c0|c1=c0" if ch == 1 else ""
        if not known or t0 is None:
            chains.append(f"[{i}:a]anull{stereo}[a{i}]")  # can't line up: keep the sound as it was recorded
        else:
            n = len(spans)
            chains.append(f"[{i}:a]asplit={n}" + "".join(f"[s{i}_{k}]" for k in range(n)))
            for k, (start, dur) in enumerate(spans):
                a, b = start - t0, start - t0 + dur
                piece = f"[s{i}_{k}]atrim=start={max(0.0, a):.3f}:end={max(0.0, b):.3f},asetpts=PTS-STARTPTS"
                if a < 0:  # the sound started a moment after this video piece: pad the front with silence
                    piece += f",adelay=delays={int(round(-a * 1000))}:all=1"
                chains.append(piece + f",apad=whole_dur={dur:.3f},atrim=end={dur:.3f}[p{i}_{k}]")
            chains.append("".join(f"[p{i}_{k}]" for k in range(n)) + f"concat=n={n}:v=0:a=1{stereo}[a{i}]")
        labels.append(f"[a{i}]")
    if tracks:
        if len(labels) > 1:
            chains.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0,apad[aout]")
        else:
            chains.append(f"{labels[0]}apad[aout]")
        cmd += ["-filter_complex", ";".join(chains), "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    else:
        cmd += ["-map", "0:v", "-c:v", "copy"]
    return cmd + ["-movflags", "+faststart", out]


class RecorderError(Exception):
    def __init__(self, message: str, details: str = ""):
        super().__init__(message)
        self.message = message
        self.details = details or message


class ScreenRecorder:
    """Start/stop a full-screen recording. No GUI code here."""

    def __init__(self, tools: dict, log=None, audio_factory=None):
        self.tools = tools
        self.log = log or (lambda m, l="info": None)
        self.audio_factory = audio_factory
        self.proc: subprocess.Popen | None = None
        self.audio: list[AudioCapture] = []
        self.started_at: float | None = None
        self.stats = ""
        self.frames = 0
        self.size = ""
        self._stderr: list[str] = []
        self.warnings: list[str] = []
        self.segments: list[str] = []   # one video piece per stretch between pauses
        self.paused = False
        self.paused_total = 0.0
        self._pause_at: float | None = None

    @property
    def elapsed(self) -> float:
        """Recorded time so far, not counting pauses."""
        if not self.started_at:
            return 0.0
        end = self._pause_at if (self.paused and self._pause_at) else time.time()
        return max(0.0, end - self.started_at - self.paused_total)

    def start(self, folder: str, pipeline: dict, fps: int = 30, high: bool = False, mouse: bool = True,
              system_audio: bool = True, mic: bool = False):
        ffmpeg = self.tools.get("ffmpeg")
        if not ffmpeg:
            raise RecorderError("FFmpeg is missing. Click 'Install automatically' at the top.")
        os.makedirs(folder, exist_ok=True)
        self.folder = folder
        self.tmp = os.path.join(folder, f".vd-temp-rec-{uuid.uuid4().hex[:8]}")
        os.makedirs(self.tmp, exist_ok=True)
        hide_folder(self.tmp)
        self.video = os.path.join(self.tmp, "video.mkv")
        self.audio = []
        if system_audio:
            self.audio.append(AudioCapture("system", os.path.join(self.tmp, "system.pcm"), 2, self.audio_factory))
        if mic:
            self.audio.append(AudioCapture("mic", os.path.join(self.tmp, "mic.pcm"), 1, self.audio_factory))
        for a in self.audio:
            a.start()
        self.warnings = []
        for a in self.audio:
            a.started_ok.wait(5)
            if a.error:
                what = "computer sound" if a.kind == "system" else "the microphone"
                self.warnings.append(f"Saved without {what}: {a.error}")
                self.log(f"Couldn't record {what}: {a.error}. Continuing without it.", "warn")
        self.audio = [a for a in self.audio if not a.error]
        for a in self.audio:
            if a.device:
                self.log(f"Recording {'computer sound' if a.kind == 'system' else 'the microphone'} from: {a.device}")

        self.segments = [self.video]
        self.paused, self.paused_total, self._pause_at = False, 0.0, None
        self._cap = (pipeline, fps, high, mouse)
        err = self._launch(self.video)
        if err:
            self._stop_audio()
            remove_tree(self.tmp)
            raise err
        self.started_at = time.time()

    def _launch(self, path: str):
        """Start FFmpeg capturing the screen into `path`. Returns None when it runs, else a RecorderError
        (and nothing is left running)."""
        pipeline, fps, high, mouse = self._cap
        cmd = build_capture_cmd(self.tools["ffmpeg"], pipeline["capture"], pipeline["encoder"], fps, high, mouse,
                                ["-f", "matroska", "-y", path])
        self._stderr, self.frames, self.stats, self.size = [], 0, "", ""
        self._capturing = False
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, **_popen_kwargs())
        except OSError as e:
            self.proc = None
            return RecorderError(f"Couldn't start FFmpeg: {e}")
        threading.Thread(target=self._read_stderr, daemon=True).start()
        deadline = time.time() + 8
        while time.time() < deadline:
            if self.proc.poll() is not None:
                break
            if self.frames > 0 or self._capturing:
                break
            time.sleep(0.05)
        settle = time.time() + 1.0   # capture opened: make sure it keeps running (some drivers fail on frame 1)
        while self.proc.poll() is None and self.frames == 0 and time.time() < settle:
            time.sleep(0.05)
        if self.proc.poll() is not None or not (self.frames > 0 or self._capturing):
            kill_tree(self.proc)
            self.proc = None
            return RecorderError("Screen capture didn't start on this PC. Click 'Show details' for FFmpeg's message.",
                                 "$ " + " ".join(cmd) + "\n\n" + "\n".join(self._stderr))
        return None

    @staticmethod
    def _finish_capture(proc):
        """Ask FFmpeg to finish the file properly (like pressing q), then wait for it."""
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                kill_tree(proc)

    def pause(self):
        """Stop the screen capture for now. The sound keeps going; the paused part is cut out when saving."""
        if self.paused or not self.proc:
            return
        self._pause_at = time.time()
        proc, self.proc = self.proc, None
        self._finish_capture(proc)
        self.paused = True

    def resume(self):
        """Capture again into a new piece. Raises RecorderError if the screen capture won't restart."""
        if not self.paused:
            return
        piece = os.path.join(self.tmp, f"video_{len(self.segments) + 1}.mkv")
        err = self._launch(piece)
        if err:
            raise err
        self.segments.append(piece)
        self.paused_total += time.time() - (self._pause_at or time.time())
        self._pause_at = None
        self.paused = False

    def _read_stderr(self):
        buf = b""
        proc = self.proc
        while proc and proc.stderr:
            chunk = proc.stderr.read(256)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = decode(raw).strip()
                if not line:
                    continue
                if proc is not self.proc:
                    return  # an earlier piece (before a pause) finishing up: don't mix up the counters
                m = re.search(r"frame=\s*(\d+)", line)
                if m:
                    self.frames = int(m.group(1))
                    self.stats = line
                else:
                    if line.startswith("Press [q]"):   # inputs open, capture running
                        self._capturing = True
                    self._stderr.append(line)
                    self._stderr = self._stderr[-40:]

    def _stop_audio(self):
        for a in self.audio:
            a.stop()
        for a in self.audio:
            a.join(5)

    def stop(self, out_path: str) -> Result:
        """Stop, line up sound + picture, save as MP4. Returns the Result."""
        proc, self.proc = self.proc, None
        self._finish_capture(proc)
        self._stop_audio()
        if not os.path.isfile(self.video) or os.path.getsize(self.video) < 1024:
            remove_tree(self.tmp)
            raise RecorderError("The recording is empty. Click 'Show details' for FFmpeg's message.",
                                "\n".join(self._stderr))
        v_start = None
        ffprobe = self.tools.get("ffprobe")
        if ffprobe:
            rc, outp, _ = run_hidden([ffprobe, "-v", "error", "-show_entries", "format=start_time",
                                      "-of", "default=nw=1:nk=1", self.video], timeout=60)
            try:
                v_start = float(outp.strip())
            except ValueError:
                v_start = None
        tracks = [(a.path, a.t0, a.channels) for a in self.audio
                  if a.t0 is not None and os.path.isfile(a.path) and os.path.getsize(a.path) > 0]
        for a in self.audio:
            if a.error and not any(w.endswith(a.error) for w in self.warnings):
                what = "computer sound" if a.kind == "system" else "the microphone"
                self.warnings.append(f"{what.capitalize()} stopped during the recording: {a.error}")
        for a in self.audio:
            if a.kind == "mic" and not a.error and a.frames > AUDIO_RATE and a.peak < MIC_SILENCE_PEAK:
                self.warnings.append("The microphone recorded only silence (the terminal says what to check).")
                self.log("The microphone recorded only silence. Check: the mic mute key (F8 on many HP laptops); "
                         "Settings > System > Sound > Input (pick your mic, volume up; the test bar should move); "
                         "Settings > Privacy & security > Microphone > 'Let desktop apps access your microphone'. "
                         f"Mic used: {a.device or 'Windows default'}.", "warn")
        pieces = [p for p in self.segments if os.path.isfile(p) and os.path.getsize(p) >= 1024]
        if len(pieces) > 1:  # paused at least once: join the pieces and cut the paused time out of the sound
            spans = [probe_span(ffprobe, p) for p in pieces]
            if any(sp is None for sp in spans):
                self.warnings.append("Sound may be out of step after a pause (FFprobe is missing).")
            mux = build_mux_cmd_pieces(self.tools["ffmpeg"], pieces, spans, tracks, out_path, self.tmp)
        else:
            mux = build_mux_cmd(self.tools["ffmpeg"], self.video, v_start, tracks, out_path)
        rc, _, err = run_hidden(mux, timeout=7200)
        if rc != 0 or not os.path.isfile(out_path):
            keep = os.path.splitext(out_path)[0] + ".mkv"
            try:
                shutil.move(self.video, keep)
            except OSError:
                keep = self.video
            raise RecorderError(f"Couldn't finish the MP4. The raw recording was kept: {keep}", err)
        remove_tree(self.tmp)
        ok, reason, details = verify_media_file(out_path, ffprobe)
        if not ok:
            raise RecorderError(f"The saved recording looks broken: {reason}.")
        return Result(path=out_path, size=os.path.getsize(out_path), width=details.get("width"),
                      height=details.get("height"), duration=details.get("duration"), defender="own")

    def abort(self):
        kill_tree(self.proc)
        self._stop_audio()
        if getattr(self, "tmp", None):
            remove_tree(self.tmp)


# =============================================================================
# Self-test (python VTeamScreenRecorder.py --selftest)
# =============================================================================

def selftest() -> int:
    failures = []
    try:
        sys.stdout.reconfigure(errors="replace")  # never crash on unusual characters in the console
    except Exception:
        pass

    def check(name, cond):
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if not cond:
            failures.append(name)

    print(f"{APP_NAME} {APP_VERSION} self-test\n")
    print("Screen recorder:")
    cmd = build_mux_cmd("ffmpeg", "v.mkv", 100.0, [("s.pcm", 100.25, 2), ("m.pcm", 99.9, 1)], "out.mp4")
    fc = cmd[cmd.index("-filter_complex") + 1]
    check("sound placed 250 ms after first frame", "adelay=delays=250:all=1" in fc)
    check("early sound trimmed by 100 ms", "atrim=start=0.100" in fc)
    check("computer sound + mic mixed", "amix=inputs=2" in fc and "pan=stereo" in fc)
    check("video copied, not re-encoded", cmd[cmd.index("-c:v") + 1] == "copy")
    check("GPU encoders tried before CPU", ENCODERS[-1] == "libx264" and len(ENCODERS) == 4)
    rec_cmd = build_capture_cmd("ffmpeg", "gdigrab", "libx264", 30, False, True, ["-f", "matroska", "out.mkv"])
    before_input = rec_cmd[:rec_cmd.index("-i")]
    check("no output-only options on the screen input (FFmpeg 7+)",
          not any(o in before_input for o in ("-thread_queue_size", "-shortest", "-vf", "-c:v")))
    probe_cmd = build_capture_cmd("ffmpeg", "gdigrab", "libx264", 30, False, True, ["-t", "1", "-f", "null", "-"])
    check("one-time check runs the same command as a real recording",
          probe_cmd[:probe_cmd.index("-copyts") + 1] == rec_cmd[:rec_cmd.index("-copyts") + 1])
    print("\nNames:")
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "Screen Recording.mp4"), "w").close()
        open(os.path.join(d, "Screen Recording (2).mp4"), "w").close()
        check("never overwrites: picks 'Screen Recording (3)'",
              unique_base(d, "Screen Recording", "mp4") == "Screen Recording (3)")
        fake = os.path.join(d, "movie.mp4")
        with open(fake, "wb") as fh:
            fh.write(b"MZ\x90\x00" + b"\x00" * 200)
        check("a program disguised as .mp4 is not accepted as a recording", not verify_media_file(fake, None)[0])
    check("sanitizes Windows-illegal names", sanitize_filename('a<b>:c"d/e\\f|g?h*') == "a b c d e f g h")
    check("default folder is Videos\\Screen Recordings", default_save_dir().endswith("Screen Recordings"))
    check("only FFmpeg is needed", list(TOOLS) == ["ffmpeg"])
    print("\nTools:")
    tools = detect_tools()
    path = tools.get("ffmpeg")
    if path:
        print(f"  OK    FFmpeg {tool_version('ffmpeg', path)}  ({path})")
    else:
        print(f"  MISS  FFmpeg not found. Install: {winget_command('ffmpeg')}")
    try:
        import soundcard as _sc  # console self-test only; the app imports it inside the audio thread
        print(f"  OK    PC-sound recording: {_sc.default_speaker().name}")
    except Exception as e:
        print(f"  info  PC-sound recording not available here ({str(e)[:60]})")
    print("\nRecorder bar and camera:")
    check("timer shows 00:01:05", fmt_clock(65) == "00:01:05")
    check("timer shows hours", fmt_clock(3725) == "01:02:05")
    new_list = ('[dshow @ 0000020a] "HP TrueVision HD Camera" (video)\n'
                '[dshow @ 0000020a]   Alternative name "@device_pnp_usb#vid_04f2"\n'
                '[dshow @ 0000020a] "Microphone Array (Realtek)" (audio)\n')
    check("finds the webcam in FFmpeg's device list", parse_dshow_cameras(new_list) == ["HP TrueVision HD Camera"])
    old_list = ('[dshow @ 01] DirectShow video devices\n[dshow @ 01]  "Integrated Webcam"\n'
                '[dshow @ 01]     Alternative name "@device_pnp_x"\n[dshow @ 01] DirectShow audio devices\n'
                '[dshow @ 01]  "Microphone"\n')
    check("finds the webcam in older FFmpeg's list", parse_dshow_cameras(old_list) == ["Integrated Webcam"])
    check("no camera -> empty list", parse_dshow_cameras("Error opening input") == [])
    check("camera busy -> helpful message", "another app" in camera_error_text("Could not run graph"))
    check("camera blocked -> privacy setting hint", "Privacy" in camera_error_text("Access is denied"))
    ff = find_tool("ffmpeg")
    if ff:
        feed = CameraFeed(ff, ["-re", "-f", "lavfi", "-i", "testsrc=size=640x480:rate=30"], 96)
        feed.start()
        t0 = time.time()
        while feed.frames < 3 and time.time() - t0 < 10:
            time.sleep(0.05)
        got_frames = feed.frames >= 3 and len(feed.frame or b"") == 96 * 96 * 3
        feed.stop()
        check("camera frames come through FFmpeg (test picture)", got_frames)
        check("camera process is closed afterwards", not feed.running)
    else:
        print("  info  FFmpeg not found here; camera pipe test skipped")
    try:
        import onnxruntime  # noqa: F401
        model = resource_path(SEG_MODEL_FILE)
        if os.path.exists(model):
            seg = Segmenter(model, (254, 0, 254))
            blank = bytes([128]) * (96 * 96 * 3)
            t0 = time.time()
            out = seg.cutout(blank, 96)
            ms = (time.time() - t0) * 1000
            keyed = sum(1 for i in range(0, len(out), 3) if out[i:i + 3] == bytes([254, 0, 254]))
            check("background removal: an empty grey picture becomes see-through", keyed > 0.9 * 96 * 96)
            check("background removal is fast enough (first frame under 1 s)", ms < 1000)
        else:
            print(f"  info  {SEG_MODEL_FILE} not found next to the app; background removal test skipped")
    except ImportError:
        print("  info  onnxruntime not installed here; background removal test skipped")
    try:
        from PIL import Image as _PI
        canvas = _PI.new("L", (256, 256), 0)
        canvas.paste(255, (60, 60, 180, 180))
        canvas.paste(0, (100, 100, 102, 102))     # pinhole inside the person
        canvas.paste(255, (220, 30, 222, 32))     # speck in the background
        cm = clean_mask(canvas)
        check("cutout: a pinhole inside you is filled", cm.getpixel((101, 101)) == 255)
        check("cutout: a speck outside you is removed", cm.getpixel((221, 31)) == 0)
        check("cutout: your outline stays where it was", cm.getpixel((61, 61)) == 255 and cm.getpixel((57, 57)) == 0)
    except ImportError:
        print("  info  Pillow not installed here; cutout clean-up test skipped")
    check("mic icon: silence is grey", mic_level_colour(-80) == MIC_SILENT)
    g = mic_level_colour(-25)
    check("mic icon: voice is green", int(g[3:5], 16) > int(g[1:3], 16) + 80)
    check("mic icon: very loud is red", mic_level_colour(-5) == "#FF4D4F")
    try:
        import numpy as _np

        class _FakeMic:  # behaves like a soundcard microphone, playing a 440 Hz tone
            name = "Test microphone"

            def __init__(self, amp):
                self.amp = amp

            def recorder(self, samplerate, channels, blocksize=None):
                amp = self.amp

                class _Rec:
                    def __enter__(self):
                        self.t = 0
                        return self

                    def __exit__(self, *a):
                        return False

                    def record(self, numframes):
                        time.sleep(numframes / samplerate)
                        t = (_np.arange(numframes) + self.t) / samplerate
                        self.t += numframes
                        return (amp * _np.sin(2 * _np.pi * 440 * t)).reshape(-1, 1).repeat(channels, 1)
                return _Rec()
        meter = MicMeter(lambda kind: _FakeMic(0.1))
        meter.start()
        time.sleep(0.5)
        meter.stop()
        meter.join(2)
        check("mic meter hears a test tone (about -23 dB)", -26 < meter.level < -20 and meter.device == "Test microphone")
        with tempfile.TemporaryDirectory() as d:
            quiet = AudioCapture("mic", os.path.join(d, "q.pcm"), 1, lambda kind: _FakeMic(0.0))
            loud = AudioCapture("mic", os.path.join(d, "l.pcm"), 1, lambda kind: _FakeMic(0.2))
            for c in (quiet, loud):
                c.start()
            time.sleep(0.5)
            for c in (quiet, loud):
                c.stop()
                c.join(2)
            check("a silent microphone is noticed", quiet.frames > 0 and quiet.peak < MIC_SILENCE_PEAK)
            check("a working microphone's level is measured", loud.peak > 0.15 and loud.level > -20)
    except ImportError:
        print("  info  numpy not installed here; microphone level tests skipped")
    print("\nPause:")
    with tempfile.TemporaryDirectory() as d:
        cmd = build_mux_cmd_pieces("ffmpeg", [os.path.join(d, "a.mkv"), os.path.join(d, "b.mkv")],
                                   [(100.0, 2.0), (105.0, 3.0)], [(os.path.join(d, "mic.pcm"), 99.5, 1)],
                                   os.path.join(d, "out.mp4"), d)
        fc = cmd[cmd.index("-filter_complex") + 1]
        listed = open(os.path.join(d, "pieces.txt"), encoding="utf-8").read()
    check("pause: both video pieces are joined", listed.count("file '") == 2 and "concat" in cmd)
    check("pause: sound is cut to each piece (0.5-2.5 s and 5.5-8.5 s)",
          "atrim=start=0.500:end=2.500" in fc and "atrim=start=5.500:end=8.500" in fc)
    check("pause: each sound piece is exactly as long as its video piece",
          "apad=whole_dur=2.000,atrim=end=2.000" in fc and "apad=whole_dur=3.000,atrim=end=3.000" in fc)
    check("pause: piece length is end minus start when FFmpeg reports clock times",
          span_from_probe({"start_time": "1795656639.567", "duration": "1795656642.933"}) == (1795656639.567,
                                                                                                 3.366 + 0.0)
          or abs(span_from_probe({"start_time": "1795656639.567", "duration": "1795656642.933"})[1] - 3.366) < 1e-6)
    check("pause: a normal length stays as it is",
          span_from_probe({"start_time": "0.000", "duration": "3.500"}) == (0.0, 3.5))
    r = ScreenRecorder({})
    r.started_at, r.paused, r.paused_total, r._pause_at = time.time() - 10, True, 3.0, time.time() - 2
    check("pause: the timer leaves paused time out", 4.5 < r.elapsed < 5.5)
    print("\nSupport form:")
    sid = make_support_id()
    check("support ID looks like 7F3K-92QD", is_support_id(sid))
    check("support ID never uses 0, O, 1 or I", not any(c in sid for c in "0O1I"))
    check("two support IDs differ", make_support_id() != make_support_id())
    check("email check accepts name@site.com", bool(EMAIL_RE.match("name@site.com")))
    check("email check rejects 'hello'", not EMAIL_RE.match("hello"))
    p = build_support_payload("Other", " name@site.com ", " it broke ", sid, "diag", b"\xff\xd8jpeg")
    check("payload trims fields and carries the app key",
          p["email"] == "name@site.com" and p["message"] == "it broke" and p["key"] == SUPPORT_APP_KEY)
    check("payload carries the screenshot", base64.b64decode(p["image_b64"]) == b"\xff\xd8jpeg")
    try:
        import io as _io
        from PIL import Image as _Img
        b = _io.BytesIO()
        _Img.new("RGB", (4000, 1000), "white").save(b, "PNG")
        with _Img.open(_io.BytesIO(normalize_screenshot(b.getvalue()))) as im2:
            check("screenshot becomes a JPEG, max 2560 px", im2.format == "JPEG" and max(im2.size) <= 2560)
        try:
            normalize_screenshot(b"MZ\x90\x00 this is a program, not a picture")
            check("a non-picture file is refused", False)
        except Exception:
            check("a non-picture file is refused", True)
    except ImportError:
        print("  info  Pillow not installed here; screenshot test skipped")
    try:
        import http.server
        got = {}

        class _FakeRelay(http.server.BaseHTTPRequestHandler):  # answers like Google Apps Script: 302, then JSON
            def log_message(self, *a):
                pass

            def do_POST(self):
                got["p"] = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self.send_response(302)
                self.send_header("Location", "/reply")
                self.end_headers()

            def do_GET(self):
                body = json.dumps({"ok": got.get("p", {}).get("key") == SUPPORT_APP_KEY, "error": ""}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        for var in ("no_proxy", "NO_PROXY"):  # the fake relay is on this PC: never route it through a proxy
            os.environ[var] = ",".join(x for x in (os.environ.get(var), "127.0.0.1", "localhost") if x)
        srv = http.server.HTTPServer(("127.0.0.1", 0), _FakeRelay)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        ok, _msg, _detail = send_support(p, url=f"http://127.0.0.1:{srv.server_port}/exec", timeout=10)
        srv.shutdown()
        check("sends to the relay and follows its redirect", ok and got.get("p", {}).get("support_id") == sid)
        ok2, msg2, _ = send_support(p, url="http://127.0.0.1:9/exec", timeout=3)
        check("no server -> clear 'couldn't reach' message", not ok2 and "Couldn't reach" in msg2)
    except Exception as e:
        check(f"local relay test ran ({type(e).__name__}: {e})", False)
    print("\nDonate (UPI):")
    check("UPI ID check accepts name@okaxis", is_upi_id("shiva.k@okaxis") and is_upi_id("vd-app@ybl"))
    check("UPI ID check rejects junk", not any(is_upi_id(v) for v in ("", "name", "@okaxis", "a b@ybl", None,
                                                                         "name@", "x@1bank")))
    check("amounts stay between ₹20 and ₹300", donate_amount(5) == 20 and donate_amount(1000) == 300
          and donate_amount("100") == 100 and donate_amount("abc") == DONATE_DEFAULT)
    link = upi_link("shiva.k@okaxis", 100)
    check("UPI link has the ID, ₹100 and INR",
          link.startswith("upi://pay?pa=shiva.k@okaxis&") and "&am=100.00&" in link and "&cu=INR" in link)
    check("UPI link note and name are URL-safe", " " not in link and "pn=VTeam%20Screen%20Recorder" in link)
    check("UPI payment note says 'VTeam Screen Recorder voluntary donation'",
          urllib.parse.unquote(link.split("&tn=")[1]) == "VTeam Screen Recorder voluntary donation")
    check("consent text is the agreed wording", DONATE_CONSENT.startswith("I'm giving this voluntarily, from my own "
          "bank account") and DONATE_CONSENT.endswith("it's not refundable"))
    try:
        upi_link("not an id", 50)
        check("a bad UPI ID is refused", False)
    except ValueError:
        check("a bad UPI ID is refused", True)
    try:
        png = upi_qr_png(link)
        check("QR code is a PNG picture", png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 300)
    except ImportError:
        print("  info  segno not installed here; QR picture test skipped (the Donate dialog shows the UPI ID)")
    check("DONATE_UPI_ID is a real UPI ID", is_upi_id(DONATE_UPI_ID))
    print("\nUpdate check:")
    check("reads 'v2.1.0' and '2.1'", parse_version("v2.1.0") == (2, 1, 0) and parse_version("2.1") == (2, 1, 0))
    check("ignores junk and pre-release tags", parse_version("latest") is None and parse_version("v2.1.0-beta") is None)
    check("v99.0.0 counts as newer", is_newer("v99.0.0"))
    check("this version or an older one doesn't", not is_newer(APP_VERSION) and not is_newer("v0.1.0"))
    try:
        import http.server
        replies = []

        class _FakeGitHub(http.server.BaseHTTPRequestHandler):  # answers like api.github.com/.../releases/latest
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps(replies[0]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        for var in ("no_proxy", "NO_PROXY"):  # the fake server is on this PC: never route it through a proxy
            os.environ[var] = ",".join(x for x in (os.environ.get(var), "127.0.0.1", "localhost") if x)
        srv = http.server.HTTPServer(("127.0.0.1", 0), _FakeGitHub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_port}/latest"
        own = f"https://github.com/{UPDATE_REPO}/releases/tag/v9.9.9"
        replies[:] = [{"tag_name": "v9.9.9", "html_url": own}]
        check("finds the newest release (local test server)", fetch_latest_release(url, 5) == ("9.9.9", own))
        replies[:] = [{"tag_name": "v9.9.9", "html_url": "https://evil.example/x"}]
        check("only ever opens this app's own releases page", fetch_latest_release(url, 5) == ("9.9.9", UPDATE_PAGE))
        replies[:] = [{"tag_name": "v9.9.9", "prerelease": True, "html_url": own}]
        check("pre-releases are ignored", fetch_latest_release(url, 5) is None)
        srv.shutdown()
        check("no internet -> no update and no error", fetch_latest_release("http://127.0.0.1:9/x", 3) is None)
    except Exception as e:
        check(f"local update test ran ({type(e).__name__}: {e})", False)
    print("\n" + ("ALL TESTS PASSED" if not failures else f"{len(failures)} TEST(S) FAILED: {', '.join(failures)}"))
    return 0 if not failures else 1


# =============================================================================
# GUI
# =============================================================================

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import filedialog, messagebox
except ImportError:  # self-test still works without the GUI packages
    ctk = None

try:
    from PIL import Image
except ImportError:
    Image = None
try:  # PyInstaller must bundle these or the .exe crashes when showing pictures
    from PIL import ImageTk  # noqa: F401
    import PIL._tkinter_finder  # noqa: F401
except ImportError:
    pass

# White theme palette
BG = "#FFFFFF"
SURFACE = "#F6F8FA"
SURFACE_2 = "#EEF1F4"
BORDER = "#D0D7DE"
TEXT = "#1F2328"
MUTED = "#57606A"
FAINT = "#8C959F"
GREEN = "#1A7F37"
GREEN_HOVER = "#116329"
RED = "#CF222E"
RED_HOVER = "#A40E26"
AMBER_BG = "#FFF8C5"
AMBER_TEXT = "#7D4E00"
AMBER_BORDER = "#D4A72C"
BLUE = "#0969DA"
SLATE = "#2D3B45"
SLATE_HOVER = "#1F2A33"
BAR_BG = "#1F2328"
BAR_HOVER = "#30363D"
BAR_DIM = "#6E7681"
BAR_TEXT = "#E6EDF3"
BAR_ON = "#3FB950"
PAUSE_AMBER = "#D29922"
# recorder bar icons: Windows' own icon font (as in the Snipping Tool), emoji where that font is missing
BAR_ICONS_FLUENT = {"mic": "\uE720", "mic_off": "\uEC54", "system": "\uE767", "system_off": "\uE74F",
                    "camera": "\uE722", "camera_off": "\uE722", "caret": "\uE70D", "gear": "\uE713",
                    "close": "\uE711", "pause": "\uE769", "resume": "\uE768"}
BAR_ICONS_EMOJI = {"mic": "🎙", "mic_off": "🎙", "system": "🔊", "system_off": "🔊", "camera": "📷",
                   "camera_off": "📷", "caret": "▾", "gear": "⚙", "close": "✕", "pause": "⏸", "resume": "▶"}
LOG_COLORS = {"info": MUTED, "ok": GREEN, "warn": "#9A6700", "err": RED, "safe": BLUE}
LOG_TAGS = {"info": "INFO", "ok": " OK ", "warn": "WARN", "err": "FAIL", "safe": "SAFE"}
THUMB_W, THUMB_H = 300, 169

# The "How to use" guide shown when the app opens. ("h" = heading, "p" = paragraph)

# The record button on the home window
REC_BTN_IDLE = {"text": "⏺  Record screen", "fg_color": RED, "hover_color": RED_HOVER, "text_color": "#FFFFFF",
                "border_color": RED}
REC_BTN_ACTIVE = {"text": "■  Stop recording", "fg_color": BAR_BG, "hover_color": BAR_HOVER, "text_color": "#FFFFFF",
                  "border_color": BAR_BG}

GUIDE = [
    ("Quick start", [
        ("h", "Record your screen in 4 steps"),
        ("p", "1.  SAVE TO is the folder for your recordings (Videos\\Screen Recordings at first). Click Browse to "
              "change it; the app remembers it."),
        ("p", "2.  Click ⏺ Record screen. A small dark bar appears at the top of your screen, like the Windows "
              "Snipping Tool."),
        ("p", "3.  Switch on what you want on the bar: microphone, PC sound (everything you hear) and camera. "
              "Lit = on. Hover an icon to see its name."),
        ("p", "4.  Click ● Start. After a 3-second countdown the timer runs. The pause icon next to Stop pauses "
              "(that part is left out of the video). Click ■ Stop and you get one MP4, with ▶ Play video and "
              "Show in folder."),
        ("h", "Good to know"),
        ("p", "The bar and this window never appear in the recording (Windows 10 version 2004 or newer). "
              "The camera bubble does, so people can see you."),
        ("p", "⚙ on the bar: 30 or 60 fps, Standard or High quality, show the mouse pointer, and minimise this "
              "window while recording."),
        ("p", "The app records the main screen. It uses your graphics card to encode when it can (NVIDIA, Intel "
              "or AMD), so it stays smooth."),
        ("p", "Netflix, Prime Video and other DRM players show black in any recording; that's Windows' copy "
              "protection. Always tell people before you record a call or meeting."),
    ]),
    ("Camera & mic", [
        ("h", "Camera"),
        ("p", "Click the camera icon on the bar to show your webcam. ▾ next to it picks the style: Bubble (round) "
              "or Cutout (no background, like streamers), the size (Small to Extra large) and which camera."),
        ("p", "Drag the camera on screen to move it; the app remembers where. Cutout works best with good light "
              "and a plain wall behind you."),
        ("p", "Camera won't start? Close other apps that use it (Zoom, Teams, the Camera app) and check "
              "Settings > Privacy & security > Camera > 'Let desktop apps access your camera'."),
        ("h", "Microphone"),
        ("p", "The mic icon shows your voice live: grey when it's quiet, green when you speak, red after a few "
              "seconds of non-stop talking."),
        ("p", "If it stays grey while you talk: check the mic mute key on your keyboard (F8 on many HP laptops), "
              "Settings > System > Sound > Input, and Settings > Privacy & security > Microphone."),
        ("h", "PC sound"),
        ("p", "Records everything you hear from your default speakers or headphones (videos, calls, games). "
              "Sound and picture are lined up when the video is saved, also after a pause."),
    ]),
    ("Help", [
        ("h", "If something goes wrong"),
        ("p", "'FFmpeg is missing': click Install automatically in the yellow bar (about 1 minute)."),
        ("p", "The video looks choppy: choose 30 fps and Standard quality under ⚙."),
        ("p", "No PC sound: make sure your speakers or headphones are the default output in Windows Sound "
              "settings, and that the PC sound icon on the bar is lit."),
        ("p", "Anything else: click 💬 Support (top right). Describe the problem and attach a screenshot; your "
              "support ID is added for you. Or email vteam732@gmail.com."),
        ("h", "Tip the developer"),
        ("p", "The app is free, with every feature. If it's useful to you, ☕ Tip the developer (top right) lets "
              "you send ₹20 to ₹300 by UPI. It's optional and doesn't unlock anything."),
    ]),
]


if ctk is not None:

    class App(ctk.CTk):
        def __init__(self):
            ctk.set_appearance_mode("light")
            super().__init__()
            self.settings = Settings(SETTINGS_FILE)
            self.support_id = self._load_support_id()
            self.tool_versions: dict = {}
            self._support_win = None
            self._donate_win = None
            self._support_last = 0.0
            self.q: queue.Queue = queue.Queue()
            self.tools: dict = {}
            self.installing = False
            self.closing = False
            self.result: Result | None = None
            self.details_text = ""
            self._guide = None
            self._pending_install = None
            self.recorder: ScreenRecorder | None = None
            self._rec_bar = None
            self._rec_dialog = None
            self._rec_opts: dict = {}
            self._rec_run = 0  # each Start gets a number, so a cancelled countdown can't fire later
            self._rec_paused = False
            self._rec_pausing = False
            self._rec_menu = None
            self._cameras: list | None = None
            self._cam_device = ""
            self._cam_win = None
            self._cam_feed: CameraFeed | None = None
            self._cam_size = 240
            self._cam_key_rgb = (31, 35, 40)
            self._cam_seen = 0
            self._cam_test_input: list | None = None  # self-tests feed a test picture instead of a webcam
            self._audio_test_factory = None  # self-tests feed fake microphones
            self._rec_cam_menu = None
            self._mic_meter: MicMeter | None = None
            self._mic_ticking = False
            self._mic_colour = MIC_SILENT
            self._mic_disp = -120.0
            self._mic_voice_since = None
            self._mic_quiet_since = None
            self._mic_error_logged = False
            self._rec_state = "idle"  # idle | preparing | countdown | recording | saving
            self._close_after_rec = False

            self.title(f"{APP_NAME}" + ("  (Administrator)" if is_admin() else ""))
            self.geometry("820x660")
            self.minsize(760, 600)
            self.configure(fg_color=BG)
            try:
                if IS_WIN:
                    self.iconbitmap(resource_path("icon.ico"))
            except Exception:
                pass
            self._fonts()
            self._build()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.after(50, self._pump)
            self.after(200, self._startup)

        # ------------------------------------------------------------- setup
        def _fonts(self):
            try:
                import tkinter.font as tkfont
                fams = set(tkfont.families(self))
            except Exception:
                fams = set()
            ui = next((f for f in ("Segoe UI", "Inter", "DejaVu Sans") if f in fams), "TkDefaultFont")
            mono = next((f for f in ("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New") if f in fams), "TkFixedFont")
            self.f_title = ctk.CTkFont(mono, 24, "bold")
            self.f_sub = ctk.CTkFont(mono, 13)
            self.f_label = ctk.CTkFont(mono, 12, "bold")
            self.f_ui = ctk.CTkFont(ui, 15)
            self.f_ui_b = ctk.CTkFont(ui, 15, "bold")
            self.f_link = ctk.CTkFont(ui, 16)
            self.f_small = ctk.CTkFont(ui, 13)
            self.f_btn = ctk.CTkFont(ui, 18, "bold")
            self.f_mono = ctk.CTkFont(mono, 13)
            self.f_chip = ctk.CTkFont(mono, 12)
            self.f_small_b = ctk.CTkFont(ui, 13, "bold")
            self.f_h2 = ctk.CTkFont(ui, 20, "bold")
            self.f_timer = ctk.CTkFont(mono, 17)
            self.f_tog_off = ctk.CTkFont(ui, 13, overstrike=True)
            iconfam = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)
            self.bar_icons = BAR_ICONS_FLUENT if iconfam else BAR_ICONS_EMOJI
            self.f_icon = ctk.CTkFont(iconfam, 15) if iconfam else ctk.CTkFont(ui, 15)
            self.f_icon_off = self.f_icon if iconfam else ctk.CTkFont(ui, 15, overstrike=True)  # own "off" glyphs
            self.f_icon_small = ctk.CTkFont(iconfam, 10) if iconfam else self.f_small
            self.f_timer_s = ctk.CTkFont(mono, 14)

        def _label(self, parent, text, hint=""):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            ctk.CTkLabel(row, text=text, font=self.f_label, text_color=MUTED).pack(side="left")
            if hint:
                ctk.CTkLabel(row, text="  " + hint, font=self.f_small, text_color=FAINT).pack(side="left")
            return row

        def _entry(self, parent, var, height=42, font=None):
            # (customtkinter hides placeholder text when a variable is attached, so hints live in the labels)
            return ctk.CTkEntry(parent, textvariable=var, height=height, font=font or self.f_ui, fg_color=BG,
                                border_color=BORDER, border_width=1, text_color=TEXT, corner_radius=6)

        def _button(self, parent, text, command, width=110, height=42, primary=False, danger=False, font=None):
            if primary or danger:
                return ctk.CTkButton(parent, text=text, command=command, width=width, height=height,
                                     font=font or self.f_ui_b, corner_radius=6, text_color="#FFFFFF",
                                     fg_color=RED if danger else GREEN, hover_color=RED_HOVER if danger else GREEN_HOVER,
                                     text_color_disabled="#E6E6E6")
            return ctk.CTkButton(parent, text=text, command=command, width=width, height=height,
                                 font=font or self.f_ui, corner_radius=6, fg_color=SURFACE, hover_color=SURFACE_2,
                                 text_color=TEXT, border_width=1, border_color=BORDER, text_color_disabled=FAINT)

        def _option(self, parent, var, values, width):
            return ctk.CTkOptionMenu(parent, variable=var, values=values, width=width, height=42, font=self.f_ui,
                                     dropdown_font=self.f_ui, corner_radius=6, fg_color=SURFACE,
                                     button_color=SURFACE_2, button_hover_color=BORDER, text_color=TEXT,
                                     dropdown_fg_color=BG, dropdown_hover_color=SURFACE_2, dropdown_text_color=TEXT,
                                     dynamic_resizing=False)

        def _build(self):
            pad = 24
            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(6, weight=1)

            # Row 0: header
            hdr = ctk.CTkFrame(self, fg_color="transparent")
            hdr.grid(row=0, column=0, sticky="ew", padx=pad, pady=(18, 4))
            ctk.CTkLabel(hdr, text="⏺ VTeam Screen Recorder", font=self.f_title, text_color=RED).pack(side="left")
            ctk.CTkLabel(hdr, text=f"   v{APP_VERSION}", font=self.f_sub,
                         text_color=FAINT).pack(side="left", pady=(6, 0))
            self._github_button(hdr).pack(side="right")
            self._button(hdr, "❔ How to use", lambda: self._show_guide(), width=124, height=34,
                         font=self.f_small).pack(side="right", padx=(0, 8))

            # Row 1: FFmpeg status, support ID, Tip + Support
            bar = ctk.CTkFrame(self, fg_color="transparent")
            bar.grid(row=1, column=0, sticky="ew", padx=pad, pady=(2, 8))
            self.btn_tools = ctk.CTkButton(bar, text="FFMPEG  ○ checking…  ▾", command=self._toggle_tools_menu,
                                           width=190, height=28, font=self.f_label, corner_radius=6,
                                           fg_color=SURFACE, hover_color=SURFACE_2, text_color=FAINT,
                                           border_width=1, border_color=BORDER)
            self.btn_tools.pack(side="left")
            # this PC's support ID; click to copy (users quote it when they contact Support)
            self.lbl_sid = ctk.CTkLabel(bar, text=self._sid_text(), font=self.f_chip, text_color=MUTED)
            self.lbl_sid.pack(side="left", padx=(14, 0))
            self.lbl_sid.bind("<Button-1>", lambda e: self._copy_support_id())
            # "New version" pill: hidden until the once-a-day update check finds a newer release on GitHub
            self.btn_newver = ctk.CTkButton(bar, text="", width=10, height=28, font=self.f_small_b, corner_radius=14,
                                            fg_color="#DAFBE1", hover_color="#BFE8C9", text_color=GREEN,
                                            border_width=1, border_color="#A7E3B7",
                                            command=lambda: webbrowser.open(safe_release_page(self._app_update_page)))
            self._app_update_page = UPDATE_PAGE
            self._app_update_logged = False
            self.btn_support = self._button(bar, "💬 Support", self._show_support, width=112, height=28,
                                            font=self.f_small)
            self.btn_support.pack(side="right")
            self.btn_donate = ctk.CTkButton(bar, text="☕ Tip the developer", command=self._show_donate, width=164,
                                            height=28, font=self.f_small_b, corner_radius=6, fg_color=AMBER_BG,
                                            hover_color="#FBE7A1", text_color=AMBER_TEXT, border_width=1,
                                            border_color=AMBER_BORDER)
            self.btn_donate.pack(side="right", padx=(0, 8))
            ctk.CTkFrame(self, height=1, fg_color=BORDER).grid(row=2, column=0, sticky="ew", padx=pad)

            # Row 3: FFmpeg-missing banner (hidden until needed)
            self.banner = ctk.CTkFrame(self, fg_color=AMBER_BG, border_color=AMBER_BORDER, border_width=1, corner_radius=8)
            self.banner_text = ctk.CTkLabel(self.banner, text="", font=self.f_ui, text_color=AMBER_TEXT, justify="left",
                                            anchor="w")
            self.banner_text.pack(side="left", padx=14, pady=10, fill="x", expand=True)
            self.banner_cmd = ctk.CTkEntry(self.banner, width=280, height=34, font=self.f_mono, fg_color=BG,
                                           border_color=AMBER_BORDER, text_color=TEXT)
            self.btn_install = self._button(self.banner, "Install automatically", lambda: self._install(None),
                                            width=190, height=36, primary=True, font=self.f_ui_b)
            self.btn_install.pack(side="right", padx=(8, 12), pady=10)
            self.banner_cmd.pack(side="right", pady=10)

            # Row 4: save folder + the big record button
            main = ctk.CTkFrame(self, fg_color="transparent")
            main.grid(row=4, column=0, sticky="ew", padx=pad, pady=(14, 6))
            main.grid_columnconfigure(0, weight=1)
            self._label(main, "SAVE TO", "folder for your recordings").grid(row=0, column=0, sticky="w")
            r = ctk.CTkFrame(main, fg_color="transparent")
            r.grid(row=1, column=0, sticky="ew", pady=(4, 16))
            r.grid_columnconfigure(0, weight=1)
            self.save_var = tk.StringVar(value=self.settings.get("save_dir") or default_save_dir())
            self.e_save = self._entry(r, self.save_var)
            self.e_save.grid(row=0, column=0, sticky="ew")
            self.e_save.bind("<FocusOut>", lambda e: self._remember_folder())
            self._button(r, "Browse", self._browse, width=96).grid(row=0, column=1, padx=(8, 0))
            self._button(r, "Open", self._open_save_dir, width=72).grid(row=0, column=2, padx=(8, 0))
            self.btn_record = ctk.CTkButton(main, command=self._record_clicked, height=64, font=self.f_btn,
                                            corner_radius=8, border_width=1, **REC_BTN_IDLE)
            self.btn_record.grid(row=2, column=0, sticky="ew")
            ctk.CTkLabel(main, text="Opens the recording bar at the top of your screen: switch on the mic, PC sound "
                                    "and camera, then press ● Start.", font=self.f_small, text_color=MUTED,
                         wraplength=720, justify="left", anchor="w").grid(row=3, column=0, sticky="w", pady=(6, 0))

            # Row 5: status / result / error
            self.status_area = ctk.CTkFrame(self, fg_color="transparent")
            self.status_area.grid(row=5, column=0, sticky="ew", padx=pad, pady=(10, 4))
            self.status_area.grid_columnconfigure(0, weight=1)
            self.stage = ctk.CTkLabel(self.status_area, text="Ready.", font=self.f_ui_b, text_color=TEXT, anchor="w")
            self.stage.grid(row=0, column=0, sticky="ew")

            self.res_frame = ctk.CTkFrame(self.status_area, fg_color="#F0FFF4", border_color="#A7E3B7", border_width=1,
                                          corner_radius=8)
            self.res_frame.grid_columnconfigure(0, weight=1)
            self.res_title = ctk.CTkLabel(self.res_frame, text="", font=self.f_ui_b, text_color=GREEN, anchor="w")
            self.res_title.grid(row=0, column=0, sticky="ew", padx=16, pady=(10, 0))
            self.res_meta = ctk.CTkLabel(self.res_frame, text="", font=self.f_small, text_color=MUTED, anchor="w")
            self.res_meta.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 4))
            self.res_badge = ctk.CTkLabel(self.res_frame, text="", font=self.f_small, wraplength=460, justify="left",
                                          anchor="w", corner_radius=6)
            self.res_badge.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 10))
            btns = ctk.CTkFrame(self.res_frame, fg_color="transparent")
            btns.grid(row=0, column=1, rowspan=3, padx=12)
            self._button(btns, "▶  Play video", self._play, width=140, primary=True).pack(side="left", padx=4)
            self._button(btns, "Show in folder", self._show_in_folder, width=140).pack(side="left", padx=4)

            self.err_frame = ctk.CTkFrame(self.status_area, fg_color="#FFF5F5", border_color="#F5B5B9", border_width=1,
                                          corner_radius=8)
            self.err_frame.grid_columnconfigure(0, weight=1)
            self.err_title = ctk.CTkLabel(self.err_frame, text="", font=self.f_ui_b, text_color=RED, anchor="w")
            self.err_title.grid(row=0, column=0, sticky="ew", padx=16, pady=(10, 0))
            self.err_msg = ctk.CTkLabel(self.err_frame, text="", font=self.f_ui, text_color=TEXT, anchor="w",
                                        justify="left", wraplength=520)
            self.err_msg.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))
            eb = ctk.CTkFrame(self.err_frame, fg_color="transparent")
            eb.grid(row=0, column=1, rowspan=2, padx=12)
            self._button(eb, "Show details", self._show_details, width=130).pack(side="left", padx=4)
            self._button(eb, "OK", self._dismiss_error, width=70).pack(side="left", padx=4)

            # Row 6: log
            term = ctk.CTkFrame(self, fg_color="transparent")
            term.grid(row=6, column=0, sticky="nsew", padx=pad, pady=(8, 18))
            term.grid_columnconfigure(0, weight=1)
            term.grid_rowconfigure(1, weight=1)
            th = ctk.CTkFrame(term, fg_color="transparent")
            th.grid(row=0, column=0, sticky="ew", pady=(0, 4))
            ctk.CTkLabel(th, text="LOG", font=self.f_label, text_color=FAINT).pack(side="left")
            self._button(th, "Clear", self._clear_log, width=70, height=28, font=self.f_small).pack(side="right")
            self._button(th, "Show details", self._show_details, width=120, height=28,
                         font=self.f_small).pack(side="right", padx=(0, 8))
            self.term = ctk.CTkTextbox(term, font=self.f_mono, fg_color=SURFACE, border_color=BORDER, border_width=1,
                                       text_color=TEXT, corner_radius=6, wrap="word", height=110)
            self.term.grid(row=1, column=0, sticky="nsew")
            for level, color in LOG_COLORS.items():
                self.term.tag_config(level, foreground=color)
            self.term.configure(state="disabled")
            self._build_tools_menu()

        # ------------------------------------------------------------- FFmpeg dropdown
        def _build_tools_menu(self):
            """Floating panel under the TOOLS button with each tool's version. Hidden until clicked."""
            self.tools_menu = ctk.CTkFrame(self, fg_color=BG, border_color=BORDER, border_width=1, corner_radius=8)
            ctk.CTkLabel(self.tools_menu, text="INSTALLED TOOLS", font=self.f_label,
                         text_color=FAINT).pack(anchor="w", padx=14, pady=(10, 4))
            self.chips = {}
            keys = ("ffmpeg",)
            for key in keys:
                chip = ctk.CTkLabel(self.tools_menu, text=f"○ {TOOLS[key]['label']}: checking…", font=self.f_chip,
                                    text_color=FAINT, anchor="w")
                chip.pack(fill="x", padx=14, pady=1)
                self.chips[key] = chip
            ctk.CTkLabel(self.tools_menu, text="FFmpeg records the screen and saves the MP4.", font=self.f_chip,
                         text_color=MUTED, anchor="w").pack(fill="x", padx=14, pady=(6, 10))
            self.update_var = tk.BooleanVar(value=bool(self.settings.get("update_check", True)))
            ctk.CTkCheckBox(self.tools_menu, text="Check for new versions (once a day)", variable=self.update_var,
                            command=self._app_update_setting_changed, font=self.f_chip, text_color=MUTED,
                            fg_color=GREEN, hover_color=GREEN_HOVER, border_color=BORDER, checkbox_width=18,
                            checkbox_height=18).pack(anchor="w", padx=14, pady=(0, 10))
            self.tools_menu_open = False
            # close on a click anywhere else, or on Esc (bind_all so the window's own Esc binding is kept)
            self.bind_all("<Button-1>", self._tools_menu_click_outside, add="+")
            self.bind_all("<Escape>", lambda e: self._hide_tools_menu(), add="+")

        def _toggle_tools_menu(self):
            if self.tools_menu_open:
                self._hide_tools_menu()
                return
            self.tools_menu.place(in_=self.btn_tools, x=0, rely=1.0, y=6, anchor="nw")
            self.tools_menu.lift()
            self.tools_menu_open = True

        def _hide_tools_menu(self):
            if getattr(self, "tools_menu_open", False):
                self.tools_menu.place_forget()
                self.tools_menu_open = False

        def _tools_menu_click_outside(self, event):
            if not self.tools_menu_open:
                return
            path = str(event.widget)
            for owner in (self.tools_menu, self.btn_tools):
                if path == str(owner) or path.startswith(str(owner) + "."):
                    return  # click on the panel or the button itself
            self._hide_tools_menu()

        # ------------------------------------------------------------- support ID, Donate, Support
        def _load_support_id(self) -> str:
            sid = self.settings.get("support_id")
            if not is_support_id(sid):
                sid = make_support_id()
                self.settings.set("support_id", sid)
            return sid

        def _sid_text(self) -> str:
            return f"Support ID {self.support_id}"

        def _copy_support_id(self):
            self.clipboard_clear()
            self.clipboard_append(self.support_id)
            self.lbl_sid.configure(text="ID copied ✓", text_color=GREEN)
            self.after(1500, lambda: self.lbl_sid.configure(text=self._sid_text(), text_color=MUTED))

        def _focus_existing(self, attr) -> bool:
            """A second click on Tip the developer / Support brings the open dialog to the front."""
            win = getattr(self, attr, None)
            try:
                if win is not None and win.winfo_exists():
                    win.lift()
                    win.focus_force()
                    return True
            except tk.TclError:
                pass
            return False

        def _dialog(self, title, size):
            win = ctk.CTkToplevel(self)
            win.title(title)
            win.geometry(size)
            win.resizable(False, False)
            win.configure(fg_color=BG)
            win.transient(self)
            try:
                if IS_WIN:
                    win.after(250, lambda: win.iconbitmap(resource_path("icon.ico")))
            except Exception:
                pass
            win.bind("<Escape>", lambda e: win.destroy())
            win.after(150, win.lift)
            win.after(170, win.focus_force)
            return win

        def _diagnostics(self, include_log: bool) -> str:
            how = "exe" if getattr(sys, "frozen", False) else "python"
            tools = ", ".join(f"{TOOLS[k]['label']} {self.tool_versions.get(k) or 'missing'}"
                              for k in ("ffmpeg",))
            lines = [f"App: {APP_NAME} {APP_VERSION} ({how})", f"Support ID: {self.support_id}",
                     f"Windows: {platform.platform()}", f"Tools: {tools}"]
            if include_log:
                try:
                    log = self.term.get("1.0", "end").strip()
                except tk.TclError:
                    log = ""
                lines += ["", "--- Terminal (last part) ---", log[-6000:]]
            return "\n".join(lines)

        def _show_donate(self):
            if self._focus_existing("_donate_win"):
                return
            win = self._donate_win = self._dialog("Tip the developer", "520x760")
            pad = {"padx": 26}
            ctk.CTkLabel(win, text="☕  Buy me a chai", font=self.f_h2, text_color=TEXT
                         ).pack(anchor="w", pady=(20, 0), **pad)
            ctk.CTkLabel(win, text="A small tip to the developer. Everything in the app is free, including the "
                                   "screen recorder, and it stays exactly the same whether you donate or not.",
                         font=self.f_small, text_color=MUTED, wraplength=468, justify="left"
                         ).pack(anchor="w", pady=(4, 14), **pad)
            upi = DONATE_UPI_ID.strip()
            if not is_upi_id(upi):
                ctk.CTkLabel(win, text="Donations open soon. Thank you for wanting to help!", font=self.f_ui,
                             text_color=TEXT, wraplength=468, justify="left").pack(anchor="w", pady=(8, 12), **pad)
                self._button(win, "Open the website", lambda: webbrowser.open(DONATE_PAGE), width=180
                             ).pack(anchor="w", **pad)
                return
            self._label(win, "CHOOSE AN AMOUNT", "₹20 to ₹300").pack(anchor="w", pady=(0, 6), **pad)
            labels = [f"₹{a}" for a in DONATE_AMOUNTS]
            choice = tk.StringVar(value=f"₹{DONATE_DEFAULT}")
            seg = ctk.CTkSegmentedButton(win, values=labels, variable=choice, height=38, font=self.f_ui_b,
                                         selected_color="#BFE8C9", selected_hover_color="#A9DDB6",
                                         unselected_color=SURFACE, unselected_hover_color=SURFACE_2,
                                         fg_color=SURFACE, text_color=TEXT, corner_radius=6)
            seg.pack(fill="x", **pad)
            # consent tick box: local variable only, never written to settings, so it starts unticked every time
            agreed = tk.BooleanVar(value=False)
            consent = ctk.CTkFrame(win, fg_color=SURFACE, border_color=BORDER, border_width=1, corner_radius=8)
            consent.pack(fill="x", pady=(14, 0), **pad)
            tick = ctk.CTkCheckBox(consent, text="", variable=agreed, onvalue=True, offvalue=False, width=24,
                                   checkbox_width=22, checkbox_height=22, fg_color=GREEN, hover_color=GREEN_HOVER,
                                   border_color=MUTED, corner_radius=4)
            tick.pack(side="left", anchor="n", padx=(12, 6), pady=12)
            consent_lbl = ctk.CTkLabel(consent, text=DONATE_CONSENT, font=self.f_small, text_color=TEXT,
                                       wraplength=400, justify="left", anchor="w", cursor="hand2")
            consent_lbl.pack(side="left", fill="x", expand=True, padx=(0, 12), pady=10)
            consent_lbl.bind("<Button-1>", lambda e: tick.toggle())
            qr_box = ctk.CTkFrame(win, fg_color="#FFFFFF", border_color=BORDER, border_width=1, corner_radius=8)
            qr_box.pack(pady=(14, 6))
            qr_lbl = ctk.CTkLabel(qr_box, text="", width=240, height=240, font=self.f_ui, text_color=TEXT,
                                  wraplength=220)
            # placeholder shown instead of the QR until the box is ticked (two widgets: swapping a CTkLabel's
            # image back to None leaves a dead Tk image behind)
            locked_lbl = ctk.CTkLabel(qr_box, text="Tick the box above\nto show the QR code", width=240, height=240,
                                      font=self.f_ui, text_color=FAINT, fg_color=SURFACE, corner_radius=6)
            how = ctk.CTkLabel(win, text="", font=self.f_small, text_color=MUTED, wraplength=468, justify="center")
            how.pack(**pad)
            row = ctk.CTkFrame(win, fg_color="transparent")
            row.pack(pady=(12, 0), **pad)
            id_lbl = ctk.CTkLabel(row, text="", font=self.f_mono, text_color=TEXT)
            id_lbl.pack(side="left")
            copied = ctk.CTkLabel(row, text="", font=self.f_small, text_color=GREEN)

            def copy_id():
                if not agreed.get():
                    return
                self.clipboard_clear()
                self.clipboard_append(upi)
                copied.configure(text="  copied ✓")
                win.after(1500, lambda: copied.configure(text="") if copied.winfo_exists() else None)
            copy_btn = self._button(row, "Copy", copy_id, width=70, height=30, font=self.f_small)
            copy_btn.pack(side="left", padx=(10, 0))
            copied.pack(side="left")
            win._donate_parts = {"agreed": agreed, "tick": tick, "qr": qr_lbl, "locked": locked_lbl,
                                 "copy": copy_btn, "id": id_lbl, "how": how, "seg": seg}  # for the window test

            def show(_value=None):
                amount = donate_amount(choice.get().lstrip("₹"))
                if not agreed.get():  # QR, UPI ID and Copy stay off until the box is ticked
                    qr_lbl.pack_forget()
                    locked_lbl.pack(padx=12, pady=12)
                    how.configure(text=f"Amount: ₹{amount}")
                    id_lbl.configure(text="UPI ID: shown after you tick the box", text_color=FAINT)
                    copy_btn.configure(state="disabled")
                    return
                locked_lbl.pack_forget()
                qr_lbl.pack(padx=12, pady=12)
                id_lbl.configure(text=f"UPI ID: {upi}", text_color=TEXT)
                copy_btn.configure(state="normal")
                link = upi_link(upi, amount)
                try:
                    import io as _io
                    pic = Image.open(_io.BytesIO(upi_qr_png(link)))
                    self._qr_img = ctk.CTkImage(light_image=pic, dark_image=pic, size=(240, 240))
                    qr_lbl.configure(image=self._qr_img, text="")
                    how.configure(text=f"Scan with GPay, PhonePe, Paytm or any UPI app to give ₹{amount}.")
                except Exception:  # segno or Pillow missing: the UPI ID still works by hand
                    qr_lbl.configure(text=f"Pay ₹{amount}\nto {upi}\nin any UPI app")
                    how.configure(text="Open your UPI app, choose Pay to UPI ID, and enter the ID below.")
            seg.configure(command=show)
            tick.configure(command=show)
            show()
            ctk.CTkLabel(win, text="Thank you! 🙏", font=self.f_small, text_color=FAINT, wraplength=468,
                         justify="left").pack(anchor="w", pady=(14, 0), **pad)
            link_lbl = ctk.CTkLabel(win, text="Open the donate page on the website ↗", font=self.f_small,
                                    text_color=GREEN, cursor="hand2")
            link_lbl.pack(anchor="w", pady=(4, 18), **pad)
            link_lbl.bind("<Button-1>", lambda e: webbrowser.open(DONATE_PAGE))

        def _show_support(self):
            if self._focus_existing("_support_win"):
                return
            win = self._support_win = self._dialog("Support", "600x710")
            pad = {"padx": 26}
            ctk.CTkLabel(win, text="💬  Support", font=self.f_h2, text_color=BLUE).pack(anchor="w", pady=(18, 0), **pad)
            ctk.CTkLabel(win, text="Tell us what happened. We reply to the email address you enter.",
                         font=self.f_small, text_color=MUTED).pack(anchor="w", pady=(2, 4), **pad)

            def label(text, hint=""):
                self._label(win, text, hint).pack(anchor="w", pady=(10, 4), **pad)

            label("SUBJECT")
            subj_var = tk.StringVar(value=SUPPORT_SUBJECTS[0])
            self._option(win, subj_var, list(SUPPORT_SUBJECTS), 548).pack(anchor="w", **pad)
            label("YOUR EMAIL", "we reply here")
            email_var = tk.StringVar(value=self.settings.get("support_email") or "")
            e_email = self._entry(win, email_var)
            e_email.pack(fill="x", **pad)
            label("WHAT HAPPENED", "what you did, what you expected, what you saw")
            box = ctk.CTkTextbox(win, height=110, font=self.f_ui, fg_color=BG, border_color=BORDER, border_width=1,
                                 text_color=TEXT, corner_radius=6, wrap="word")
            box.pack(fill="x", **pad)
            label("SCREENSHOT", "optional")
            shot_row = ctk.CTkFrame(win, fg_color="transparent")
            shot_row.pack(fill="x", **pad)
            shot = {"jpeg": None}
            status = ctk.CTkLabel(win, text="", font=self.f_small, text_color=MUTED, wraplength=540, justify="left")

            def set_shot(data, name=""):
                shot["jpeg"] = data
                if data:
                    shot_lbl.configure(text=f"✓ {name} · {max(1, len(data) // 1024)} KB", text_color=GREEN)
                    btn_rm.pack(side="left", padx=(8, 0))
                else:
                    shot_lbl.configure(text="No screenshot", text_color=FAINT)
                    btn_rm.pack_forget()

            def capture():
                win.withdraw()  # hide this form so the picture shows the app itself
                self.update()

                def grab():
                    try:
                        import io as _io
                        from PIL import ImageGrab
                        x, y = self.winfo_rootx(), self.winfo_rooty()
                        img = ImageGrab.grab(bbox=(x, y, x + self.winfo_width(), y + self.winfo_height()),
                                             all_screens=True)
                        buf = _io.BytesIO()
                        img.save(buf, "PNG")
                        set_shot(normalize_screenshot(buf.getvalue()), "app window")
                        status.configure(text="")
                    except Exception as e:
                        status.configure(text=f"Couldn't capture the window ({type(e).__name__}). "
                                              "Use 'Attach image…' instead.", text_color=RED)
                    finally:
                        win.deiconify()
                        win.lift()
                        win.focus_force()
                self.after(350, grab)

            def attach():
                path = filedialog.askopenfilename(parent=win, title="Choose a screenshot",
                                                  filetypes=[("Pictures", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                                                             ("All files", "*.*")])
                if not path:
                    return
                try:
                    if os.path.getsize(path) > 25 * 1024 * 1024:
                        raise ValueError("bigger than 25 MB")
                    with open(path, "rb") as f:
                        set_shot(normalize_screenshot(f.read()), os.path.basename(path)[:40])
                    status.configure(text="")
                except Exception:
                    status.configure(text="That file isn't a picture the app can read. Try a .png or .jpg "
                                          "under 25 MB.", text_color=RED)

            self._button(shot_row, "📷 Capture this app", capture, width=170, height=34,
                         font=self.f_small).pack(side="left")
            self._button(shot_row, "Attach image…", attach, width=130, height=34,
                         font=self.f_small).pack(side="left", padx=(8, 0))
            shot_lbl = ctk.CTkLabel(shot_row, text="No screenshot", font=self.f_small, text_color=FAINT)
            shot_lbl.pack(side="left", padx=(12, 0))
            btn_rm = ctk.CTkButton(shot_row, text="✕", width=28, height=28, command=lambda: set_shot(None),
                                   font=self.f_small, corner_radius=6, fg_color=SURFACE, hover_color=SURFACE_2,
                                   text_color=MUTED, border_width=1, border_color=BORDER)
            inc_var = tk.BooleanVar(value=True)
            ctk.CTkCheckBox(win, text="Include app details (version, Windows, tools, terminal log)",
                            variable=inc_var, font=self.f_small, text_color=TEXT, fg_color=GREEN,
                            hover_color=GREEN_HOVER, border_color=BORDER, checkbox_width=20, checkbox_height=20
                            ).pack(anchor="w", pady=(14, 0), **pad)
            ctk.CTkLabel(win, text=f"Your support ID {self.support_id} is sent with the message.", font=self.f_small,
                         text_color=FAINT).pack(anchor="w", pady=(4, 0), **pad)
            status.pack(anchor="w", pady=(6, 0), **pad)
            fallback = ctk.CTkFrame(win, fg_color="transparent")
            send_btn = ctk.CTkButton(win, text="Send message", height=46, font=self.f_btn, corner_radius=6,
                                     fg_color=GREEN, hover_color=GREEN_HOVER, text_color="#FFFFFF",
                                     text_color_disabled="#E6E6E6")
            send_btn.pack(fill="x", side="bottom", pady=(8, 18), **pad)

            def full_text(subject, email, msg, diag):
                return f"Subject: {subject}\nFrom: {email}\n\n{msg}\n\n{diag}"

            def show_fallback(reason, subject, email, msg, diag):
                status.configure(text=f"{reason} Please email us at {SUPPORT_EMAIL} instead. "
                                      "Your message is ready below.", text_color=AMBER_TEXT)
                for w in fallback.winfo_children():
                    w.destroy()
                text = full_text(subject, email, msg, diag)

                def copy_msg():
                    self.clipboard_clear()
                    self.clipboard_append(text)
                    status.configure(text="Message copied. Paste it into an email to " + SUPPORT_EMAIL + ".",
                                     text_color=GREEN)

                def open_mail():
                    webbrowser.open(support_mailto(f"[{APP_NAME}] {subject} ({self.support_id})", text))
                self._button(fallback, "Open email app", open_mail, width=150, height=34,
                             font=self.f_small).pack(side="left")
                self._button(fallback, "Copy message", copy_msg, width=130, height=34,
                             font=self.f_small).pack(side="left", padx=(8, 0))
                if shot["jpeg"]:
                    def show_shot():
                        folder = os.path.join(DATA_DIR, "support")
                        os.makedirs(folder, exist_ok=True)
                        p = os.path.join(folder, f"screenshot-{self.support_id}-{datetime.now():%H%M%S}.jpg")
                        with open(p, "wb") as f:
                            f.write(shot["jpeg"])
                        if IS_WIN:
                            subprocess.Popen(f'explorer /select,"{os.path.normpath(p)}"')
                        else:
                            self._open_path(folder)
                    self._button(fallback, "Show screenshot", show_shot, width=140, height=34,
                                 font=self.f_small).pack(side="left", padx=(8, 0))
                fallback.pack(anchor="w", pady=(8, 0), **pad)
                win.geometry("600x760")

            def done(ok, user_msg, detail, subject, email, msg, diag):
                if ok:
                    self._support_last = time.time()
                    self._log(f"Support message sent (support ID {self.support_id}).", "ok")
                else:
                    self._log(f"Support message not sent: {detail or user_msg}", "warn")
                try:
                    if not win.winfo_exists():
                        return
                except tk.TclError:
                    return
                if ok:
                    status.configure(text=f"✓ Sent. We'll reply to {email}.", text_color=GREEN)
                    send_btn.configure(text="Close", state="normal", command=win.destroy)
                else:
                    send_btn.configure(text="Try again", state="normal")
                    show_fallback(user_msg, subject, email, msg, diag)

            def send():
                subject, email = subj_var.get(), email_var.get().strip()
                msg = box.get("1.0", "end").strip()
                if not EMAIL_RE.match(email):
                    status.configure(text="Enter your email address so we can reply.", text_color=RED)
                    e_email.focus_set()
                    return
                if len(msg) < 10:
                    status.configure(text="Please describe the problem in a sentence or two.", text_color=RED)
                    box.focus_set()
                    return
                wait = int(SUPPORT_COOLDOWN_S - (time.time() - self._support_last))
                if wait > 0:
                    status.configure(text=f"Please wait {wait} seconds before sending another message.",
                                     text_color=AMBER_TEXT)
                    return
                self.settings.set("support_email", email)
                diag = self._diagnostics(bool(inc_var.get()))
                if not SUPPORT_URL:
                    show_fallback("Sending from the app isn't switched on yet.", subject, email, msg, diag)
                    return
                payload = build_support_payload(subject, email, msg, self.support_id, diag, shot["jpeg"])
                send_btn.configure(state="disabled", text="Sending…")
                status.configure(text="Sending your message…", text_color=MUTED)

                def work():
                    ok, user_msg, detail = send_support(payload)
                    self.ui(done, ok, user_msg, detail, subject, email, msg, diag)
                threading.Thread(target=work, daemon=True).start()
            send_btn.configure(command=send)
            if not email_var.get():
                win.after(250, e_email.focus_set)

        # ------------------------------------------------------------- thread plumbing
        def ui(self, fn, *args):
            """Worker threads never touch widgets; they send work to the main thread."""
            self.q.put((fn, args))

        def _pump(self):
            try:
                for _ in range(300):
                    fn, args = self.q.get_nowait()
                    try:
                        fn(*args)
                    except Exception as e:  # keep the app alive
                        self._log(f"Internal UI error: {e}", "err")
            except queue.Empty:
                pass
            try:
                self.after(50, self._pump)
            except tk.TclError:  # window already closed
                pass

        def ask_main(self, fn):
            """Run a dialog on the main thread from a worker and wait for the answer."""
            ev, box = threading.Event(), {}

            def run():
                box["r"] = fn()
                ev.set()
            self.ui(run)
            ev.wait()
            return box.get("r")

        def _log(self, msg: str, level: str = "info"):
            self.term.configure(state="normal")
            self.term.insert("end", f"[{LOG_TAGS.get(level, 'INFO')}] ", level)
            self.term.insert("end", msg + "\n")
            self.term.see("end")
            self.term.configure(state="disabled")

        def tlog(self, msg, level="info"):
            self.ui(self._log, msg, level)

        def _clear_log(self):
            self.term.configure(state="normal")
            self.term.delete("1.0", "end")
            self.term.configure(state="disabled")

        # ------------------------------------------------------------- update check
        def _app_update_setting_changed(self):
            on = bool(self.update_var.get())
            self.settings.set("update_check", on)
            if on:
                self._app_update_maybe_check(force=True)
            else:
                self.btn_newver.pack_forget()

        def _app_update_maybe_check(self, force=False):
            """At start-up: show what the last check found, and ask GitHub again if that was a day ago."""
            if not self.settings.get("update_check", True):
                return
            cached = self.settings.get("update_latest")
            if isinstance(cached, str) and is_newer(cached):
                self._app_update_found(cached, self.settings.get("update_page"))
            try:
                last = float(self.settings.get("update_last") or 0)
            except (TypeError, ValueError):
                last = 0.0
            if force or not 0 <= time.time() - last < UPDATE_EVERY_S:
                threading.Thread(target=self._app_update_worker, daemon=True).start()

        def _app_update_worker(self):
            self.ui(self._app_update_checked, fetch_latest_release())

        def _app_update_checked(self, found):
            self.settings.set("update_last", time.time())
            if not found:
                return  # offline, or GitHub didn't answer: try again next time
            ver, page = found
            self.settings.set("update_latest", ver)
            self.settings.set("update_page", page)
            if is_newer(ver) and self.settings.get("update_check", True):
                self._app_update_found(ver, page)
            else:
                self.btn_newver.pack_forget()

        def _app_update_found(self, ver, page):
            self._app_update_page = safe_release_page(page)
            self.btn_newver.configure(text=f"⬆ New version v{ver}")
            if not self.btn_newver.winfo_ismapped():
                self.btn_newver.pack(side="left", padx=(12, 0))
            if not self._app_update_logged:
                self._app_update_logged = True
                self._log(f"A new version, v{ver}, is available. Click '⬆ New version' at the top to get it.", "ok")

        # ------------------------------------------------------------- startup
        def _startup(self):
            self._log(f"{APP_NAME} {APP_VERSION} ready" + (" (running as Administrator)" if is_admin() else ""))
            if self.settings.first_run:
                self.lift()
                messagebox.showinfo(APP_NAME, "Welcome! First, choose the folder where your recordings should be "
                                              "saved.\n\nYou can change it any time with 'Browse'.", parent=self)
                start = default_save_dir()
                os.makedirs(start, exist_ok=True)
                chosen = filedialog.askdirectory(parent=self, title="Choose where to save your recordings",
                                                 initialdir=start, mustexist=False)
                self.save_var.set(os.path.normpath(chosen) if chosen else start)
                self._remember_folder()
                self._log(f"Recordings will be saved to: {self.save_var.get()}", "ok")
            else:
                self._log(f"Saving to: {self.save_var.get()}")
            self._clean_stale_temp()
            self._log("Click ⏺ Record screen to open the recording bar.")
            if self.settings.get("show_guide", True):
                self._show_guide()  # opened before the FFmpeg check, so the install question waits for it
            threading.Thread(target=self._detect_tools_worker, args=(True,), daemon=True).start()
            self.after(4000, self._app_update_maybe_check)

        def _clean_stale_temp(self):
            folder = self.save_var.get()
            for d in glob.glob(os.path.join(folder, ".vd-temp-*")):
                try:
                    if time.time() - os.path.getmtime(d) > 3600:
                        remove_tree(d, tries=2)
                except OSError:
                    pass

        def _detect_tools_worker(self, offer_install=False):
            tools = detect_tools()
            versions = {k: (tool_version(k, p) if p else None) for k, p in tools.items() if k in TOOLS}
            self.ui(self._tools_ready, tools, versions, offer_install)

        def _tools_ready(self, tools, versions, offer_install):
            self.tools = tools
            self.tool_versions = versions
            chip = self.chips["ffmpeg"]
            if tools.get("ffmpeg"):
                chip.configure(text=f"● FFmpeg {versions.get('ffmpeg') or ''}".rstrip(), text_color=GREEN)
                self.btn_tools.configure(text="FFMPEG  ● ready  ▾", text_color=GREEN)
                self.banner.grid_forget()
                if self.settings.get("install_declined"):
                    self.settings.set("install_declined", False)
                self._log("FFmpeg found. Everything is ready.", "ok")
                return
            chip.configure(text="● FFmpeg: missing", text_color=RED)
            self.btn_tools.configure(text="FFMPEG  ● missing  ▾", text_color=RED)
            self.banner_text.configure(text="FFmpeg is missing. The recorder needs it to capture the screen.")
            self.banner_cmd.configure(state="normal")
            self.banner_cmd.delete(0, "end")
            self.banner_cmd.insert(0, winget_command("ffmpeg"))
            self.banner_cmd.configure(state="readonly")
            self.banner.grid(row=3, column=0, sticky="ew", padx=24, pady=(10, 0))
            self._log(f"FFmpeg not found. Install command: {winget_command('ffmpeg')}", "warn")
            if offer_install and not self.settings.get("install_declined"):
                if self._guide_open():
                    self._pending_install = ["ffmpeg"]  # ask after the guide is closed
                else:
                    self._offer_install(["ffmpeg"])

        def _offer_install(self, missing):
            names = " and ".join(TOOLS[k]["label"] for k in missing)
            if messagebox.askyesno(APP_NAME, f"{names} {'is' if len(missing) == 1 else 'are'} needed to record "
                                             f"the screen.\n\nInstall automatically now? (about 1-2 minutes)", parent=self):
                self._install(missing)
            else:
                self.settings.set("install_declined", True)  # don't ask on every launch; the banner stays
                self._log("OK. Use 'Install automatically' in the yellow bar whenever you're ready.", "warn")

        def _github_button(self, parent):
            """Small dark GitHub button. Drop the official GitHub mark in as github.png to show it as the icon."""
            img = None
            path = resource_path("github.png")
            if Image is not None and os.path.isfile(path):
                try:
                    pic = Image.open(path).convert("RGBA")
                    img = ctk.CTkImage(light_image=pic, dark_image=pic, size=(18, 18))
                except Exception:
                    img = None
            if img is not None:
                return ctk.CTkButton(parent, text="GitHub", image=img, compound="left", width=100, height=34,
                                     font=self.f_small_b, corner_radius=6, fg_color=SURFACE, hover_color=SURFACE_2,
                                     text_color=TEXT, border_width=1, border_color=BORDER,
                                     command=lambda: webbrowser.open(GITHUB_URL))
            return ctk.CTkButton(parent, text="</>  GitHub", width=104, height=34, font=self.f_small_b,
                                 corner_radius=6, fg_color="#24292F", hover_color="#3B434B", text_color="#FFFFFF",
                                 command=lambda: webbrowser.open(GITHUB_URL))

        # ------------------------------------------------------------- how-to-use guide
        def _guide_open(self) -> bool:
            g = getattr(self, "_guide", None)
            try:
                return g is not None and bool(g.winfo_exists())
            except tk.TclError:
                return False

        def _show_guide(self):
            if self._guide_open():
                self._guide.lift()
                self._guide.focus_force()
                return
            win = ctk.CTkToplevel(self)
            self._guide = win
            win.title(f"How to use {APP_NAME}")
            win.geometry("860x620")
            win.minsize(740, 520)
            win.configure(fg_color=BG)
            win.transient(self)
            try:
                if IS_WIN:
                    win.after(250, lambda: win.iconbitmap(resource_path("icon.ico")))
            except Exception:
                pass
            ctk.CTkLabel(win, text=f"How to use {APP_NAME}", font=self.f_h2, text_color=GREEN
                         ).pack(anchor="w", padx=24, pady=(18, 0))
            ctk.CTkLabel(win, text="Click Record screen, switch on what you want, press Start. "
                                   "The tabs explain the recording bar, the camera and mic, and what to do if something goes wrong.",
                         font=self.f_small, text_color=MUTED, wraplength=800, justify="left"
                         ).pack(anchor="w", padx=24, pady=(2, 0))
            tabs = ctk.CTkTabview(win, fg_color=SURFACE, border_color=BORDER, border_width=1, corner_radius=8,
                                  segmented_button_fg_color=SURFACE_2, segmented_button_selected_color="#C8F0D2",
                                  segmented_button_selected_hover_color="#B2E8BF",
                                  segmented_button_unselected_color=SURFACE_2,
                                  segmented_button_unselected_hover_color=BORDER, text_color=TEXT)
            tabs.pack(fill="both", expand=True, padx=20, pady=(10, 8))
            for title, blocks in GUIDE:
                tab = tabs.add(title)
                body = ctk.CTkScrollableFrame(tab, fg_color=BG, corner_radius=6)
                body.pack(fill="both", expand=True, padx=4, pady=4)
                for kind, text in blocks:
                    if kind == "h":
                        ctk.CTkLabel(body, text=text, font=self.f_ui_b, text_color=TEXT, anchor="w", justify="left",
                                     wraplength=740).pack(fill="x", anchor="w", padx=12, pady=(14, 4))
                    else:
                        ctk.CTkLabel(body, text=text, font=self.f_ui, text_color=TEXT, anchor="w", justify="left",
                                     wraplength=740).pack(fill="x", anchor="w", padx=12, pady=2)
            foot = ctk.CTkFrame(win, fg_color="transparent")
            foot.pack(fill="x", padx=24, pady=(0, 16))
            show_var = tk.BooleanVar(value=bool(self.settings.get("show_guide", True)))
            ctk.CTkCheckBox(foot, text="Show this guide when the app opens", variable=show_var, font=self.f_small,
                            text_color=MUTED, fg_color=GREEN, hover_color=GREEN_HOVER, border_color=BORDER,
                            checkbox_width=20, checkbox_height=20).pack(side="left")

            def close():
                self.settings.set("show_guide", bool(show_var.get()))
                try:
                    win.destroy()
                except tk.TclError:
                    pass
                self._guide = None
                pending, self._pending_install = getattr(self, "_pending_install", None), None
                if pending:
                    self.after(200, self._offer_install, pending)
            self._button(foot, "Got it, let's go", close, width=160, height=38, primary=True).pack(side="right")
            win.protocol("WM_DELETE_WINDOW", close)
            win.bind("<Escape>", lambda e: close())
            win.after(150, win.lift)
            win.after(170, win.focus_force)

        def _install(self, keys):
            if self.installing:
                return
            if keys is None:
                keys = [k for k in TOOLS if TOOLS[k]["required"] and not self.tools.get(k)]
            if not keys:
                return
            self.installing = True
            self.btn_install.configure(state="disabled", text="Installing…")
            self.btn_record.configure(state="disabled")

            def work():
                for k in keys:
                    install_tool(k, self.tlog)
                self.ui(self._install_done)
            threading.Thread(target=work, daemon=True).start()

        def _install_done(self):
            self.installing = False
            self.btn_install.configure(state="normal", text="Install automatically")
            self.btn_record.configure(state="normal")
            threading.Thread(target=self._detect_tools_worker, args=(False,), daemon=True).start()

        # ------------------------------------------------------------- folder
        def _browse(self):
            start = self.save_var.get() if os.path.isdir(self.save_var.get()) else videos_folder()
            chosen = filedialog.askdirectory(parent=self, title="Choose where to save your recordings", initialdir=start)
            if chosen:
                self.save_var.set(os.path.normpath(chosen))
                self._remember_folder()
                self._log(f"Recordings will be saved to: {self.save_var.get()}", "ok")

        def _remember_folder(self):
            folder = self.save_var.get().strip().strip('"')
            if folder:
                self.settings.set("save_dir", os.path.normpath(folder))

        def _open_save_dir(self):
            folder = self.save_var.get()
            os.makedirs(folder, exist_ok=True)
            self._open_path(folder)

        @staticmethod
        def _open_path(path):
            if IS_WIN:
                os.startfile(path)  # noqa
            else:
                subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # ------------------------------------------------------------- result / error
        def _done(self, res: Result, details: str | None = None):
            self.result = res
            self.details_text = details or ""
            self.err_frame.grid_forget()
            self.stage.grid_forget()
            name = os.path.basename(res.path)
            meta = []
            if res.width and res.height:
                meta.append(f"{res.width}×{res.height}")
            meta.append(human_size(res.size))
            if res.duration:
                meta.append(fmt_time(res.duration))
            self.res_title.configure(text=f"✓ Saved: {name}")
            self.res_meta.configure(text="  ·  ".join(meta))
            self.res_frame.grid(row=0, column=0, sticky="ew")
            self._log(f"Saved: {name}  ·  {'  ·  '.join(meta)}", "ok")

        def _show_error(self, title, message, details):
            if details:
                self.details_text = details
            self.stage.grid_forget()
            self.res_frame.grid_forget()
            self.err_title.configure(text="⛔ " + title)
            self.err_msg.configure(text=message)
            self.err_frame.grid(row=0, column=0, sticky="ew")

        def _dismiss_error(self):
            self.err_frame.grid_forget()
            self.stage.configure(text="Ready.", text_color=TEXT)
            self.stage.grid(row=0, column=0, sticky="ew")

        def _show_details(self):
            win = ctk.CTkToplevel(self)
            win.title("Details (full log)")
            win.geometry("900x560")
            win.configure(fg_color=BG)
            box = ctk.CTkTextbox(win, font=self.f_mono, fg_color=SURFACE, text_color=TEXT, border_color=BORDER,
                                 border_width=1, wrap="none")
            box.pack(fill="both", expand=True, padx=14, pady=(14, 8))
            text = self.details_text or self.term.get("1.0", "end").strip() or "No details yet."
            box.insert("1.0", text)
            box.configure(state="disabled")

            def copy():
                self.clipboard_clear()
                self.clipboard_append(text)
            self._button(win, "Copy to clipboard", copy, width=170, height=36).pack(pady=(0, 14))
            win.transient(self)
            win.after(150, win.lift)
            win.after(160, win.focus_force)

        def _play(self):
            if self.result and os.path.isfile(self.result.path) and file_ext(self.result.path) in MEDIA_EXTS:
                self._open_path(self.result.path)  # media files only, never programs

        def _show_in_folder(self):
            if not self.result:
                return
            path = os.path.normpath(self.result.path)
            if IS_WIN and os.path.exists(path):
                subprocess.Popen(f'explorer /select,"{path}"')  # opens Explorer with the file selected
            else:
                self._open_path(os.path.dirname(path))

        # ------------------------------------------------------------- screen recorder: compact bar + camera
        def _record_clicked(self):
            if self._rec_state in ("countdown", "recording"):
                self._rec_stop()
            elif self._rec_state == "idle":
                self._show_rec_bar()

        def _rec_load_opts(self) -> dict:
            saved = self.settings.get("rec") or {}
            pos = saved.get("cam_pos")
            return {"system": bool(saved.get("system", True)), "mic": bool(saved.get("mic", False)),
                    "camera": bool(saved.get("camera", False)),
                    "fps": saved.get("fps") if saved.get("fps") in REC_FPS else "30 fps",
                    "quality": saved.get("quality") if saved.get("quality") in REC_QUALITY else "Standard",
                    "mouse": bool(saved.get("mouse", True)), "hide": bool(saved.get("hide", True)),
                    "cam_size": saved.get("cam_size") if saved.get("cam_size") in CAM_SIZES else "Medium",
                    "cam_style": saved.get("cam_style") if saved.get("cam_style") in CAM_STYLES else "Bubble",
                    "cam_name": str(saved.get("cam_name") or ""),
                    "cam_pos": pos if isinstance(pos, list) and len(pos) == 2 else None}

        def _rec_save_opts(self):
            self.settings.set("rec", dict(self._rec_opts))

        def _bar_alive(self) -> bool:
            try:
                return self._rec_bar is not None and bool(self._rec_bar.winfo_exists())
            except tk.TclError:
                return False

        def _show_rec_bar(self):
            if self._bar_alive():
                self._rec_bar.lift()
                return
            if not self.tools.get("ffmpeg"):
                messagebox.showwarning(APP_NAME, "The screen recorder needs FFmpeg. Click 'Install automatically' "
                                                 "in the yellow bar first.", parent=self)
                return
            self._rec_opts = self._rec_load_opts()
            self._mic_colour = MIC_SILENT
            self._rec_make_bar()
            if self._rec_opts["camera"]:
                self._cam_open()
            if self._rec_opts["mic"]:
                self._mic_meter_start()

        def _rec_make_bar(self):
            """Compact dark bar at the top of the screen (like the Snipping Tool recorder). Icons only."""
            bar = ctk.CTkToplevel(self)
            self._rec_bar = bar
            bar.overrideredirect(True)
            bar.attributes("-topmost", True)
            bar.configure(fg_color=BAR_HOVER)  # 1 px frame around the bar
            f = ctk.CTkFrame(bar, fg_color=BAR_BG, corner_radius=0)
            f.pack(fill="both", expand=True, padx=1, pady=1)
            grip = ctk.CTkLabel(f, text="⋮⋮", font=self.f_small_b, text_color=BAR_DIM, width=14)
            grip.pack(side="left", padx=(8, 4), pady=5)
            self._rec_start_btn = ctk.CTkButton(f, text="●  Start", command=self._rec_main_button, width=92, height=30,
                                                font=self.f_small_b, corner_radius=6, fg_color=RED,
                                                hover_color=RED_HOVER, text_color="#FFFFFF",
                                                text_color_disabled="#F0B8BB")
            self._rec_start_btn.pack(side="left", pady=5)
            self._rec_pause_btn = ctk.CTkButton(f, text=self.bar_icons["pause"], command=self._rec_pause_clicked,
                                                width=30, height=30, corner_radius=6, fg_color=BAR_BG,
                                                hover_color=BAR_HOVER, text_color=BAR_TEXT,
                                                text_color_disabled="#484F58", border_width=0, font=self.f_icon,
                                                state="disabled")
            self._rec_pause_btn.pack(side="left", padx=(4, 0), pady=5)
            self._rec_dot = ctk.CTkLabel(f, text="", font=self.f_small_b, text_color="#FF4D4F", width=10)
            self._rec_dot.pack(side="left", padx=(4, 0))
            self._rec_label = ctk.CTkLabel(f, text="00:00:00", font=self.f_timer_s, text_color=BAR_TEXT, width=96,
                                           anchor="w")
            self._rec_label.pack(side="left", padx=(2, 4))

            def divider():
                ctk.CTkFrame(f, width=1, height=20, fg_color=BAR_HOVER).pack(side="left", padx=(0, 6))

            def icon_button(text, command, width=32, font=None):
                return ctk.CTkButton(f, text=text, command=command, width=width, height=30, corner_radius=6,
                                     fg_color=BAR_BG, hover_color=BAR_HOVER, text_color=BAR_DIM,
                                     text_color_disabled="#484F58", border_width=0, font=font or self.f_icon)
            divider()
            self._rec_toggles = {}
            ic = self.bar_icons
            for key in ("mic", "system", "camera"):
                b = icon_button(ic[key], lambda k=key: self._rec_toggle(k))
                b.pack(side="left", padx=(0, 0 if key == "camera" else 2))
                self._rec_toggles[key] = b
            self._rec_cam_caret = icon_button(ic["caret"], self._rec_camera_menu, width=18, font=self.f_icon_small)
            self._rec_cam_caret.pack(side="left", padx=(0, 6))
            divider()
            self._rec_gear = icon_button(ic["gear"], self._rec_settings_menu, width=30)
            self._rec_gear.pack(side="left", padx=(0, 2))
            self._rec_close_btn = icon_button(ic["close"], self._rec_close_bar, width=30)
            self._rec_close_btn.pack(side="left", padx=(0, 6))
            for key in self._rec_toggles:
                self._rec_style_toggle(key)
            for w, tip in ((self._rec_toggles["mic"], "Microphone: grey = quiet, green = voice, red = non-stop talking"),
                           (self._rec_toggles["system"], "PC sound (everything you hear)"),
                           (self._rec_toggles["camera"], "Camera"),
                           (self._rec_cam_caret, "Camera: Bubble or Cutout, and size"),
                           (self._rec_pause_btn, "Pause / go on"),
                           (self._rec_gear, "Settings"), (self._rec_close_btn, "Close")):
                self._tip(w, tip)
            bar.update_idletasks()
            x = (bar.winfo_screenwidth() - bar.winfo_reqwidth()) // 2
            bar.geometry(f"+{max(0, x)}+12")
            drag = {}

            def press(e):
                drag["x"], drag["y"] = e.x_root - bar.winfo_x(), e.y_root - bar.winfo_y()

            def move(e):
                if drag:
                    bar.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")
            for w in (f, grip, self._rec_dot, self._rec_label):
                w.bind("<ButtonPress-1>", press)
                w.bind("<B1-Motion>", move)
            bar.bind("<Escape>", lambda e: self._rec_close_bar())
            bar.after(120, lambda: exclude_from_capture(bar, True))  # the bar never appears in the video
            bar.after(150, bar.lift)

        def _tip(self, widget, text):
            """Small dark hint under an icon after you hover for half a second (never shows in recordings)."""
            state = {"win": None, "job": None}

            def show():
                state["job"] = None
                try:
                    if state["win"] is not None or not widget.winfo_exists():
                        return
                    tw = tk.Toplevel(widget)
                    tw.overrideredirect(True)
                    tw.attributes("-topmost", True)
                    tk.Label(tw, text=text, bg=BAR_HOVER, fg=BAR_TEXT, padx=8, pady=3,
                             font=(self.f_small.cget("family"), 9)).pack()
                    tw.geometry(f"+{widget.winfo_rootx()}+{widget.winfo_rooty() + widget.winfo_height() + 4}")
                    state["win"] = tw
                    tw.after(60, lambda: exclude_from_capture(tw, True))
                    widget.after(200, watch)
                except tk.TclError:
                    pass

            def watch():
                """Close the hint as soon as the pointer is off the icon, even if Windows misses the 'leave'."""
                if state["win"] is None:
                    return
                try:
                    px, py = widget.winfo_pointerxy()
                    x0, y0 = widget.winfo_rootx(), widget.winfo_rooty()
                    inside = x0 <= px < x0 + widget.winfo_width() and y0 <= py < y0 + widget.winfo_height()
                except tk.TclError:
                    inside = False
                if inside:
                    widget.after(200, watch)
                else:
                    leave(None)

            def enter(_e):
                if state["job"] is None and state["win"] is None:
                    state["job"] = widget.after(450, show)

            def leave(_e):
                if state["job"] is not None:
                    try:
                        widget.after_cancel(state["job"])
                    except tk.TclError:
                        pass
                    state["job"] = None
                if state["win"] is not None:
                    try:
                        state["win"].destroy()
                    except tk.TclError:
                        pass
                    state["win"] = None
            widget.bind("<Enter>", enter, add="+")
            widget.bind("<Leave>", leave, add="+")
            widget.bind("<ButtonPress-1>", leave, add="+")

        def _rec_style_toggle(self, key):
            b = getattr(self, "_rec_toggles", {}).get(key)
            if b is None or not self._bar_alive():
                return
            if self._rec_opts.get(key):
                colour = self._mic_colour if key == "mic" else BAR_ON
                b.configure(text=self.bar_icons[key], text_color=colour, text_color_disabled=colour, font=self.f_icon)
            else:
                b.configure(text=self.bar_icons[key + "_off"], text_color=BAR_DIM, text_color_disabled="#484F58",
                            font=self.f_icon_off)

        def _rec_toggle(self, key):
            if key != "camera" and self._rec_state != "idle":
                return  # sound is fixed once a recording starts; the camera can be switched any time
            self._rec_opts[key] = not self._rec_opts.get(key)
            self._rec_save_opts()
            if key == "mic":
                if self._rec_opts["mic"]:
                    self._mic_colour = MIC_SILENT
                    self._mic_meter_start()
                else:
                    self._mic_meter_stop()
            self._rec_style_toggle(key)
            if key == "camera":
                if self._rec_opts["camera"]:
                    self._cam_open()
                else:
                    self._cam_close()

        def _rec_bar_state(self, state):
            if not self._bar_alive():
                return
            idle = state == "idle"
            for k in ("mic", "system"):
                self._rec_toggles[k].configure(state="normal" if idle else "disabled")
            cam_state = "disabled" if state == "saving" else "normal"
            self._rec_toggles["camera"].configure(state=cam_state)
            self._rec_cam_caret.configure(state=cam_state)
            for b in (self._rec_gear, self._rec_close_btn):
                b.configure(state="normal" if idle else "disabled")
            paused = state == "recording" and self._rec_paused
            self._rec_pause_btn.configure(state="normal" if state == "recording" and not self._rec_pausing
                                          else "disabled",
                                          text=self.bar_icons["resume" if paused else "pause"],
                                          text_color=PAUSE_AMBER if paused else BAR_TEXT)
            sb = self._rec_start_btn
            if idle:
                sb.configure(text="●  Start", state="normal", fg_color=RED, hover_color=RED_HOVER,
                             text_color="#FFFFFF")
                self._rec_label.configure(text="00:00:00", text_color=BAR_TEXT)
                self._rec_dot.configure(text="")
            elif state == "preparing":
                sb.configure(text="Wait…", state="disabled")
                self._rec_label.configure(text="Checking…", text_color=BAR_DIM)
            elif state == "countdown":
                sb.configure(text="✕  Cancel", state="normal", fg_color=BAR_HOVER, hover_color="#484F58",
                             text_color=BAR_TEXT)
            elif state == "recording":
                sb.configure(text="■  Stop", state="normal", fg_color=RED, hover_color=RED_HOVER,
                             text_color="#FFFFFF")
                self._rec_label.configure(text_color=BAR_TEXT)
            elif state == "saving":
                sb.configure(text="Saving…", state="disabled")
                self._rec_label.configure(text="Saving…", text_color=BAR_DIM)
                self._rec_dot.configure(text="")

        def _rec_camera_menu(self):
            """▾ next to the camera: Bubble or Cutout, size, which camera."""
            if not self._bar_alive() or self._rec_state == "saving":
                return
            o = self._rec_opts
            m = tk.Menu(self._rec_bar, tearoff=0)
            self._rec_cam_menu = m
            v = {k: tk.StringVar(value=o[k]) for k in ("cam_style", "cam_size", "cam_name")}
            self._rec_cam_vars = v  # keep the variables alive while the menu is open

            def pick(key):
                def apply():
                    o[key] = v[key].get()
                    self._rec_save_opts()
                    if not o.get("camera"):  # picking a look means you want the camera: switch it on
                        self._rec_toggle("camera")
                    elif self._cam_win is None:
                        self._cam_open()
                    else:
                        self._cam_restart()
                return apply
            m.add_command(label="Style", state="disabled")
            names = {"Bubble": "Bubble (round)", "Cutout (no background)": "Cutout (no background)"}
            for k in CAM_STYLES:
                m.add_radiobutton(label=names.get(k, k), variable=v["cam_style"], value=k, command=pick("cam_style"))
            m.add_separator()
            m.add_command(label="Size", state="disabled")
            for k in CAM_SIZES:
                m.add_radiobutton(label=k, variable=v["cam_size"], value=k, command=pick("cam_size"))
            if self._cameras and len(self._cameras) > 1:
                m.add_separator()
                m.add_command(label="Camera", state="disabled")
                if v["cam_name"].get() not in self._cameras:
                    v["cam_name"].set(self._cam_device or self._cameras[0])
                for name in self._cameras:
                    m.add_radiobutton(label=name, variable=v["cam_name"], value=name, command=pick("cam_name"))
            m.add_separator()
            m.add_command(label="Turn camera off" if o.get("camera") else "Turn camera on",
                          command=lambda: self._rec_toggle("camera"))
            c = self._rec_cam_caret
            try:
                m.tk_popup(c.winfo_rootx(), c.winfo_rooty() + c.winfo_height() + 2)
            finally:
                m.grab_release()

        # ---- live mic icon: grey when quiet, green with your voice, red after a few seconds of non-stop talking
        def _mic_meter_start(self):
            if self._mic_meter is None:
                self._mic_meter = MicMeter(self._audio_test_factory)
                self._mic_meter.start()
            self._mic_error_logged = False
            self._mic_reset_meter()
            self._mic_tick_ensure()

        def _mic_meter_stop(self):
            m, self._mic_meter = self._mic_meter, None
            if m is not None:
                m.stop()

        def _mic_reset_meter(self):
            self._mic_disp = -120.0
            self._mic_voice_since = None
            self._mic_quiet_since = None

        def _mic_tick_ensure(self):
            if not self._mic_ticking:
                self._mic_ticking = True
                self.after(60, self._mic_tick)

        def _mic_current_level(self):
            if self._rec_state in ("countdown", "recording", "saving"):
                for a in getattr(self.recorder, "audio", None) or []:
                    if a.kind == "mic":
                        return a.level
                return None
            m = self._mic_meter
            if m is None:
                return None
            if m.error:
                if not self._mic_error_logged:
                    self._mic_error_logged = True
                    self._log(f"Can't listen to the microphone: {m.error}", "warn")
                return None
            return m.level

        def _mic_tick(self):
            if not self._bar_alive() or not self._rec_opts.get("mic"):
                self._mic_ticking = False
                return
            level = self._mic_current_level()
            now = time.time()
            if level is None:
                colour = MIC_SILENT
            else:
                self._mic_disp = max(level, self._mic_disp - 1.5)  # jumps up at once, sinks gently
                if level > MIC_VOICE_DB:
                    self._mic_quiet_since = None
                    if self._mic_voice_since is None:
                        self._mic_voice_since = now
                elif self._mic_quiet_since is None:
                    self._mic_quiet_since = now
                elif now - self._mic_quiet_since > 0.5:  # a real pause ends the "non-stop talking" timer
                    self._mic_voice_since = None
                long_talk = self._mic_voice_since is not None and now - self._mic_voice_since >= MIC_LONG_TALK_S
                colour = "#FF4D4F" if long_talk else mic_level_colour(self._mic_disp)
            if colour != self._mic_colour:
                self._mic_colour = colour
                self._rec_style_toggle("mic")
            self.after(60, self._mic_tick)

        def _rec_pause_clicked(self):
            if self._rec_state != "recording" or self._rec_pausing or self.recorder is None:
                return
            self._rec_pausing = True
            self._rec_bar_state("recording")  # greys the pause icon while FFmpeg stops / starts
            rec, going_on = self.recorder, self._rec_paused

            def work():
                try:
                    if going_on:
                        rec.resume()
                    else:
                        rec.pause()
                    self.ui(self._rec_pause_done, not going_on, "")
                except RecorderError as e:
                    self.ui(self._rec_pause_done, True, e.message)
            threading.Thread(target=work, daemon=True).start()

        def _rec_pause_done(self, paused, error):
            self._rec_pausing = False
            self._rec_paused = paused
            if error:
                self._log(f"Couldn't start the screen capture again: {error} Saving what was recorded so far.", "warn")
                self._rec_stop()
                return
            self._log("Paused. Click ▶ on the bar to go on." if paused else "Recording again.", "info")
            self._rec_bar_state("recording")

        def _rec_main_button(self):
            if self._rec_state == "idle":
                self._rec_start_clicked()
            elif self._rec_state in ("countdown", "recording"):
                self._rec_stop()

        def _rec_settings_menu(self):
            if self._rec_state != "idle" or not self._bar_alive():
                return
            o = self._rec_opts
            m = tk.Menu(self._rec_bar, tearoff=0)
            self._rec_menu = m
            v = {k: tk.StringVar(value=o[k]) for k in ("fps", "quality")}
            v.update({k: tk.BooleanVar(value=o[k]) for k in ("mouse", "hide")})
            self._rec_menu_vars = v  # keep the variables alive while the menu is open

            def setter(key):
                def apply():
                    o[key] = v[key].get()
                    self._rec_save_opts()
                return apply

            def section(title):
                m.add_command(label=title, state="disabled")
            section("Frame rate")
            for k in REC_FPS:
                m.add_radiobutton(label=k, variable=v["fps"], value=k, command=setter("fps"))
            m.add_separator()
            section("Quality")
            for k in REC_QUALITY:
                m.add_radiobutton(label=k, variable=v["quality"], value=k, command=setter("quality"))
            m.add_separator()
            m.add_checkbutton(label="Show the mouse pointer", variable=v["mouse"], command=setter("mouse"))
            m.add_checkbutton(label="Minimise this app while recording", variable=v["hide"], command=setter("hide"))
            m.add_separator()
            folder = self.save_var.get()
            shown = folder if len(folder) <= 44 else "…" + folder[-43:]
            m.add_command(label=f"Save to: {shown}", command=lambda: self._open_path(folder))
            x = self._rec_gear.winfo_rootx()
            y = self._rec_gear.winfo_rooty() + self._rec_gear.winfo_height() + 2
            try:
                m.tk_popup(x, y)
            finally:
                m.grab_release()

        def _rec_start_clicked(self):
            folder = self.save_var.get().strip().strip('"') or default_save_dir()
            if not _is_writable(folder):
                messagebox.showwarning(APP_NAME, f"Can't save to “{folder}”. Choose another SAVE TO folder.",
                                       parent=self)
                return
            opts = dict(self._rec_opts)
            self._rec_save_opts()
            self._rec_run += 1
            run = self._rec_run
            self._mic_meter_stop()  # the recording listens to the mic itself (and feeds the icon)
            self._rec_state = "preparing"
            self._rec_bar_state("preparing")
            ffmpeg = self.tools["ffmpeg"]
            cached = self.settings.get("rec_pipeline") or {}

            def work():
                pipe = cached if (cached.get("ffmpeg") == ffmpeg and cached.get("capture") in capture_methods()
                                  and cached.get("v") == REC_PIPELINE_VERSION) else None
                if not pipe:
                    pipe = choose_pipeline(ffmpeg, self.tlog)
                self.ui(self._rec_ready, pipe, opts, folder, run)
            threading.Thread(target=work, daemon=True).start()

        def _rec_ready(self, pipe, opts, folder, run):
            if run != self._rec_run or self._rec_state != "preparing":
                return
            if not pipe:
                self._rec_state = "idle"
                self._rec_bar_state("idle")
                if self._rec_opts.get("mic"):
                    self._mic_meter_start()
                messagebox.showerror(APP_NAME, "Screen capture isn't working with this FFmpeg. Reinstall FFmpeg "
                                               "(winget install --id Gyan.FFmpeg -e), then try again.",
                                     parent=self)
                return
            self.settings.set("rec_pipeline", pipe)
            enc = pipe["encoder"]
            kind = "graphics card" if enc != "libx264" else "processor"
            self._log(f"Screen recorder ready: {pipe['capture']} capture, {enc} encoder (uses your {kind}).", "ok")
            if opts["hide"]:
                self.iconify()
            exclude_from_capture(self, True)
            self._rec_state = "countdown"
            self._rec_bar_state("countdown")
            self.btn_record.configure(**REC_BTN_ACTIVE)
            self._rec_countdown(3, pipe, opts, folder, run)

        def _rec_countdown(self, n, pipe, opts, folder, run):
            if run != self._rec_run or self._rec_state != "countdown":
                return  # cancelled (or a newer start took over)
            if n > 0:
                self._rec_label.configure(text=f"Start in {n}", text_color="#FF7B72")
                self.after(1000, self._rec_countdown, n - 1, pipe, opts, folder, run)
                return
            self._rec_label.configure(text="Starting…", text_color=MUTED)
            self.recorder = ScreenRecorder(self.tools, log=self.tlog, audio_factory=self._audio_test_factory)
            rec = self.recorder

            def work():
                tries = [pipe]
                fallback = {"capture": capture_methods()[-1], "encoder": "libx264", "ffmpeg": pipe["ffmpeg"],
                            "v": REC_PIPELINE_VERSION}
                if fallback["capture"] != pipe["capture"] or fallback["encoder"] != pipe["encoder"]:
                    tries.append(fallback)
                last, last_details = None, ""
                for p in tries:
                    try:
                        rec.start(folder, p, fps=REC_FPS[opts["fps"]], high=REC_QUALITY[opts["quality"]],
                                  mouse=opts["mouse"], system_audio=opts["system"], mic=opts["mic"])
                        if p is not pipe:
                            self.ui(lambda: self.settings.set("rec_pipeline", p))
                        self.ui(self._rec_started, folder, run)
                        return
                    except RecorderError as e:
                        last, last_details = e.message, e.details
                        self.tlog(f"Capture method {p['capture']}/{p['encoder']} failed, trying another…", "warn")
                self.ui(self._rec_failed, last or "Screen capture didn't start.", last_details)
            threading.Thread(target=work, daemon=True).start()

        def _rec_started(self, folder, run):
            if run != self._rec_run or self._rec_state != "countdown":
                return
            self._rec_state = "recording"
            self._rec_paused = self._rec_pausing = False
            self._rec_folder = folder
            self._rec_bar_state("recording")
            self._log("Recording started. Click ■ Stop on the bar when you're done.", "ok")
            if self._rec_opts.get("mic"):
                self._mic_reset_meter()
                self._mic_tick_ensure()
            self._rec_tick()

        def _rec_tick(self):
            if self._rec_state != "recording" or not self.recorder or not self._bar_alive():
                return
            el = self.recorder.elapsed
            if self._rec_paused:  # timer stands still in amber while paused
                self._rec_label.configure(text=fmt_clock(el), text_color=PAUSE_AMBER)
                self._rec_dot.configure(text="●", text_color=PAUSE_AMBER)
            else:
                self._rec_label.configure(text=fmt_clock(el), text_color=BAR_TEXT)
                self._rec_dot.configure(text="●", text_color="#FF4D4F" if int(el * 2) % 2 == 0 else "#F5B5B9")
            self.after(500, self._rec_tick)

        def _rec_stop(self):
            if self._rec_state == "countdown" and not self.recorder:
                self._rec_back_to_ready()
                self._log("Recording cancelled before it started.", "warn")
                return
            if self._rec_state == "countdown" or self._rec_pausing:   # still starting / pausing: wait for it
                self.after(300, self._rec_stop)
                return
            if self._rec_state != "recording":
                return
            self._rec_state = "saving"
            self._rec_bar_state("saving")
            rec, folder = self.recorder, self._rec_folder
            name = unique_base(folder, "Screen Recording " + datetime.now().strftime("%Y-%m-%d %H-%M-%S"), "mp4")
            out = os.path.join(folder, name + ".mp4")

            def work():
                try:
                    res = rec.stop(out)
                    self.ui(self._rec_done, res, "\n".join(rec._stderr), list(rec.warnings))
                except RecorderError as e:
                    self.ui(self._rec_failed, e.message, e.details)
                except Exception as e:
                    self.ui(self._rec_failed, f"Something unexpected went wrong: {e}", "")
            threading.Thread(target=work, daemon=True).start()

        def _rec_restore_main(self):
            exclude_from_capture(self, False)
            self.btn_record.configure(**REC_BTN_IDLE)
            self.deiconify()

        def _rec_back_to_ready(self):
            """Cancel during the countdown: back to the bar, nothing recorded."""
            self._rec_state = "idle"
            self.recorder = None
            self._rec_run += 1
            self._rec_restore_main()
            self._rec_bar_state("idle")
            if self._rec_opts.get("mic"):
                self._mic_meter_start()
            if self._bar_alive():
                self._rec_bar.lift()

        def _rec_close_bar(self, force=False):
            if self._rec_state != "idle" and not force:
                return
            self._cam_close()
            self._mic_meter_stop()
            if self._rec_bar is not None:
                try:
                    self._rec_bar.destroy()
                except tk.TclError:
                    pass
            self._rec_bar = None

        def _rec_cleanup_ui(self):
            self._rec_state = "idle"
            self._rec_paused = self._rec_pausing = False
            self.recorder = None
            self._rec_close_bar(force=True)
            self._rec_restore_main()
            self.lift()
            if self._close_after_rec:
                self.after(300, self.destroy)

        # ---- camera bubble: your webcam in a round window that shows up in the recording, like streamers
        def _px_scale(self) -> float:
            try:
                return max(1.0, float(self.winfo_fpixels("1i")) / 96.0)
            except (tk.TclError, ValueError):
                return 1.0

        def _cam_input_args(self) -> list:
            if self._cam_test_input:
                return list(self._cam_test_input)
            return ["-f", "dshow", "-rtbufsize", "64M", "-i", f"video={self._cam_device}"]

        def _cam_open(self):
            if self._cam_win is not None or Image is None:
                return
            self._cam_make_window()  # shows "Starting camera…" right away
            if self._cam_test_input:
                self._cam_start_feed()
                return
            known = self._cameras

            def work():
                found = known if known else list_cameras(self.tools.get("ffmpeg") or "")
                self.ui(self._cam_found, found)
            threading.Thread(target=work, daemon=True).start()

        def _cam_found(self, cams):
            self._cameras = cams
            if self._cam_win is None:
                return  # switched off while we were looking
            if not cams:
                self._cam_fail("No camera found. Plug one in, or check Settings > Privacy & security > Camera.")
                return
            name = self._rec_opts.get("cam_name")
            self._cam_device = name if name in cams else cams[0]
            self._cam_start_feed()

        def _cam_make_window(self):
            from PIL import ImageDraw, ImageTk
            s = int(CAM_SIZES.get(self._rec_opts.get("cam_size"), CAM_SIZES["Medium"]) * self._px_scale())
            self._cam_size = s
            round_ = IS_WIN  # Windows can make the corners see-through; elsewhere it's a square
            key = CAM_KEY if round_ else "#1F2328"
            win = tk.Toplevel(self)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=key)
            if round_:
                try:
                    win.attributes("-transparentcolor", CAM_KEY)
                except tk.TclError:
                    pass
            ring = max(3, s // 55)
            bg = Image.new("RGB", (s, s), key)
            mask = Image.new("L", (s, s), 0)
            draw_bg, draw_mask = ImageDraw.Draw(bg), ImageDraw.Draw(mask)
            if self._cam_is_cutout():  # no frame at all: only the person shows, the rest is see-through
                draw_mask.rectangle((0, 0, s - 1, s - 1), fill=255)
            else:
                shape = "ellipse" if round_ else "rectangle"
                getattr(draw_bg, shape)((0, 0, s - 1, s - 1), fill="#FFFFFF")  # white ring
                getattr(draw_mask, shape)((ring, ring, s - 1 - ring, s - 1 - ring), fill=255)
            self._cam_key_rgb = hex_rgb(key)
            self._cam_bg, self._cam_mask = bg, mask
            placeholder = bg.copy()
            placeholder.paste(Image.new("RGB", (s, s), "#1F2328"), (0, 0), mask)
            dp = ImageDraw.Draw(placeholder)
            msg = "Starting camera…"
            left, top, right, bottom = dp.textbbox((0, 0), msg)
            dp.text(((s - (right - left)) / 2, (s - (bottom - top)) / 2), msg, fill="#D0D7DE")
            self._cam_photo = ImageTk.PhotoImage(placeholder)
            lbl = tk.Label(win, image=self._cam_photo, bd=0, highlightthickness=0, bg=key, cursor="fleur")
            lbl.pack()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            pos = self._rec_opts.get("cam_pos")
            x, y = (int(pos[0]), int(pos[1])) if pos else (sw - s - 40, sh - s - 90)
            x, y = min(max(0, x), max(0, sw - s)), min(max(0, y), max(0, sh - s))
            win.geometry(f"{s}x{s}+{x}+{y}")
            drag = {}

            def press(e):
                drag["x"], drag["y"] = e.x_root - win.winfo_x(), e.y_root - win.winfo_y()

            def move(e):
                if drag:
                    win.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

            def release(e):
                if drag:  # use the mouse position: the window may not have moved on screen yet
                    self._rec_opts["cam_pos"] = [e.x_root - drag["x"], e.y_root - drag["y"]]
                    self._rec_save_opts()
            lbl.bind("<ButtonPress-1>", press)
            lbl.bind("<B1-Motion>", move)
            lbl.bind("<ButtonRelease-1>", release)
            self._cam_win, self._cam_lbl = win, lbl

        def _cam_is_cutout(self) -> bool:
            return str(self._rec_opts.get("cam_style", "")).startswith("Cutout")

        def _cam_start_feed(self):
            transform = None
            if self._cam_is_cutout():
                try:
                    seg = Segmenter(resource_path(SEG_MODEL_FILE), self._cam_key_rgb)
                    size = self._cam_size
                    transform = lambda frame: seg.cutout(frame, size)  # noqa: E731
                except Exception as e:
                    self._log(f"Background removal isn't available ({type(e).__name__}: {e}). "
                              "Showing the round camera instead.", "warn")
                    self._rec_opts["cam_style"] = "Bubble"
                    self._rec_save_opts()
                    messagebox.showwarning(APP_NAME, "Background removal isn't available in this copy of the app, "
                                                     "so the camera shows as a round bubble instead.",
                                           parent=self._rec_bar if self._bar_alive() else self)
                    self._cam_close()
                    self._cam_open()
                    return
            self._cam_feed = CameraFeed(self.tools.get("ffmpeg"), self._cam_input_args(), self._cam_size,
                                        transform=transform)
            try:
                self._cam_feed.start()
            except Exception as e:
                self._cam_fail("Couldn't start the camera.", str(e))
                return
            self._cam_seen = 0
            self.after(40, self._cam_poll)

        def _cam_poll(self):
            feed = self._cam_feed
            if self._cam_win is None or feed is None:
                return
            frame, count = feed.frame, feed.frames
            if frame is not None and count != self._cam_seen:
                self._cam_seen = count
                try:
                    img = Image.frombytes("RGB", (self._cam_size, self._cam_size), frame)
                    if not self._cam_is_cutout():  # cut-out frames come ready-made from the camera thread
                        out = self._cam_bg.copy()
                        out.paste(img, (0, 0), self._cam_mask)
                        img = out
                    self._cam_photo.paste(img)
                except (ValueError, tk.TclError):
                    pass
            elif not feed.running:
                self._cam_fail(camera_error_text(feed.error, had_frames=count > 0), feed.error)
                return
            self.after(40, self._cam_poll)

        def _cam_close(self, wait=False):
            feed, self._cam_feed = self._cam_feed, None
            if feed is not None:
                feed.stop(wait=wait)
            win, self._cam_win = self._cam_win, None
            if win is not None:
                try:
                    win.destroy()
                except tk.TclError:
                    pass

        def _cam_restart(self):
            if self._cam_win is not None:
                self._cam_close()
                self.after(500, self._cam_open)  # give Windows a moment to release the camera

        def _cam_fail(self, message, detail=""):
            tail = (detail or "").strip()[-200:]
            self._log(message + (f"  [{tail}]" if tail else ""), "warn")
            self._cam_close()
            self._rec_opts["camera"] = False
            self._rec_save_opts()
            self._rec_style_toggle("camera")
            messagebox.showwarning(APP_NAME, message, parent=self._rec_bar if self._bar_alive() else self)

        def _rec_done(self, res: Result, details: str, warnings: list | None = None):
            self._rec_cleanup_ui()
            self._done(res, details=details)
            if warnings:
                self.res_badge.configure(text="⚠ " + "\n⚠ ".join(w[:160] for w in warnings), fg_color=AMBER_BG,
                                         text_color=AMBER_TEXT)
                for w in warnings:
                    self._log(w, "warn")
            else:
                self.res_badge.configure(text=" ✓ Sound and picture saved together ", fg_color="#DAFBE1",
                                         text_color=GREEN)

        def _rec_failed(self, msg, details=""):
            if self.recorder:
                try:
                    self.recorder.abort()
                except Exception:
                    pass
            self._rec_cleanup_ui()
            self._log(msg, "err")
            self._show_error("Recording failed", msg, details or msg)

        # ------------------------------------------------------------- closing
        def destroy(self):
            try:
                self._cam_close(wait=True)  # never leave the webcam light on
                self._mic_meter_stop()
            except Exception:
                pass
            super().destroy()

        def _on_close(self):
            if self._rec_state in ("countdown", "recording", "saving"):
                if self._rec_state == "saving" or messagebox.askyesno(
                        APP_NAME, "A screen recording is running. Stop it, save it, and then quit?", parent=self):
                    self._close_after_rec = True
                    if self._rec_state != "saving":
                        self._rec_stop()
                return
            self.destroy()


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--version" in sys.argv:
        print(f"{APP_NAME} {APP_VERSION}")
        return
    if ctk is None:
        print("customtkinter is not installed. Run:  pip install customtkinter pillow soundcard segno")
        sys.exit(1)
    if IS_WIN:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # own taskbar icon
        except Exception:
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
