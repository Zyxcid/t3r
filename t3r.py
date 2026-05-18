import tkinter as tk
import threading
import subprocess
import sys
import os
import winreg
import ctypes
import winsound
import math
import struct
import io

from PIL import Image, ImageDraw
import pystray

WORK_MINUTES  = 20
PRE_BREAK_SECS = 5
BREAK_SECONDS = 20

BG_DARK      = "#0A0A0F"
BG_CARD      = "#13131C"
ACCENT_BLUE  = "#4A9EFF"
ACCENT_GREEN = "#3DD68C"
ACCENT_AMBER = "#FFB347"
TEXT_PRIMARY = "#F0F0F5"
TEXT_MUTED   = "#5A5A72"
BORDER       = "#22222E"

WORK_SECS = WORK_MINUTES * 60
APP_NAME = "t3r"


def _generate_chime(freq=440, duration_ms=300, volume=0.2):
    sample_rate = 44100
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    samples = []
    for i in range(num_samples):
        t = float(i) / sample_rate
        envelope = min(1.0, t * 20, (duration_ms/1000.0 - t) * 20)
        value = volume * envelope * math.sin(2.0 * math.pi * freq * t)
        samples.append(int(value * 32767))
    
    buf = io.BytesIO()
    data = b''.join(struct.pack('<h', s) for s in samples)
    buf.write(b'RIFF')
    buf.write(struct.pack('<I', 36 + len(data)))
    buf.write(b'WAVEfmt ')
    buf.write(struct.pack('<IHHIIHH', 16, 1, 1, sample_rate, sample_rate * 2, 2, 16))
    buf.write(b'data')
    buf.write(struct.pack('<I', len(data)))
    buf.write(data)
    buf.seek(0)
    return buf

def play_pre_break_sound():
    buf = _generate_chime(520, 200)
    def _play():
        data = buf.read()
        if data:
            winsound.PlaySound(data, winsound.SND_MEMORY)
    threading.Thread(target=_play, daemon=True).start()

def play_break_done_sound():
    buf = _generate_chime(800, 400)
    def _play():
        data = buf.read()
        if data:
            winsound.PlaySound(data, winsound.SND_MEMORY)
    threading.Thread(target=_play, daemon=True).start()

def send_notification(title, message):
    if sys.platform == "win32":
        try:
            from plyer import notification
            notification.notify(title=title, message=message, app_name="t3r", timeout=6)
        except Exception:
            pass
    elif sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{message}" with title "{title}"'], capture_output=True)
    else:
        subprocess.run(["notify-send", "-t", "5000", title, message], capture_output=True)

def create_icon_image():
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([8, 8, 56, 56], fill=(61, 214, 140, 255))
    return img

def is_autostart_enabled():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except WindowsError:
        return False

def toggle_autostart():
    if is_autostart_enabled():
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_WRITE)
        winreg.DeleteValue(key, APP_NAME)
        winreg.CloseKey(key)
    else:
        exe_path = sys.executable 
        cmd = f'"{exe_path}" "{os.path.abspath(sys.argv[0])}"' if exe_path.endswith("python.exe") else f'"{exe_path}"'
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_WRITE)
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)


class BreakOverlay:
    OVERLAY_BG = "#051A10"
    RING_FG    = "#3DD68C"
    RING_EMPTY = "#0F3D22"
    SKIP_FG    = "#1D6B3E"

    def __init__(self, parent_root, seconds, on_done):
        self.seconds   = seconds
        self.remaining = seconds
        self.on_done   = on_done
        self._running  = True

        self.win = tk.Toplevel(parent_root)
        self.win.configure(bg=self.OVERLAY_BG)
        self.win.attributes("-topmost", True)
        self.win.attributes("-fullscreen", True)
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)
        self.win.bind("<Escape>", lambda e: self._skip())
        self._build()
        self._tick()

    def _build(self):
        w = self.win
        center = tk.Frame(w, bg=self.OVERLAY_BG)
        center.place(relx=0.5, rely=0.5, anchor="center")

        size = 280
        self.canvas = tk.Canvas(center, width=size, height=size, bg=self.OVERLAY_BG, highlightthickness=0)
        self.canvas.pack()

        cx = cy = size // 2
        r  = 110
        self.canvas.create_oval(cx-r, cy-r, cx+r, cy+r, outline=self.RING_EMPTY, width=10)
        self.arc = self.canvas.create_arc(cx-r+5, cy-r+5, cx+r-5, cy+r-5, start=90, extent=359.9, outline=self.RING_FG, width=10, style="arc")
        self.num_text = self.canvas.create_text(cx, cy - 14, text=str(self.remaining), fill=TEXT_PRIMARY, font=("Courier New", 72, "bold"))
        self.canvas.create_text(cx, cy + 54, text="sec", fill="#2A7A4A", font=("Segoe UI", 14))

        skip = tk.Label(w, text="Skip  ×", bg=self.OVERLAY_BG, fg=self.SKIP_FG, font=("Segoe UI", 10), cursor="hand2")
        skip.place(relx=1.0, rely=1.0, anchor="se", x=-28, y=-24)
        skip.bind("<Button-1>", lambda e: self._skip())
        tk.Label(w, text="Press Esc to skip", bg=self.OVERLAY_BG, fg=self.SKIP_FG, font=("Segoe UI", 9)).place(relx=0.5, rely=0.96, anchor="center")

    def _tick(self):
        if not self._running:
            return
        self.canvas.itemconfig(self.num_text, text=str(self.remaining))
        frac = self.remaining / self.seconds
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


