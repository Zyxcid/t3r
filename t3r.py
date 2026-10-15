"""
t3r — a tiny 20-20-20 rule eye break timer.

Every `work_minutes` minutes it plays a soft chime and shows a minimal
fullscreen break overlay — a big countdown number, one dim hint line
and a hairline progress bar — to encourage relaxed eye rest.

v1.3 changes:
  * Minimal break overlay: the breathing-circle animation, countdown
    ring, rotating tips and duplicate skip controls are gone. The break
    is now a big countdown number, one hint line and a hairline progress
    bar, driven by a single change-gated 250 ms tick. No animation loop,
    no canvas redraws — near-zero CPU while the break runs.
  * No more desktop notifications: the pre-break chime (+ taskbar
    flash) and the break-done chime are cue enough. All notification
    plumbing (pystray balloon, osascript, notify-send) is gone —
    less code, fewer moving parts.
  * Chime volume setting (0–100 %) with a live "Test" preview button in
    the settings dialog; validated/clamped like every other config key.
  * Main-window visual polish: Consolas for all digits (time, stats,
    spinboxes), a filled primary Pause button with a ghost Reset,
    subtler progress track, optically inset status dot, capped stat
    labels, friendlier window title.
  * Even lower idle CPU: the main-window progress bar re-places itself
    only when its width actually changes.

v1.1.0 improvements (kept):
  * Drift-free timer engine: work timer and break overlay count down
    against time.monotonic() deadlines instead of decrementing a counter
    once per `after()` callback — no cumulative drift, and system
    sleep/suspend is handled correctly.
  * Single-instance guard: launching a second copy just pops the first
    copy's window (loopback-socket handshake, no firewall prompts).
  * Per-monitor DPI awareness on Windows: crisp text/UI on scaled
    displays, with the layout proportionally rescaled.
  * Dropped the `plyer` dependency — smaller bundle, fewer antivirus
    heuristic triggers (and since v1.3 there are no notifications at
    all for it to power).
  * Config validation/clamping, safe spinbox parsing, FlashWindowEx on
    the real top-level HWND.
  * Settings apply immediately.

Carried over from earlier versions:
  * Windows-only modules (winreg / winsound / ctypes.windll) are gated
    so the file imports cleanly on Linux / macOS too (graceful no-ops).
  * Persistent JSON config in the user's per-platform config dir.
  * Settings dialog reachable from both the tray menu and a gear button.
  * `--minimized` CLI flag and a default-on `start_minimized` setting.
"""

from __future__ import annotations

import io
import json
import math
import os
import socket
import struct
import sys
import threading
import time
import traceback
import tkinter as tk
from argparse import ArgumentParser
from pathlib import Path

from PIL import Image, ImageDraw

try:
    import pystray
except Exception:
    # pystray is optional at runtime: if it is missing or cannot reach a
    # display (e.g. headless Linux, where the Xorg backend connects to X
    # at import time), the app still runs with its main window and the
    # tray features are simply absent.
    pystray = None

# ---------------------------------------------------------------------------
# Platform detection — Windows-only stdlib modules are imported optionally.
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"

if _IS_WINDOWS:
    import winreg
    import ctypes
    import winsound
else:  # pragma: no cover — non-Windows fallbacks
    winreg = None
    ctypes = None
    winsound = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "t3r"
VERSION = "1.3"

# Design-space window size (scaled by the DPI factor at runtime).
BASE_W = 360
BASE_H = 340
STATS_EXTRA_H = 50

# Loopback ports used by the single-instance handshake.
_SINGLE_INSTANCE_PORTS = tuple(range(53127, 53138))

# Color palette (dark, low-glare)
BG_DARK = "#0A0A0F"
BG_CARD = "#13131C"
ACCENT_BLUE = "#4A9EFF"
ACCENT_BLUE_HOVER = "#6CB3FF"
ACCENT_GREEN = "#3DD68C"
ACCENT_AMBER = "#FFB347"
TEXT_PRIMARY = "#F0F0F5"
TEXT_MUTED = "#5A5A72"
TEXT_ON_ACCENT = "#0B0F16"  # dark text on filled accent buttons
BORDER = "#22222E"
PROG_TRACK = "#191922"      # progress bar track (quieter than BORDER)

# Digits/monospace face. Consolas ships with every Windows since Vista
# and is far cleaner than Courier New; non-Windows falls back to Tk's
# default monospace mapping.
MONO_FONT = "Consolas"

# UI scale factor (1.0 = 96 DPI). Set once in main() before any widget
# is constructed; pixel-based sizes below read it at construction time.
_UI_SCALE = 1.0


def _px(v: float) -> int:
    """Scale a design pixel value by the current UI scale factor."""
    return max(1, round(v * _UI_SCALE))


def _pick_mono_font(root) -> str:
    """Pin the digit font to Consolas where it exists (every normal
    Windows install); fall back to Courier New elsewhere so the big
    countdown keeps its intended size instead of Tk's tiny default."""
    global MONO_FONT
    try:
        import tkinter.font as tkfont
        if "Consolas" not in set(tkfont.families(root)):
            MONO_FONT = "Courier New"
    except Exception:
        pass
    return MONO_FONT


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def _config_dir() -> Path:
    """Cross-platform config directory: %APPDATA%/t3r on Windows,
    ~/Library/Application Support/t3r on macOS, ~/.config/t3r elsewhere."""
    if _IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home())
    elif _IS_MACOS:
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


CONFIG_PATH = _config_dir() / "config.json"

