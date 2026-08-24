"""
t3r — a tiny 20-20-20 rule eye break timer.

Every `work_minutes` minutes it dims the screen, plays a soft chime,
and shows a fullscreen break overlay with a countdown ring and a slow
breathing animation to encourage relaxed eye rest.

Improvements over the original:
  * Windows-only modules (winreg / winsound / ctypes.windll) are gated so
    the file imports cleanly on Linux / macOS too (graceful no-ops).
  * Persistent JSON config in the user's per-platform config dir.
  * Settings dialog (work / pre-break / break durations, sounds, autostart,
    start-minimized) reachable from both the tray menu and a gear button.
  * Autostart command uses `pythonw.exe` when running as a .py (no console
    window) and passes `--minimized` so the app boots straight to tray.
  * Break overlay now has a breathing-circle animation + "Breathe in /
    Breathe out" cues, alongside the countdown ring.
  * Polished main window: status dot indicator, HoverButton with hover
    state, gear button to open settings.
  * Better eye-themed tray icon (iris + pupil + reflection).
  * `--minimized` CLI flag and a default-on `start_minimized` setting
    so the app always boots into the tray.
"""

from __future__ import annotations

import io
import json
import math
import os
import struct
import subprocess
import sys
import threading
import tkinter as tk
from argparse import ArgumentParser
from pathlib import Path

from PIL import Image, ImageDraw
import pystray

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

# Color palette (dark, low-glare)
BG_DARK = "#0A0A0F"
BG_CARD = "#13131C"
ACCENT_BLUE = "#4A9EFF"
ACCENT_GREEN = "#3DD68C"
ACCENT_AMBER = "#FFB347"
TEXT_PRIMARY = "#F0F0F5"
TEXT_MUTED = "#5A5A72"
BORDER = "#22222E"


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
    "autostart": False,
    "start_minimized": True,  # the user wants the app to boot into the tray
}


class Config:
    """Tiny dict-like wrapper that loads/saves JSON from disk."""

    def __init__(self):
        self._data = dict(DEFAULT_CONFIG)
        self._load()

    def _load(self):
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data.update(loaded)
        except Exception:
            pass  # corrupt or unreadable — keep defaults

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


def play_pre_break_sound(cfg: Config):
    if not cfg.get("sound_enabled", True):
        return
    buf = _generate_chime(freq=520, duration_ms=200, volume=0.2)
    threading.Thread(target=_play_wav, args=(buf,), daemon=True).start()