class Timer2020App:
    def __init__(self, root):
        self.root = root
        self.root.title("t3r")
        self.root.geometry("360x340")
        self.root.resizable(False, False)
        self.root.configure(bg=BG_DARK)
        self.root.attributes("-topmost", True)

        self.state          = "WORK"
        self.remaining      = WORK_SECS
        self.paused_state   = None
        self.paused_rem     = 0
        self._running       = True
        self.session_count  = 0
        self._focus_minutes = 0
        self._overlay_open  = False
        self._window_hidden = False
        self._stats_visible = False
        self._tray          = None

        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        self._build_ui()
        self._setup_icon_and_tray()
        self._tick()

    def _setup_icon_and_tray(self):
        import io as _io
        icon_img = create_icon_image()
        
        buf = _io.BytesIO()
        icon_img.save(buf, format="PNG")
        self._tk_icon = tk.PhotoImage(data=buf.getvalue())
        self.root.iconphoto(True, self._tk_icon)

        menu = pystray.Menu(
            pystray.MenuItem("Show Timer", self._do_show, default=True),
            pystray.MenuItem("Pause / Resume", self._pause_resume),
            pystray.MenuItem("Reset", self._reset),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start on Login", toggle_autostart, checked=lambda item: is_autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._quit_app),
        )

        self._tray = pystray.Icon("t3r", icon_img, "t3r", menu=menu)
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

    def _quit_app(self, icon=None, item=None):
        self.root.after(0, self._do_quit)

    def _do_quit(self):
        self._running = False
        if self._tray:
            self._tray.stop()
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

    def _build_ui(self):
        r = self.root

        card = tk.Frame(r, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True, padx=20, pady=20)

        self.state_label = tk.Label(card, text="FOCUSING", bg=BG_CARD, fg=TEXT_MUTED, font=("Segoe UI", 10, "bold"))
        self.state_label.pack(pady=(24, 0))

        self.time_label = tk.Label(card, text="20:00", bg=BG_CARD, fg=TEXT_PRIMARY, font=("Courier New", 64, "bold"))
        self.time_label.pack(pady=(2, 18))

        self.prog_bg = tk.Frame(card, bg=BORDER, height=3)
        self.prog_bg.pack(fill="x", padx=28, pady=(0, 20))
        self.prog_fill = tk.Frame(self.prog_bg, bg=ACCENT_BLUE, height=3)
        self.prog_fill.place(x=0, y=0, relheight=1, relwidth=0)

        bf = tk.Frame(card, bg=BG_CARD)
        bf.pack(pady=(0, 18))
        self.pause_btn = self._btn(bf, "⏸  Pause", self._toggle_pause, ACCENT_BLUE)
        self.pause_btn.pack(side="left", padx=6)
        self._btn(bf, "↺  Reset", self._do_reset, TEXT_MUTED).pack(side="left", padx=6)

        self._toggle_btn = tk.Label(card, text="▾  Stats", bg=BG_CARD, fg=TEXT_MUTED, font=("Segoe UI", 9), cursor="hand2")
        self._toggle_btn.pack(pady=(0, 8))
        self._toggle_btn.bind("<Button-1>", self._toggle_stats)
        self._toggle_btn.bind("<Enter>", lambda e: self._toggle_btn.config(fg=TEXT_PRIMARY))
        self._toggle_btn.bind("<Leave>", lambda e: self._toggle_btn.config(fg=TEXT_MUTED))

        self._stats_frame = tk.Frame(card, bg=BG_CARD)
        self.sessions_var = tk.StringVar(value="0")
        self.streak_var   = tk.StringVar(value="0 min")
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

    def _btn(self, parent, text, cmd, color):
        return tk.Button(parent, text=text, command=cmd, bg=BG_CARD, fg=color, relief="flat",
                         font=("Segoe UI", 10, "bold"), activebackground=BORDER,
                         activeforeground=TEXT_PRIMARY, cursor="hand2", padx=12, pady=5,
                         highlightthickness=1, highlightbackground=BORDER)

    def _stat(self, parent, var, label):
        f = tk.Frame(parent, bg=BG_CARD)
        tk.Label(f, textvariable=var, bg=BG_CARD, fg=TEXT_PRIMARY, font=("Courier New", 18, "bold")).pack()
        tk.Label(f, text=label, bg=BG_CARD, fg=TEXT_MUTED, font=("Segoe UI", 8)).pack()
        return f

    def _tick(self):
        if not self._running:
            return
        
        if self.state not in ("PAUSED", "BREAK_OVERLAY"):
            self.remaining -= 1
            
            if self.state == "WORK" and self.remaining <= PRE_BREAK_SECS:
                self._enter_pre_break()
            elif self.state == "PRE_BREAK" and self.remaining == 0:
                self._next_phase()
                    
        self._refresh_ui()
        self._update_tray_tip()
        self.root.after(1000, self._tick)

    def _flash_taskbar(self):
        try:
            hwnd = int(self.root.winfo_id())
            ctypes.windll.user32.FlashWindow(hwnd, True)
        except Exception:
            pass

    def _enter_pre_break(self):
        self.state = "PRE_BREAK"
        self.remaining = PRE_BREAK_SECS
        play_pre_break_sound()
        self._flash_taskbar()
        
        threading.Thread(target=send_notification, daemon=True,
            args=("⏳ Break Incoming", f"Prepare to look away. Break starts in {PRE_BREAK_SECS} seconds."),
        ).start()

    def _next_phase(self):
        self.state           = "BREAK_OVERLAY"
        self.session_count  += 1
        self._focus_minutes += WORK_MINUTES
        self.sessions_var.set(str(self.session_count))
        self.streak_var.set(f"{self._focus_minutes} min")
        self.root.after(200, self._show_overlay)

    def _show_overlay(self):
        if self._overlay_open:
            return
        self._overlay_open = True
        BreakOverlay(self.root, BREAK_SECONDS, self._on_break_done)

    def _on_break_done(self, skipped=False):
        self._overlay_open = False
        self.state         = "WORK"
        self.remaining     = WORK_SECS if skipped else WORK_SECS
        
        play_break_done_sound()
        self._hide_to_tray()

    def _refresh_ui(self):
        if self.state == "BREAK_OVERLAY":
            self.state_label.config(text="EYE BREAK", fg=ACCENT_GREEN)
            self.time_label.config(text="00:00", fg=ACCENT_GREEN)
            self.prog_fill.place(relwidth=1.0)
            self.prog_fill.config(bg=ACCENT_GREEN)
            return

        if self.state == "PRE_BREAK":
            self.state_label.config(text="BREAK INCOMING", fg=ACCENT_AMBER)
            self.time_label.config(text=str(self.remaining), fg=ACCENT_AMBER)
            self.prog_fill.place(relwidth=1.0)
            self.prog_fill.config(bg=ACCENT_AMBER)
            return

        mins = self.remaining // 60
        secs = self.remaining % 60
        self.time_label.config(text=f"{mins:02d}:{secs:02d}")

        if self.state == "WORK":
            progress = 1 - (self.remaining / WORK_SECS)
            self.state_label.config(text="FOCUSING", fg=ACCENT_BLUE)
            self.time_label.config(fg=TEXT_PRIMARY)
            self.prog_fill.config(bg=ACCENT_BLUE)
            bar_w = self.prog_bg.winfo_width()
            if bar_w > 1:
                self.prog_fill.place(relwidth=max(0.0, min(1.0, progress)))
        elif self.state == "PAUSED":
            self.state_label.config(text="PAUSED", fg=ACCENT_AMBER)
            self.time_label.config(fg=ACCENT_AMBER)

    def _toggle_pause(self):
        if self.state == "PRE_BREAK":
            return 
        if self.state == "WORK" and self.remaining <= 11:
            return

        if self.state == "PAUSED":
            self.state     = self.paused_state
            self.remaining = self.paused_rem
            self.pause_btn.config(text="⏸  Pause")
        else:
            self.paused_state = self.state
            self.paused_rem   = self.remaining
            self.state        = "PAUSED"
            self.pause_btn.config(text="▶  Resume")

    def _do_reset(self):
        self.state     = "WORK"
        self.remaining = WORK_SECS
        self.pause_btn.config(text="⏸  Pause")


def main():
    root = tk.Tk()
    app  = Timer2020App(root)

    root.update_idletasks()
    w, h = 360, 340
    x = (root.winfo_screenwidth()  - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")

    root.mainloop()


if __name__ == "__main__":
    main()