DEFAULT_CONFIG = {
    "work_minutes": 20,
    "pre_break_secs": 5,
    "break_seconds": 20,
    "sound_enabled": True,
    "sound_volume": 20,  # chime volume, percent (0-100)
    "autostart": False,
    "start_minimized": True,  # the user wants the app to boot into the tray
}


class Config:
    """Tiny dict-like wrapper that loads/saves JSON from disk.

    Values loaded from disk are validated/clamped so a corrupt or
    hand-edited config can never crash the app or the settings dialog.
    """

    def __init__(self):
        self._data = dict(DEFAULT_CONFIG)
        self._load()
        self._validate()

    def _load(self):
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    # Whitelist merge — unknown keys are dropped.
                    for k, v in loaded.items():
                        if k in DEFAULT_CONFIG:
                            self._data[k] = v
        except Exception:
            pass  # corrupt or unreadable — keep defaults

    def _validate(self):
        def clamp_int(key, mn, mx):
            try:
                v = int(self._data.get(key, DEFAULT_CONFIG[key]))
            except (TypeError, ValueError):
                v = DEFAULT_CONFIG[key]
            self._data[key] = max(mn, min(mx, v))

        clamp_int("work_minutes", 1, 120)
        clamp_int("pre_break_secs", 1, 30)
        clamp_int("break_seconds", 5, 90)
        clamp_int("sound_volume", 0, 100)
        self._data["sound_enabled"] = bool(self._data.get("sound_enabled", True))
        self._data["autostart"] = bool(self._data.get("autostart", False))
        self._data["start_minimized"] = bool(self._data.get("start_minimized", True))

    def save(self):
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except Exception:
            pass

    def __getitem__(self, k):
        return self._data[k]

    def __setitem__(self, k, v):
        self._data[k] = v

    def __contains__(self, k):
        return k in self._data

    def get(self, k, default=None):
        return self._data.get(k, default)


# ---------------------------------------------------------------------------
# Sound — synth a soft sine chime and play it (Windows-only, gracefully
# silent elsewhere).
# ---------------------------------------------------------------------------