def play_break_done_sound(cfg: Config):
    if not cfg.get("sound_enabled", True):
        return
    buf = _generate_chime(freq=800, duration_ms=400, volume=0.2)
    threading.Thread(target=_play_wav, args=(buf,), daemon=True).start()


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def send_notification(title: str, message: str):
    try:
        if _IS_WINDOWS:
            from plyer import notification
            notification.notify(title=title, message=message, app_name=APP_NAME, timeout=6)
        elif _IS_MACOS:
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                capture_output=True,
            )
        else:
            subprocess.run(["notify-send", "-t", "5000", title, message], capture_output=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------

def create_icon_image() -> Image.Image:
    """A polished eye-themed icon: green iris, dark pupil, white reflection."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = size // 2

    # Outer ring (subtle dark green halo)
    draw.ellipse([4, 4, 60, 60], fill=(15, 61, 34, 255))
    # Iris (bright green)
    draw.ellipse([8, 8, 56, 56], fill=(61, 214, 140, 255))
    # Inner ring (depth)
    draw.ellipse([18, 18, 46, 46], fill=(40, 150, 95, 255))
    # Pupil
    draw.ellipse([24, 24, 40, 40], fill=(10, 26, 16, 255))
    # Big reflection
    draw.ellipse([28, 28, 33, 33], fill=(240, 240, 245, 255))
    # Tiny secondary reflection
    draw.ellipse([34, 34, 37, 37], fill=(240, 240, 245, 180))
    return img


# ---------------------------------------------------------------------------
# Windows autostart via HKEY_CURRENT_USER\Software\Microsoft\Windows\
# CurrentVersion\Run
# ---------------------------------------------------------------------------

def _autostart_command() -> str:
    """Build the command line stored in the registry for autostart."""
    if getattr(sys, "frozen", False):
        # PyInstaller bundle — just run the .exe with --minimized
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
# Windows taskbar flash
# ---------------------------------------------------------------------------

def flash_taskbar(hwnd: int):
    if not _IS_WINDOWS or ctypes is None:
        return
    try:
        ctypes.windll.user32.FlashWindow(hwnd, True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# UI helper: HoverButton
# ---------------------------------------------------------------------------

class HoverButton(tk.Frame):
    """Flat tk.Button substitute with smooth hover background."""

    def __init__(self, parent, text: str, cmd, color: str,
                 bg: str = BG_CARD, hover_bg: str = BORDER, **kwargs):
        super().__init__(parent, bg=bg, **kwargs)
        self._color = color
        self._bg = bg
        self._hover_bg = hover_bg
        self._cmd = cmd
        self.label = tk.Label(
            self, text=text, bg=bg, fg=color, relief="flat",
            font=("Segoe UI", 10, "bold"), cursor="hand2",
            padx=14, pady=7, highlightthickness=1, highlightbackground=BORDER,
        )
        self.label.pack()
        self.label.bind("<Button-1>", self._on_click)
        self.label.bind("<Enter>", self._on_enter)
        self.label.bind("<Leave>", self._on_leave)

    def _on_click(self, _e):
        if callable(self._cmd):
            self._cmd()

    def _on_enter(self, _e):
        self.label.config(bg=self._hover_bg, fg=TEXT_PRIMARY)

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
# Break overlay — fullscreen, countdown ring + breathing circle
# ---------------------------------------------------------------------------

class BreakOverlay:
    OVERLAY_BG = "#051A10"
    RING_FG = "#3DD68C"
    RING_EMPTY = "#0F3D22"
    SKIP_FG = "#1D6B3E"
    TEXT_DIM = "#2A7A4A"
    BREATH_FG = "#3DD68C"
    BREATH_INNER = "#2A7A4A"

    CANVAS_SIZE = 360
    BREATH_CYCLE_SECS = 8  # 4s in, 4s out
    TIPS = [
        "Look at something 20 feet away",
        "Relax your shoulders, soften your gaze",
        "Blink slowly a few times",
        "Palm your eyes gently if you like",
    ]

    def __init__(self, parent_root, seconds: int, on_done):
        self.seconds = seconds
        self.remaining = seconds
        self.on_done = on_done
        self._running = True
        self._anim_start = None  # wall-clock seconds since overlay opened

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
        self._animate_breath()

    def _build(self):
        w = self.win
        center = tk.Frame(w, bg=self.OVERLAY_BG)
        center.place(relx=0.5, rely=0.5, anchor="center")

        size = self.CANVAS_SIZE
        self.canvas = tk.Canvas(
            center, width=size, height=size,
            bg=self.OVERLAY_BG, highlightthickness=0,
        )
        self.canvas.pack()

        cx = cy = size // 2

        # Tip text (top)
        self.tip_text = self.canvas.create_text(
            cx, cy - 150, text=self.TIPS[0],
            fill=self.TEXT_DIM, font=("Segoe UI", 11),
        )

        # Countdown ring (background + arc)
        r_out = 130
        self.canvas.create_oval(
            cx - r_out, cy - r_out, cx + r_out, cy + r_out,
            outline=self.RING_EMPTY, width=8,
        )
        self.arc = self.canvas.create_arc(
            cx - r_out + 4, cy - r_out + 4, cx + r_out - 4, cy + r_out - 4,
            start=90, extent=359.9, outline=self.RING_FG, width=8, style="arc",
        )

        # Breathing circle (outer + inner)
        self._breath_r_base = 70
        self._breath_r_amp = 28
        r = self._breath_r_base
        self._breath_oval = self.canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            fill="", outline=self.BREATH_FG, width=2,
        )
        r_in = int(r * 0.65)
        self._breath_inner = self.canvas.create_oval(
            cx - r_in, cy - r_in, cx + r_in, cy + r_in,
            fill="", outline=self.BREATH_INNER, width=1,
        )

        # Countdown number (center)
        self.num_text = self.canvas.create_text(
            cx, cy, text=str(self.remaining),
            fill=TEXT_PRIMARY, font=("Courier New", 44, "bold"),
        )

        # Breathing instruction (below the ring)
        self.breath_text = self.canvas.create_text(
            cx, cy + 170, text="Breathe in...",
            fill=self.BREATH_FG, font=("Segoe UI", 13, "bold"),
        )

        # Skip controls (bottom corners)
        skip = tk.Label(
            w, text="Skip  ×", bg=self.OVERLAY_BG, fg=self.SKIP_FG,
            font=("Segoe UI", 10), cursor="hand2",
        )
        skip.place(relx=1.0, rely=1.0, anchor="se", x=-28, y=-24)
        skip.bind("<Button-1>", lambda e: self._skip())
        tk.Label(
            w, text="Press Esc or Space to skip",
            bg=self.OVERLAY_BG, fg=self.SKIP_FG, font=("Segoe UI", 9),
        ).place(relx=0.5, rely=0.96, anchor="center")

    def _animate_breath(self, step: int = 0):
        if not self._running:
            return
        elapsed = self.seconds - self.remaining  # whole seconds elapsed
        # Sub-second phase from a monotonic counter (step * 50ms)
        sub_sec = (step * 50) / 1000.0
        cycle_t = (elapsed + sub_sec) % self.BREATH_CYCLE_SECS
        phase = (cycle_t / self.BREATH_CYCLE_SECS) * 2 * math.pi
        # sin shifted by -pi/2: 0 at t=0 (exhaled), 1 at t=cycle/2 (inhaled)
        breath_value = (math.sin(phase - math.pi / 2) + 1) / 2

        cx = cy = self.CANVAS_SIZE // 2
        r = self._breath_r_base + self._breath_r_amp * breath_value
        self.canvas.coords(
            self._breath_oval, cx - r, cy - r, cx + r, cy + r,
        )
        r_in = r * 0.65
        self.canvas.coords(
            self._breath_inner, cx - r_in, cy - r_in, cx + r_in, cy + r_in,
        )

        # Instruction + tip rotation
        if cycle_t < self.BREATH_CYCLE_SECS / 2:
            instruction = "Breathe in..."
        else:
            instruction = "Breathe out..."
        self.canvas.itemconfig(self.breath_text, text=instruction)

        tip_index = (elapsed // 5) % len(self.TIPS)
        self.canvas.itemconfig(self.tip_text, text=self.TIPS[tip_index])

        self.win.after(50, lambda: self._animate_breath(step + 1))

    def _tick(self):
        if not self._running:
            return
        self.canvas.itemconfig(self.num_text, text=str(self.remaining))
        frac = self.remaining / self.seconds if self.seconds > 0 else 0
        self.canvas.itemconfig(self.arc, extent=max(0.1, frac * 359.9))
        if self.remaining <= 0:
            self._finish()
            return
        self.remaining -= 1
        self.win.after(1000, self._tick)

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
        self.win.title("t3r — Settings")
        self.win.geometry("380x460")
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
        self._autostart = tk.BooleanVar(value=is_autostart_enabled())
        self._start_min = tk.BooleanVar(value=bool(cfg["start_minimized"]))

        self._build()

    def _build(self):
        w = self.win
        card = tk.Frame(w, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True, padx=20, pady=20)

        tk.Label(
            card, text="Settings", bg=BG_CARD, fg=TEXT_PRIMARY,
            font=("Segoe UI", 14, "bold"),
        ).pack(pady=(18, 4))
        tk.Label(
            card, text="Changes apply on the next session", bg=BG_CARD, fg=TEXT_MUTED,
            font=("Segoe UI", 8),
        ).pack(pady=(0, 14))

        self._row(card, "Work duration (min)", self._work_min, 1, 120)
        self._row(card, "Pre-break warning (sec)", self._pre_break, 1, 30)
        self._row(card, "Break duration (sec)", self._break_secs, 5, 90)

        self._check(card, "Enable chime sounds", self._sound)
        self._check(card, "Start on login", self._autostart)
        self._check(card, "Start minimized to tray", self._start_min)

        bf = tk.Frame(card, bg=BG_CARD)
        bf.pack(pady=(18, 18))
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
            bg=BG_DARK, fg=TEXT_PRIMARY, font=("Courier New", 11, "bold"),
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

    def _save(self):
        if self._closed:
            return
        self._closed = True

        self.cfg["work_minutes"] = max(1, min(120, int(self._work_min.get())))
        self.cfg["pre_break_secs"] = max(1, min(30, int(self._pre_break.get())))
        self.cfg["break_seconds"] = max(5, min(90, int(self._break_secs.get())))
        self.cfg["sound_enabled"] = bool(self._sound.get())
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

        self.root.title(APP_NAME)
        self.root.geometry("360x340")
        self.root.resizable(False, False)
        self.root.configure(bg=BG_DARK)
        self.root.attributes("-topmost", True)

        self.state = "WORK"
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

        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        self._build_ui()
        self._setup_icon_and_tray()
        self._tick()

    # ---- Tray / window plumbing ----

    def _setup_icon_and_tray(self):
        icon_img = create_icon_image()
        buf = io.BytesIO()
        icon_img.save(buf, format="PNG")
        self._tk_icon = tk.PhotoImage(data=buf.getvalue())
        self.root.iconphoto(True, self._tk_icon)

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
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _hide_to_tray(self):
        self._window_hidden = True
        self.root.withdraw()

    def _do_show(self, icon=None, item=None):
        self._window_hidden = False
        self.root.after(0, self._show_window)

    def _show_window(self):
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
            mins = self.remaining // 60
            secs = self.remaining % 60
            self._tray.title = f"t3r | Focus {mins:02d}:{secs:02d}"
        elif self.state == "PRE_BREAK":
            self._tray.title = f"t3r | Break in {self.remaining}s"
        elif self.state == "BREAK_OVERLAY":
            self._tray.title = "t3r | Eye break"
        else:
            self._tray.title = "t3r | Paused"

    # ---- UI ----

    def _build_ui(self):
        r = self.root

        card = tk.Frame(r, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True, padx=20, pady=20)

        # Header: status dot + state label + spacer + settings gear
        header = tk.Frame(card, bg=BG_CARD)
        header.pack(fill="x", padx=28, pady=(22, 0))

        dot_wrap = tk.Frame(header, bg=BG_CARD, width=12, height=12)
        dot_wrap.pack(side="left")
        self.status_dot = tk.Canvas(dot_wrap, width=12, height=12, bg=BG_CARD, highlightthickness=0)
        self.status_dot.pack()
        self._dot_circle = self.status_dot.create_oval(1, 1, 11, 11, fill=ACCENT_BLUE, outline="")

        self.state_label = tk.Label(
            header, text="FOCUSING", bg=BG_CARD, fg=TEXT_MUTED,
            font=("Segoe UI", 10, "bold"),
        )
        self.state_label.pack(side="left", padx=(6, 0))

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
            font=("Courier New", 56, "bold"),
        )
        self.time_label.pack(pady=(8, 14))

        # Progress bar (slightly thicker for presence)
        self.prog_bg = tk.Frame(card, bg=BORDER, height=4)
        self.prog_bg.pack(fill="x", padx=28, pady=(0, 18))
        self.prog_fill = tk.Frame(self.prog_bg, bg=ACCENT_BLUE, height=4)
        self.prog_fill.place(x=0, y=0, relheight=1, relwidth=0)

        # Buttons
        bf = tk.Frame(card, bg=BG_CARD)
        bf.pack(pady=(0, 14))
        self.pause_btn = HoverButton(bf, "⏸  Pause", self._toggle_pause, ACCENT_BLUE)
        self.pause_btn.pack(side="left", padx=4)
        HoverButton(bf, "↺  Reset", self._do_reset, TEXT_MUTED).pack(side="left", padx=4)

        # Stats toggle
        self._toggle_btn = tk.Label(
            card, text="▾  Stats", bg=BG_CARD, fg=TEXT_MUTED,
            font=("Segoe UI", 9), cursor="hand2",
        )
        self._toggle_btn.pack(pady=(0, 6))
        self._toggle_btn.bind("<Button-1>", self._toggle_stats)
        self._toggle_btn.bind("<Enter>", lambda e: self._toggle_btn.config(fg=TEXT_PRIMARY))
        self._toggle_btn.bind("<Leave>", lambda e: self._toggle_btn.config(fg=TEXT_MUTED))

        # Collapsible stats panel
        self._stats_frame = tk.Frame(card, bg=BG_CARD)
        self.sessions_var = tk.StringVar(value="0")
        self.streak_var = tk.StringVar(value="0 min")
        self._stat(self._stats_frame, self.sessions_var, "Sessions").pack(side="left", expand=True)
        self._stat(self._stats_frame, self.streak_var, "Focus time").pack(side="left", expand=True)

    def _toggle_stats(self, event=None):
        if self._stats_visible:
            self._stats_frame.pack_forget()
            self._toggle_btn.config(text="▾  Stats")
            self._stats_visible = False
            self.root.geometry("360x340")
        else:
            self._stats_frame.pack(pady=(0, 22), fill="x", padx=28)
            self._toggle_btn.config(text="▴  Stats")
            self._stats_visible = True
            self.root.geometry("360x390")

    def _stat(self, parent, var, label):
        f = tk.Frame(parent, bg=BG_CARD)
        tk.Label(
            f, textvariable=var, bg=BG_CARD, fg=TEXT_PRIMARY,
            font=("Courier New", 18, "bold"),
        ).pack()
        tk.Label(
            f, text=label, bg=BG_CARD, fg=TEXT_MUTED,
            font=("Segoe UI", 8),
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
            # If in WORK state and current remaining exceeds the new limit,
            # clamp it so the timer doesn't keep ticking past the new work_secs.
            if self.state == "WORK" and self.remaining > self.work_secs:
                self.remaining = self.work_secs
            elif self.state == "PAUSED" and self.paused_state == "WORK" and self.paused_rem > self.work_secs:
                self.paused_rem = self.work_secs
            self._refresh_ui()

        def on_close():
            self._settings_open = False

        SettingsDialog(self.root, self.cfg, on_save, on_close=on_close)

    # ---- Timer loop ----

    def _tick(self):
        if not self._running:
            return

        if self.state not in ("PAUSED", "BREAK_OVERLAY"):
            self.remaining -= 1

            if self.state == "WORK" and self.remaining <= self.pre_break_secs:
                self._enter_pre_break()
            elif self.state == "PRE_BREAK" and self.remaining == 0:
                self._next_phase()

        self._refresh_ui()
        self._update_tray_tip()
        self.root.after(1000, self._tick)

    def _enter_pre_break(self):
        self.state = "PRE_BREAK"
        self.remaining = self.pre_break_secs
        play_pre_break_sound(self.cfg)
        flash_taskbar(int(self.root.winfo_id()))
        threading.Thread(
            target=send_notification, daemon=True,
            args=("⏳ Break Incoming",
                  f"Prepare to look away. Break starts in {self.pre_break_secs} seconds."),
        ).start()

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
        self.remaining = self.work_secs

        play_break_done_sound(self.cfg)
        if not skipped:
            threading.Thread(
                target=send_notification, daemon=True,
                args=("✓ Break Done", "Nice. Next eye break in "
                      f"{int(self.cfg['work_minutes'])} minutes."),
            ).start()
        self._hide_to_tray()

    # ---- UI refresh ----

    def _refresh_ui(self):
        if self.state == "BREAK_OVERLAY":
            self.state_label.config(text="EYE BREAK", fg=ACCENT_GREEN)
            self.time_label.config(text="00:00", fg=ACCENT_GREEN)
            self.prog_fill.place(relwidth=1.0)
            self.prog_fill.config(bg=ACCENT_GREEN)
            self.status_dot.itemconfig(self._dot_circle, fill=ACCENT_GREEN)
            return

        if self.state == "PRE_BREAK":
            self.state_label.config(text="BREAK INCOMING", fg=ACCENT_AMBER)
            self.time_label.config(text=str(self.remaining), fg=ACCENT_AMBER)
            self.prog_fill.place(relwidth=1.0)
            self.prog_fill.config(bg=ACCENT_AMBER)
            self.status_dot.itemconfig(self._dot_circle, fill=ACCENT_AMBER)
            return

        mins = self.remaining // 60
        secs = self.remaining % 60
        self.time_label.config(text=f"{mins:02d}:{secs:02d}")

        if self.state == "WORK":
            progress = 1 - (self.remaining / self.work_secs) if self.work_secs > 0 else 0
            self.state_label.config(text="FOCUSING", fg=ACCENT_BLUE)
            self.time_label.config(fg=TEXT_PRIMARY)
            self.prog_fill.config(bg=ACCENT_BLUE)
            self.status_dot.itemconfig(self._dot_circle, fill=ACCENT_BLUE)
            bar_w = self.prog_bg.winfo_width()
            if bar_w > 1:
                self.prog_fill.place(relwidth=max(0.0, min(1.0, progress)))
        elif self.state == "PAUSED":
            self.state_label.config(text="PAUSED", fg=ACCENT_AMBER)
            self.time_label.config(fg=ACCENT_AMBER)
            self.status_dot.itemconfig(self._dot_circle, fill=ACCENT_AMBER)

    def _toggle_pause(self):
        if self.state == "PRE_BREAK":
            return
        # Don't allow pausing in the last few seconds before pre-break fires
        if self.state == "WORK" and self.remaining <= self.pre_break_secs + 5:
            return

        if self.state == "PAUSED":
            self.state = self.paused_state
            self.remaining = self.paused_rem
            self.pause_btn.config(text="⏸  Pause")
        else:
            self.paused_state = self.state
            self.paused_rem = self.remaining
            self.state = "PAUSED"
            self.pause_btn.config(text="▶  Resume")

    def _do_reset(self):
        self.work_secs = int(self.cfg["work_minutes"]) * 60
        self.pre_break_secs = int(self.cfg["pre_break_secs"])
        self.break_seconds = int(self.cfg["break_seconds"])
        self.state = "WORK"
        self.remaining = self.work_secs
        self.paused_state = None
        self.paused_rem = 0
        self.pause_btn.config(text="⏸  Pause")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = ArgumentParser(description="t3r — 20-20-20 rule eye break timer")
    parser.add_argument(
        "--minimized", action="store_true",
        help="Start minimized to the system tray (overrides settings).",
    )
    args = parser.parse_args()

    cfg = Config()

    root = tk.Tk()
    app = Timer2020App(root, cfg)

    # Center on screen
    root.update_idletasks()
    w, h = 360, 340
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