def _generate_chime(freq: int = 440, duration_ms: int = 300, volume: float = 0.2) -> io.BytesIO:
    """Build an in-memory WAV (mono 16-bit) of a softly enveloped sine."""
    sample_rate = 44100
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    samples = bytearray()
    for i in range(num_samples):
        t = float(i) / sample_rate
        # Attack-decay envelope so it doesn't click
        envelope = min(1.0, t * 20, (duration_ms / 1000.0 - t) * 20)
        envelope = max(0.0, envelope)
        value = volume * envelope * math.sin(2.0 * math.pi * freq * t)
        samples.extend(struct.pack("<h", int(value * 32767)))

    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + len(samples)))
    buf.write(b"WAVEfmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16))
    buf.write(b"data")
    buf.write(struct.pack("<I", len(samples)))
    buf.write(bytes(samples))
    buf.seek(0)
    return buf


def _play_wav(buf: io.BytesIO):
    """Play an in-memory WAV. No-op on non-Windows."""
    if winsound is None:
        return
    data = buf.read()
    if data:
        try:
            winsound.PlaySound(data, winsound.SND_MEMORY)
        except Exception:
            pass


def _chime_volume(cfg: Config) -> float:
    """Configured chime volume as 0.0..1.0 (0 = effectively silent)."""
    try:
        pct = int(cfg.get("sound_volume", DEFAULT_CONFIG["sound_volume"]))
    except (TypeError, ValueError):
        pct = DEFAULT_CONFIG["sound_volume"]
    return max(0.0, min(1.0, pct / 100.0))


def _play_chime(cfg: Config, freq: int, duration_ms: int):
    """Synthesize + play one chime on a worker thread, honoring the
    sound_enabled and sound_volume settings."""
    if not cfg.get("sound_enabled", True):
        return
    vol = _chime_volume(cfg)
    if vol <= 0.0:
        return
    buf = _generate_chime(freq=freq, duration_ms=duration_ms, volume=vol)
    threading.Thread(target=_play_wav, args=(buf,), daemon=True).start()


def play_pre_break_sound(cfg: Config):
    _play_chime(cfg, freq=520, duration_ms=200)


def play_break_done_sound(cfg: Config):
    _play_chime(cfg, freq=800, duration_ms=400)


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------

def create_icon_image(size: int = 64) -> Image.Image:
    """A polished eye-themed icon: green iris, dark pupil, white reflection.

    Geometry is proportional to `size`, so the same routine renders the
    64 px tray icon and the 256 px master used to generate icon.ico.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = size / 2.0

    def circle(r_frac, dx=0.0, dy=0.0, fill=None):
        r = size * r_frac / 2.0
        x = cx + size * dx
        y = cy + size * dy
        draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)

    circle(0.875, fill=(15, 61, 34, 255))          # outer dark green halo
    circle(0.75, fill=(61, 214, 140, 255))         # bright green iris
    circle(0.4375, fill=(40, 150, 95, 255))        # inner ring (depth)
    circle(0.25, fill=(10, 26, 16, 255))           # pupil
    circle(0.078125, dx=-0.0234375, dy=-0.0234375,
           fill=(240, 240, 245, 255))              # big reflection
    circle(0.046875, dx=0.0546875, dy=0.0546875,
           fill=(240, 240, 245, 180))              # tiny reflection
    return img


# ---------------------------------------------------------------------------
# Windows autostart via HKEY_CURRENT_USER\Software\Microsoft\Windows\
# CurrentVersion\Run
# ---------------------------------------------------------------------------

def _autostart_command() -> str:
    """Build the command line stored in the registry for autostart."""
    if getattr(sys, "frozen", False):
        # PyInstaller/Nuitka bundle — just run the .exe with --minimized
        return f'"{sys.executable}" --minimized'

    # Running as a plain .py — invoke pythonw.exe on Windows (no console)
    script = os.path.abspath(sys.argv[0])
    python_exe = sys.executable
    if _IS_WINDOWS and python_exe.lower().endswith("python.exe"):
        pythonw = python_exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(pythonw):
            python_exe = pythonw
    return f'"{python_exe}" "{script}" --minimized'


def is_autostart_enabled() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_READ,
        )
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def enable_autostart() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _autostart_command())
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def disable_autostart() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        winreg.DeleteValue(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    if enabled:
        return enable_autostart()
    return disable_autostart()


# ---------------------------------------------------------------------------
# Windows taskbar flash (FlashWindowEx on the real top-level HWND)
# ---------------------------------------------------------------------------

def toplevel_hwnd(widget) -> int:
    """Return the top-level Win32 HWND for a Tk widget (0 if unavailable).

    widget.winfo_id() returns the Tk *inner* window; the taskbar owns the
    wrapper window, which is its parent.
    """
    if not _IS_WINDOWS or ctypes is None:
        return 0
    try:
        widget.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
        return int(hwnd) if hwnd else int(widget.winfo_id())
    except Exception:
        return 0


def flash_taskbar(hwnd: int):
    """Flash the taskbar icon a few times to attract attention."""
    if not _IS_WINDOWS or ctypes is None or not hwnd:
        return

    FLASHW_ALL = 0x3  # FLASHW_CAPTION | FLASHW_TRAY

    try:
        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_ulong),
                ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_ulong),
            ]

        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_ALL, 3, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        try:  # ancient fallback
            ctypes.windll.user32.FlashWindow(hwnd, True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Per-monitor DPI awareness (Windows) — crisp text on scaled displays.
# Must be called BEFORE the Tk root is created.
# ---------------------------------------------------------------------------

def enable_windows_dpi_awareness():
    if not _IS_WINDOWS or ctypes is None:
        return
    try:
        # 2 == PROCESS_PER_MONITOR_DPI_AWARE (Win 8.1+ via shcore)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Single-instance guard.
#
# The first instance binds a loopback TCP port and listens. A second
# instance fails to bind, sends "show" to the first (which pops its
# window) and exits. Loopback sockets never trigger firewall prompts.
# ---------------------------------------------------------------------------

def acquire_single_instance() -> "socket.socket | None":
    """Return a bound listener socket if we are the first instance.

    Returns None if another t3r answered on one of our ports (the caller
    should exit — the running copy just popped its window). If all ports
    are taken by unrelated apps, returns a best-effort unguarded run.
    """
    for port in _SINGLE_INSTANCE_PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            s.listen(1)
            return s
        except OSError:
            s.close()
            if notify_existing_instance(port):
                return None
    return None


def notify_existing_instance(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as c:
            c.sendall(b"show\n")
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Small Tk helper — avoid needless redraws from the periodic tick.
# ---------------------------------------------------------------------------

def config_if_changed(widget, **kw):
    """widget.config() but only for options whose value actually changed."""
    for k, v in kw.items():
        try:
            cur = widget.cget(k)
        except Exception:
            cur = None
        if cur != v:
            widget.config(**{k: v})


# ---------------------------------------------------------------------------
# UI helper: HoverButton
# ---------------------------------------------------------------------------

class HoverButton(tk.Frame):
    """Flat tk.Button substitute with smooth hover background.

    `color` is the resting text color and `hover_fg` the text color
    while hovered — pass a dark pair on filled accent buttons so the
    label stays readable on the bright hover background.
    """

    def __init__(self, parent, text: str, cmd, color: str,
                 bg: str = BG_CARD, hover_bg: str = BORDER,
                 hover_fg: str = TEXT_PRIMARY, **kwargs):
        super().__init__(parent, bg=bg, **kwargs)
        self._color = color
        self._bg = bg
        self._hover_bg = hover_bg
        self._hover_fg = hover_fg
        self._cmd = cmd
        self.label = tk.Label(
            self, text=text, bg=bg, fg=color, relief="flat",
            font=("Segoe UI", 10, "bold"), cursor="hand2",
            padx=_px(14), pady=_px(7),
            highlightthickness=1, highlightbackground=BORDER,
        )
        self.label.pack()
        self.label.bind("<Button-1>", self._on_click)
        self.label.bind("<Enter>", self._on_enter)
        self.label.bind("<Leave>", self._on_leave)

    def _on_click(self, _e):
        if callable(self._cmd):
            self._cmd()

    def _on_enter(self, _e):
        self.label.config(bg=self._hover_bg, fg=self._hover_fg)

    def _on_leave(self, _e):
        self.label.config(bg=self._bg, fg=self._color)

    def config(self, **kwargs):  # minimal compatible API
        if "text" in kwargs:
            self.label.config(text=kwargs.pop("text"))
        if "fg" in kwargs:
            self._color = kwargs.pop("fg")
            self.label.config(fg=self._color)
        super().config(**kwargs)


# ---------------------------------------------------------------------------
# Break overlay — fullscreen but minimal: a big countdown number, one dim
# hint line and a hairline progress bar along the bottom edge. Everything
# is driven by a single change-gated 250 ms tick against a
# time.monotonic() deadline (drift-free, near-zero CPU — no animation
# loop, no canvas redraws).
# ---------------------------------------------------------------------------

class BreakOverlay:
    OVERLAY_BG = "#05070A"  # near-black, low glare
    TEXT_MAIN = "#E8ECF2"
    TEXT_DIM = "#6A7080"
    PROG_FG = "#3DD68C"
    PROG_BG = "#10151A"

    HINT = "Look at something 20 feet away   ·   Esc to skip"

    def __init__(self, parent_root, seconds: int, on_done):
        self.seconds = seconds
        self.remaining = seconds
        self.on_done = on_done
        self._running = True
        # Drift-free: count down against a monotonic deadline.
        self._deadline = time.monotonic() + seconds
        self._last_num: int | None = None
        self._last_frac: float | None = None

        self.win = tk.Toplevel(parent_root)
        self.win.configure(bg=self.OVERLAY_BG)
        self.win.attributes("-topmost", True)
        self.win.attributes("-fullscreen", True)
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)
        self.win.bind("<Escape>", lambda e: self._skip())
        self.win.bind("<space>", lambda e: self._skip())
        self.win.focus_force()

        self._build()
        self._tick()

    def _build(self):
        w = self.win

        center = tk.Frame(w, bg=self.OVERLAY_BG)
        center.place(relx=0.5, rely=0.5, anchor="center")

        # The one big element: the remaining seconds.
        self.num_label = tk.Label(
            center, text=str(self.remaining), bg=self.OVERLAY_BG,
            fg=self.TEXT_MAIN, font=("Courier New", 72, "bold"),
        )
        self.num_label.pack()

        tk.Label(
            center, text=self.HINT, bg=self.OVERLAY_BG, fg=self.TEXT_DIM,
            font=("Segoe UI", 11),
        ).pack(pady=(_px(20), 0))

        # Hairline progress bar flush with the bottom edge of the screen.
        bar = tk.Frame(w, bg=self.PROG_BG, height=_px(3))
        bar.place(relx=0.0, rely=1.0, anchor="sw", relwidth=1.0)
        self.prog_fill = tk.Frame(bar, bg=self.PROG_FG, height=_px(3))
        self.prog_fill.place(x=0, y=0, relheight=1, relwidth=1.0)

    def _tick(self):
        if not self._running:
            return
        self.remaining = max(0, int(math.ceil(self._deadline - time.monotonic())))

        # Repaint only when the displayed second actually changed.
        if self.remaining != self._last_num:
            self._last_num = self.remaining
            self.num_label.config(text=str(self.remaining))

        frac = (self.remaining / self.seconds) if self.seconds > 0 else 0.0
        if frac != self._last_frac:
            self._last_frac = frac
            self.prog_fill.place(x=0, y=0, relheight=1, relwidth=frac)

        if self.remaining <= 0:
            self._finish()
            return
        self.win.after(250, self._tick)

    def _finish(self):
        self._running = False
        self.win.destroy()
        self.on_done(skipped=False)

    def _skip(self):
        self._running = False
        self.win.destroy()
        self.on_done(skipped=True)


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog:
    def __init__(self, parent, cfg: Config, on_save, on_close=None):
        self.cfg = cfg
        self.on_save = on_save
        self.on_close = on_close
        self._closed = False

        self.win = tk.Toplevel(parent)
        self.win.title(f"{APP_NAME} — Settings")
        self.win.geometry(f"{_px(380)}x{_px(520)}")
        self.win.resizable(False, False)
        self.win.configure(bg=BG_DARK)
        self.win.attributes("-topmost", True)
        self.win.transient(parent)
        self.win.grab_set()
        self.win.focus_force()
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)

        self._work_min = tk.IntVar(value=int(cfg["work_minutes"]))
        self._pre_break = tk.IntVar(value=int(cfg["pre_break_secs"]))
        self._break_secs = tk.IntVar(value=int(cfg["break_seconds"]))
        self._sound = tk.BooleanVar(value=bool(cfg["sound_enabled"]))
        self._volume = tk.IntVar(value=int(cfg.get("sound_volume", DEFAULT_CONFIG["sound_volume"])))
        self._autostart = tk.BooleanVar(value=is_autostart_enabled())
        self._start_min = tk.BooleanVar(value=bool(cfg["start_minimized"]))

        self._build()

    def _build(self):
        w = self.win
        card = tk.Frame(w, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True, padx=_px(20), pady=_px(20))

        tk.Label(
            card, text="Settings", bg=BG_CARD, fg=TEXT_PRIMARY,
            font=("Segoe UI", 14, "bold"),
        ).pack(pady=(18, 4))
        tk.Label(
            card, text="Changes apply immediately", bg=BG_CARD, fg=TEXT_MUTED,
            font=("Segoe UI", 8),
        ).pack(pady=(0, 14))

        self._row(card, "Work duration (min)", self._work_min, 1, 120)
        self._row(card, "Pre-break warning (sec)", self._pre_break, 1, 30)
        self._row(card, "Break duration (sec)", self._break_secs, 5, 90)

        self._check(card, "Enable chime sounds", self._sound)
        self._volume_row(card)
        self._check(card, "Start on login", self._autostart)
        self._check(card, "Start minimized to tray", self._start_min)

        bf = tk.Frame(card, bg=BG_CARD)
        bf.pack(pady=(18, 8))
        HoverButton(bf, "Save", self._save, ACCENT_GREEN).pack(side="left", padx=4)
        HoverButton(bf, "Cancel", self._cancel, TEXT_MUTED).pack(side="left", padx=4)

    def _row(self, parent, label, var, mn, mx):
        f = tk.Frame(parent, bg=BG_CARD)
        f.pack(fill="x", padx=28, pady=5)
        tk.Label(
            f, text=label, bg=BG_CARD, fg=TEXT_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left", anchor="w")
        # NOTE: buttonbackground / buttonforeground are X11-only Spinbox
        # options and raise TclError on Windows Tk 9 / macOS Aqua.
        sp = tk.Spinbox(
            f, from_=mn, to=mx, textvariable=var, width=6, justify="right",
            bg=BG_DARK, fg=TEXT_PRIMARY, font=(MONO_FONT, 11, "bold"),
            relief="flat", highlightthickness=1, highlightbackground=BORDER,
            insertbackground=TEXT_PRIMARY,
            disabledbackground=BG_DARK, disabledforeground=TEXT_MUTED,
            readonlybackground=BG_DARK,
        )
        sp.pack(side="right", anchor="e")

    def _check(self, parent, label, var):
        f = tk.Frame(parent, bg=BG_CARD)
        f.pack(fill="x", padx=28, pady=3)
        cb = tk.Checkbutton(
            f, text=label, variable=var, bg=BG_CARD, fg=TEXT_PRIMARY,
            selectcolor=BG_DARK, activebackground=BG_CARD, activeforeground=TEXT_PRIMARY,
            font=("Segoe UI", 10), highlightthickness=0, bd=0,
        )
        cb.pack(side="left", anchor="w")

    def _volume_row(self, parent):
        """Chime volume slider + live % readout + Test preview button."""
        f = tk.Frame(parent, bg=BG_CARD)
        f.pack(fill="x", padx=28, pady=(6, 0))
        tk.Label(
            f, text="Chime volume", bg=BG_CARD, fg=TEXT_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")
        self._vol_pct = tk.StringVar(value=f"{self._safe_int(self._volume, 20)}%")
        tk.Label(
            f, textvariable=self._vol_pct, bg=BG_CARD, fg=TEXT_PRIMARY,
            font=(MONO_FONT, 10, "bold"),
        ).pack(side="right")

        # NOTE: classic tk.Scale (not ttk) so it can be recolored to match
        # the dark theme without pulling in ttk style machinery.
        tk.Scale(
            parent, from_=0, to=100, orient="horizontal",
            variable=self._volume, showvalue=False, bd=0,
            bg=BG_CARD, fg=TEXT_MUTED, troughcolor=BG_DARK,
            highlightthickness=0, activebackground=ACCENT_GREEN,
            sliderlength=_px(16), length=_px(280),
        ).pack(fill="x", padx=28, pady=(0, 2))
        self._volume.trace_add(
            "write", lambda *_: self._vol_pct.set(f"{self._volume.get()}%")
        )

        HoverButton(parent, "Test sound", self._test_sound, TEXT_MUTED).pack(
            anchor="w", padx=28, pady=(2, 0)
        )

    def _test_sound(self):
        """Preview the break-done chime at the slider's current volume.
        Silent no-op on non-Windows (winsound is unavailable there)."""
        if not self._sound.get():
            return
        vol = max(0.0, min(1.0, self._safe_int(self._volume, 20) / 100.0))
        if vol <= 0.0:
            return
        threading.Thread(
            target=_play_wav,
            args=(_generate_chime(freq=800, duration_ms=400, volume=vol),),
            daemon=True,
        ).start()

    @staticmethod
    def _safe_int(var, default: int) -> int:
        """Read an IntVar, tolerating empty/garbage spinbox text."""
        try:
            return int(var.get())
        except (tk.TclError, TypeError, ValueError):
            return default

    def _save(self):
        if self._closed:
            return
        self._closed = True

        self.cfg["work_minutes"] = max(1, min(120, self._safe_int(self._work_min, int(self.cfg["work_minutes"]))))
        self.cfg["pre_break_secs"] = max(1, min(30, self._safe_int(self._pre_break, int(self.cfg["pre_break_secs"]))))
        self.cfg["break_seconds"] = max(5, min(90, self._safe_int(self._break_secs, int(self.cfg["break_seconds"]))))
        self.cfg["sound_enabled"] = bool(self._sound.get())
        self.cfg["sound_volume"] = max(
            0, min(100, self._safe_int(self._volume, int(self.cfg.get("sound_volume", 20))))
        )
        self.cfg["start_minimized"] = bool(self._start_min.get())

        # Sync registry with checkbox
        want_autostart = bool(self._autostart.get())
        if want_autostart != is_autostart_enabled():
            set_autostart(want_autostart)
        self.cfg["autostart"] = is_autostart_enabled()

        self.cfg.save()
        if callable(self.on_save):
            self.on_save(self.cfg)
        if callable(self.on_close):
            self.on_close()
        self.win.destroy()

    def _cancel(self):
        if self._closed:
            return
        self._closed = True
        if callable(self.on_close):
            self.on_close()
        self.win.destroy()


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class Timer2020App:
    def __init__(self, root: tk.Tk, cfg: Config):
        self.root = root
        self.cfg = cfg

        # Runtime timer durations (kept in sync with cfg)
        self.work_secs = int(cfg["work_minutes"]) * 60
        self.pre_break_secs = int(cfg["pre_break_secs"])
        self.break_seconds = int(cfg["break_seconds"])

        self.root.title(f"{APP_NAME} — 20-20-20 eye timer")
        _pick_mono_font(root)
        self.root.geometry(self._geo())
        self.root.resizable(False, False)
        self.root.configure(bg=BG_DARK)
        self.root.attributes("-topmost", True)

        # Drift-free timer state: WORK / PRE_BREAK count down against
        # a time.monotonic() deadline instead of decrementing a counter.
        self.state = "WORK"
        self._deadline = time.monotonic() + self.work_secs
        self.remaining = self.work_secs
        self.paused_state = None
        self.paused_rem = 0
        self._running = True
        self.session_count = 0
        self._focus_minutes = 0
        self._overlay_open = False
        self._window_hidden = False
        self._stats_visible = False
        self._tray = None
        self._settings_open = False
        self._last_tray_title = None
        self._last_prog_rw = None
        self._peer_sock = None

        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        self._build_ui()
        self._setup_icon_and_tray()
        self._tick()

    # ---- Layout helpers ----

    def _geo(self, extra_h: int = 0) -> str:
        """Window geometry string for the design size (+ extra px), scaled."""
        return f"{_px(BASE_W)}x{_px(BASE_H + extra_h)}"

    # ---- Tray / window plumbing ----

    def _setup_icon_and_tray(self):
        icon_img = create_icon_image(64)
        buf = io.BytesIO()
        icon_img.save(buf, format="PNG")
        self._tk_icon = tk.PhotoImage(data=buf.getvalue())
        self.root.iconphoto(True, self._tk_icon)

        if pystray is None:
            self._tray = None
            return

        menu = pystray.Menu(
            pystray.MenuItem("Show Timer", self._do_show, default=True),
            pystray.MenuItem("Pause / Resume", self._pause_resume),
            pystray.MenuItem("Reset", self._reset),
            pystray.MenuItem("Settings", self._open_settings_tray),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start on Login", self._toggle_autostart_tray,
                checked=lambda item: is_autostart_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._quit_app),
        )

        self._tray = pystray.Icon(APP_NAME, icon_img, APP_NAME, menu=menu)

        def run_tray():
            try:
                self._tray.run()
            except Exception:
                # No tray available on this desktop — the app keeps
                # running with its main window instead.
                pass

        threading.Thread(target=run_tray, daemon=True).start()

    def start_peer_listener(self, sock: socket.socket):
        """Watch the single-instance socket; 'show' pops our window."""
        self._peer_sock = sock

        def listen_loop():
            while True:
                try:
                    conn, _ = sock.accept()
                except OSError:
                    return  # socket closed — quitting
                with conn:
                    try:
                        data = conn.recv(64)
                        if data.startswith(b"show"):
                            self.root.after(0, self._show_window)
                    except OSError:
                        pass

        threading.Thread(target=listen_loop, daemon=True).start()

    def _hide_to_tray(self):
        self._window_hidden = True
        self.root.withdraw()

    def _do_show(self, icon=None, item=None):
        self._window_hidden = False
        self.root.after(0, self._show_window)

    def _show_window(self):
        self._window_hidden = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _pause_resume(self, icon=None, item=None):
        self.root.after(0, self._toggle_pause)

    def _reset(self, icon=None, item=None):
        self.root.after(0, self._do_reset)

    def _open_settings_tray(self, icon=None, item=None):
        self.root.after(0, self._open_settings)

    def _toggle_autostart_tray(self, icon=None, item=None):
        set_autostart(not is_autostart_enabled())
        self.cfg["autostart"] = is_autostart_enabled()
        self.cfg.save()

    def _quit_app(self, icon=None, item=None):
        self.root.after(0, self._do_quit)

    def _do_quit(self):
        self._running = False
        if self._peer_sock:
            try:
                self._peer_sock.close()
            except OSError:
                pass
        if self._tray:
            try:
                self._tray.stop()
            except Exception:
                pass
        self.root.destroy()

    def _update_tray_tip(self):
        if not self._tray:
            return
        if self.state == "WORK":
            mins, secs = divmod(self.remaining, 60)
            tip = f"{APP_NAME} | Focus {mins:02d}:{secs:02d}"
        elif self.state == "PRE_BREAK":
            tip = f"{APP_NAME} | Break in {self.remaining}s"
        elif self.state == "BREAK_OVERLAY":
            tip = f"{APP_NAME} | Eye break"
        else:
            tip = f"{APP_NAME} | Paused"
        if tip != self._last_tray_title:
            self._last_tray_title = tip
            try:
                self._tray.title = tip
            except Exception:
                pass

    # ---- UI ----

    def _build_ui(self):
        r = self.root

        card = tk.Frame(r, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True, padx=_px(20), pady=_px(20))

        # Header: status dot + state label + spacer + settings gear
        header = tk.Frame(card, bg=BG_CARD)
        header.pack(fill="x", padx=_px(28), pady=(_px(22), 0))

        dot_wrap = tk.Frame(header, bg=BG_CARD, width=_px(12), height=_px(12))
        dot_wrap.pack(side="left")
        self.status_dot = tk.Canvas(
            dot_wrap, width=_px(12), height=_px(12),
            bg=BG_CARD, highlightthickness=0,
        )
        self.status_dot.pack()
        self._dot_circle = self.status_dot.create_oval(
            2, 2, _px(12) - 2, _px(12) - 2, fill=ACCENT_BLUE, outline="",
        )

        self.state_label = tk.Label(
            header, text="FOCUSING", bg=BG_CARD, fg=TEXT_MUTED,
            font=("Segoe UI", 9, "bold"),
        )
        self.state_label.pack(side="left", padx=(7, 0))

        # Settings gear (top-right)
        gear = tk.Label(
            header, text="⚙", bg=BG_CARD, fg=TEXT_MUTED,
            font=("Segoe UI", 13), cursor="hand2",
        )
        gear.pack(side="right")
        gear.bind("<Button-1>", lambda e: self._open_settings())
        gear.bind("<Enter>", lambda e: gear.config(fg=TEXT_PRIMARY))
        gear.bind("<Leave>", lambda e: gear.config(fg=TEXT_MUTED))

        # Big time display
        self.time_label = tk.Label(
            card, text="20:00", bg=BG_CARD, fg=TEXT_PRIMARY,
            font=(MONO_FONT, 54, "bold"),
        )
        self.time_label.pack(pady=(_px(10), _px(16)))

        # Progress bar
        bar_h = max(3, _px(5))
        self.prog_bg = tk.Frame(card, bg=PROG_TRACK, height=bar_h)
        self.prog_bg.pack(fill="x", padx=_px(28), pady=(0, _px(20)))
        self.prog_fill = tk.Frame(self.prog_bg, bg=ACCENT_BLUE, height=bar_h)
        self.prog_fill.place(x=0, y=0, relheight=1, relwidth=0)

        # Buttons — filled primary + ghost secondary
        bf = tk.Frame(card, bg=BG_CARD)
        bf.pack(pady=(0, _px(14)))
        self.pause_btn = HoverButton(
            bf, "⏸  Pause", self._toggle_pause, TEXT_ON_ACCENT,
            bg=ACCENT_BLUE, hover_bg=ACCENT_BLUE_HOVER, hover_fg=TEXT_ON_ACCENT,
        )
        self.pause_btn.pack(side="left", padx=4)
        HoverButton(bf, "↺  Reset", self._do_reset, TEXT_MUTED).pack(side="left", padx=4)

        # Stats toggle
        self._toggle_btn = tk.Label(
            card, text="▾  Stats", bg=BG_CARD, fg=TEXT_MUTED,
            font=("Segoe UI", 9), cursor="hand2",
        )
        self._toggle_btn.pack(pady=(0, _px(6)))
        self._toggle_btn.bind("<Button-1>", self._toggle_stats)
        self._toggle_btn.bind("<Enter>", lambda e: self._toggle_btn.config(fg=TEXT_PRIMARY))
        self._toggle_btn.bind("<Leave>", lambda e: self._toggle_btn.config(fg=TEXT_MUTED))

        # Collapsible stats panel
        self._stats_frame = tk.Frame(card, bg=BG_CARD)
        self.sessions_var = tk.StringVar(value="0")
        self.streak_var = tk.StringVar(value="0 min")
        self._stat(self._stats_frame, self.sessions_var, "SESSIONS").pack(side="left", expand=True)
        self._stat(self._stats_frame, self.streak_var, "FOCUS TIME").pack(side="left", expand=True)

    def _toggle_stats(self, event=None):
        if self._stats_visible:
            self._stats_frame.pack_forget()
            self._toggle_btn.config(text="▾  Stats")
            self._stats_visible = False
            self.root.geometry(self._geo())
        else:
            self._stats_frame.pack(pady=(0, _px(22)), fill="x", padx=_px(28))
            self._toggle_btn.config(text="▴  Stats")
            self._stats_visible = True
            self.root.geometry(self._geo(STATS_EXTRA_H))

    def _stat(self, parent, var, label):
        f = tk.Frame(parent, bg=BG_CARD)
        tk.Label(
            f, textvariable=var, bg=BG_CARD, fg=TEXT_PRIMARY,
            font=(MONO_FONT, 18, "bold"),
        ).pack()
        tk.Label(
            f, text=label, bg=BG_CARD, fg=TEXT_MUTED,
            font=("Segoe UI", 7, "bold"),
        ).pack()
        return f

    # ---- Settings dialog ----

    def _open_settings(self):
        if self._settings_open:
            return
        self._settings_open = True

        def on_save(cfg):
            self.work_secs = int(cfg["work_minutes"]) * 60
            self.pre_break_secs = int(cfg["pre_break_secs"])
            self.break_seconds = int(cfg["break_seconds"])
            # Apply live: clamp the running WORK countdown so it never
            # ticks past the new limit (matches the old behaviour).
            if self.state == "WORK":
                now = time.monotonic()
                rem = max(0, int(math.ceil(self._deadline - now)))
                if rem > self.work_secs:
                    self._deadline = now + self.work_secs
            elif self.state == "PAUSED" and self.paused_state == "WORK":
                if self.paused_rem > self.work_secs:
                    self.paused_rem = self.work_secs
            self._refresh_ui()

        def on_close():
            self._settings_open = False

        SettingsDialog(self.root, self.cfg, on_save, on_close=on_close)

    # ---- Timer loop (drift-free) ----

    def _remaining_now(self) -> int:
        return max(0, int(math.ceil(self._deadline - time.monotonic())))

    def _tick(self):
        if not self._running:
            return

        if self.state not in ("PAUSED", "BREAK_OVERLAY"):
            self.remaining = self._remaining_now()

            if self.state == "WORK" and self.remaining <= self.pre_break_secs:
                self._enter_pre_break()
            elif self.state == "PRE_BREAK" and self.remaining <= 0:
                self._next_phase()

        self._refresh_ui()
        self._update_tray_tip()
        self.root.after(250, self._tick)

    def _enter_pre_break(self):
        self.state = "PRE_BREAK"
        self._deadline = time.monotonic() + self.pre_break_secs
        self.remaining = self.pre_break_secs
        # The chime + taskbar flash are the whole heads-up — no popups.
        play_pre_break_sound(self.cfg)
        flash_taskbar(toplevel_hwnd(self.root))

    def _next_phase(self):
        self.state = "BREAK_OVERLAY"
        self.session_count += 1
        self._focus_minutes += int(self.cfg["work_minutes"])
        self.sessions_var.set(str(self.session_count))
        self.streak_var.set(f"{self._focus_minutes} min")
        self.root.after(200, self._show_overlay)

    def _show_overlay(self):
        if self._overlay_open:
            return
        self._overlay_open = True
        BreakOverlay(self.root, self.break_seconds, self._on_break_done)

    def _on_break_done(self, skipped=False):
        self._overlay_open = False
        self.state = "WORK"
        self.work_secs = int(self.cfg["work_minutes"]) * 60  # pick up any setting change
        self._deadline = time.monotonic() + self.work_secs
        self.remaining = self.work_secs

        play_break_done_sound(self.cfg)
        self._hide_to_tray()

    # ---- UI refresh ----

    def _place_progress(self, relw: float):
        """place() the progress fill only when its width actually changed
        (idle ticks then cost no geometry work at all)."""
        if relw != self._last_prog_rw:
            self._last_prog_rw = relw
            self.prog_fill.place(relwidth=relw)

    def _refresh_ui(self):
        if self.state == "BREAK_OVERLAY":
            config_if_changed(self.state_label, text="EYE BREAK", fg=ACCENT_GREEN)
            config_if_changed(self.time_label, text="00:00", fg=ACCENT_GREEN)
            self._place_progress(1.0)
            config_if_changed(self.prog_fill, bg=ACCENT_GREEN)
            if self.status_dot.itemcget(self._dot_circle, "fill") != ACCENT_GREEN:
                self.status_dot.itemconfig(self._dot_circle, fill=ACCENT_GREEN)
            return

        if self.state == "PRE_BREAK":
            config_if_changed(self.state_label, text="BREAK INCOMING", fg=ACCENT_AMBER)
            config_if_changed(self.time_label, text=str(self.remaining), fg=ACCENT_AMBER)
            self._place_progress(1.0)
            config_if_changed(self.prog_fill, bg=ACCENT_AMBER)
            if self.status_dot.itemcget(self._dot_circle, "fill") != ACCENT_AMBER:
                self.status_dot.itemconfig(self._dot_circle, fill=ACCENT_AMBER)
            return

        mins, secs = divmod(self.remaining, 60)
        config_if_changed(self.time_label, text=f"{mins:02d}:{secs:02d}")

        if self.state == "WORK":
            progress = 1 - (self.remaining / self.work_secs) if self.work_secs > 0 else 0
            config_if_changed(self.state_label, text="FOCUSING", fg=ACCENT_BLUE)
            config_if_changed(self.time_label, fg=TEXT_PRIMARY)
            config_if_changed(self.prog_fill, bg=ACCENT_BLUE)
            if self.status_dot.itemcget(self._dot_circle, "fill") != ACCENT_BLUE:
                self.status_dot.itemconfig(self._dot_circle, fill=ACCENT_BLUE)
            if self.prog_bg.winfo_width() > 1:
                self._place_progress(max(0.0, min(1.0, progress)))
        elif self.state == "PAUSED":
            config_if_changed(self.state_label, text="PAUSED", fg=ACCENT_AMBER)
            config_if_changed(self.time_label, fg=ACCENT_AMBER)
            if self.status_dot.itemcget(self._dot_circle, "fill") != ACCENT_AMBER:
                self.status_dot.itemconfig(self._dot_circle, fill=ACCENT_AMBER)

    def _toggle_pause(self):
        if self.state == "PRE_BREAK":
            return
        # Don't allow pausing in the last few seconds before pre-break fires
        if self.state == "WORK" and self._remaining_now() <= self.pre_break_secs + 5:
            return

        if self.state == "PAUSED":
            self.state = self.paused_state
            self._deadline = time.monotonic() + self.paused_rem
            self.remaining = self.paused_rem
            self.pause_btn.config(text="⏸  Pause")
        else:
            self.paused_state = self.state
            self.paused_rem = self._remaining_now()
            self.state = "PAUSED"
            self.pause_btn.config(text="▶  Resume")

    def _do_reset(self):
        self.work_secs = int(self.cfg["work_minutes"]) * 60
        self.pre_break_secs = int(self.cfg["pre_break_secs"])
        self.break_seconds = int(self.cfg["break_seconds"])
        self.state = "WORK"
        self._deadline = time.monotonic() + self.work_secs
        self.remaining = self.work_secs
        self.paused_state = None
        self.paused_rem = 0
        self.pause_btn.config(text="⏸  Pause")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = ArgumentParser(description=f"{APP_NAME} — 20-20-20 rule eye break timer")
    parser.add_argument(
        "--minimized", action="store_true",
        help="Start minimized to the system tray (overrides settings).",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    args = parser.parse_args()

    # Crisp rendering on high-DPI Windows displays. Must run before Tk.
    enable_windows_dpi_awareness()

    # Single-instance guard: if another t3r is running, pop its window
    # and exit quietly.
    peer_sock = acquire_single_instance()
    if peer_sock is None:
        return

    # Scale the pixel-based layout for the display's actual DPI.
    global _UI_SCALE

    cfg = Config()

    root = tk.Tk()
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except Exception:
        dpi = 96.0
    _UI_SCALE = max(1.0, min(2.5, dpi / 96.0))

    # Keep a tray app alive: print Tk callback errors to stderr instead
    # of popping a modal traceback dialog.
    def _on_tk_error(exc_type, exc, tb):
        traceback.print_exception(exc_type, exc, tb)

    root.report_callback_exception = _on_tk_error

    app = Timer2020App(root, cfg)
    app.start_peer_listener(peer_sock)

    # Center on screen
    root.update_idletasks()
    w, h = _px(BASE_W), _px(BASE_H)
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")

    # Auto-minimize to tray on launch (the user wants the app to boot
    # quietly into the tray, not pop a window in their face).
    start_minimized = args.minimized or bool(cfg.get("start_minimized", True))
    if start_minimized:
        root.after(100, app._hide_to_tray)

    root.mainloop()


if __name__ == "__main__":
    main()
