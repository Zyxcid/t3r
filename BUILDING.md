# Building t3r — packaging & antivirus notes

Companion to the README: how to turn `t3r.py` into a Windows exe, and
what to do when Windows Defender flags it as a virus.

---

## 1. Why Windows may mark the exe as a virus

Windows Defender did not find malware in your code. It matched a
*heuristic*. PyInstaller `--onefile` exes are packed as a compressed
self-extracting archive around a generic bootloader, and that pattern is
so common in real malware that machine-learning classifiers flag
unsigned, metadata-less, freshly-compiled onefile exes by default. The
detection name usually ends in `!ml` (machine learning) — e.g.
`Trojan:Win32/Wacatac!ml` — which is the tell-tale sign of a generic
false positive.

The four things that decide the verdict: **packing method** (onefile is
the worst), **UPX compression** (never use it), **embedded metadata**
(unsigned exe with no company/product info looks exactly like malware),
and **reputation** (a hash nobody has seen yet is distrusted; signatures
and Microsoft whitelist entries build it).

You cannot make a *new* unsigned exe instantly trusted — but the
hardened build removes the triggers, and a one-time report to Microsoft
clears the remaining flag.

---

## 2. Which build should you upload to GitHub Releases?

| You want | Use | Result |
|---|---|---|
| **One exe, no folder** | `build_nuitka.bat` | `dist_onefile\t3r.exe` — a single file users download and run. Recommended for GitHub Releases. |
| The lowest antivirus risk possible | `build_exe.bat` | `dist\t3r\` folder — zip it, upload the zip; users extract once and run `t3r.exe`. |

A plain `pyinstaller --onefile` also produces a single exe, but it is
the single most antivirus-flagged packaging pattern in existence (a
generic self-extracting bootloader around compressed bytecode) — the
exact thing that got the original build flagged. We deliberately don't
ship a script for it.

And no, rewriting t3r in C would not change the trust situation:
SmartScreen and Defender distrust *unsigned exes from unknown
publishers*, in any language. C would only shrink the exe to ~100 KB
and start a bit faster — at the cost of rewriting the whole app.

### First launch of the onefile exe

The Nuitka onefile exe unpacks itself into a cache folder under
`%LOCALAPPDATA%` the first time it runs — allow a few extra seconds on
that very first launch; every later start reuses the cache and is
instant. Nothing is installed and no admin rights are needed.

---

## 3. Build A — hardened PyInstaller (folder, zipped)

1. Put `t3r.py`, `build_exe.bat`, `version.txt`, `make_icon.py` in one folder.
2. Double-click `build_exe.bat` (or run it from a terminal).
3. Take `dist\t3r\` — **the whole folder**, zip it, share the zip.
   `t3r.exe` alone will not work; the `_internal` folder next to it
   contains the bundled Python runtime.

What the script does differently from a plain `pyinstaller --onefile`:

```
--onedir            self-extracting onefile exes are the #1 heuristic trigger
--noupx             UPX compression is malware-associated
--version-file      embeds company/product/copyright metadata
--icon              proper icon resource
--clean             fresh build, no stale cache
latest pyinstaller  old bootloaders sit on AV blocklists
--exclude-module plyer  t3r no longer uses it; smaller bundle
```

## 4. Build B — Nuitka onefile (single exe, recommended for releases)

Nuitka translates Python to C and compiles a real native binary — no
PyInstaller bootloader, no UPX — and `build_nuitka.bat` wraps it into
ONE `t3r.exe`:

1. Put all the kit files in one folder.
2. Run `build_nuitka.bat`. The first run downloads a C compiler and
   takes 5–15 minutes; later builds are faster.
3. Take `dist_onefile\t3r.exe` — that single file is your release.
   Upload it directly to GitHub Releases.

What the script uses:

```
--onefile                   one exe, nothing else next to it
--onefile-tempdir-spec=...  extract once into a cached folder, so later
                            launches are instant instead of re-unpacking
--enable-plugin=tk-inter    bundles the Tcl/Tk runtime for the UI
--windows-console-mode=disable  no console window (GUI app)
--windows-icon-from-ico     proper icon resource
--company-name / --product-name / --file-version / --product-version /
--file-description / --copyright   embedded metadata (AV heuristics
                            penalize unsigned exes without it)
--assume-yes-for-downloads  auto-fetches the C compiler on first build
```

If your antivirus still complains about the Nuitka build, skip straight
to section 5 — with Nuitka the report almost always sticks, because the
binary is a genuine compiled application.

---

## 5. Reporting the false positive to Microsoft (the real fix)

This is what clears the flag for *everyone who downloads your exe*, not
just you. It is free and usually processed within 1–3 business days.

1. Build the exe (either script above).
2. Go to **https://www.microsoft.com/en-us/wdsi/filesubmission** and
   sign in with any Microsoft account.
3. Choose the **"Software developer"** option — developers get an
   analyst-reviewed submission, end users get an automated scan.
4. Fill in:
   - Company name: anything you like, e.g. `t3r project`
   - Product name: `t3r Eye Break Timer`
   - Product version / file version: `1.3.0.0` (must match the build)
   - File: upload `t3r.exe` (or the zip of the whole dist folder)
5. Under "Please describe..." state it is a **false positive** on an
   open-source-style utility, include the exact detection name Defender
   showed (e.g. `Trojan:Win32/Wacatac!ml`), and mention it is built with
   PyInstaller/Nuitka without UPX. Tick the box requesting
   **whitelisting for future versions**.
6. You get a ticket number by mail; when Microsoft pushes the fix, a
   Defender update clears the detection worldwide.

While you wait — on your own machine:

- **Defender**: Windows Security → Virus & threat protection →
  Protection history → find the t3r detection → Actions → **Allow**.
  Or add an exclusion folder for your dist folder.
- **Other AV** (Avast/AVG/Kaspersky…) have similar developer
  whitelisting forms; see their sites if your users report flags.

### Two extra notes

- **SmartScreen vs Defender are different.** This whole section fixes
  Defender ("virus found"). SmartScreen's "Windows protected your PC"
  on *downloaded* files is reputation-based; it fades for files that
  get downloaded often, and disappears with a code-signing certificate.
- **If you ever want zero warnings**, buy an **EV code-signing
  certificate** (identity-validated, ~$100–400/yr; CA/Sectigo/DigiCert).
  EV signatures are instantly trusted by SmartScreen. Ordinary OV certs
  help too but need to build reputation. Then add one line to
  `build_exe.bat` after the PyInstaller step:
  `signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f mycert.pfx /p PASS dist\t3r\t3r.exe`
  (skip this if you stay with the free fixes — that's fine).

---

## 6. Everyday use

- Settings live in `%APPDATA%\t3r\config.json`. You can pre-seed or
  script it; invalid values are clamped automatically.
  Keys: `work_minutes` (1–120), `pre_break_secs` (1–30),
  `break_seconds` (5–90), `sound_enabled`, `sound_volume` (0–100),
  `autostart`, `start_minimized`.
- CLI: `t3r.exe --minimized` starts in the tray (this is what the
  "Start on login" autostart entry uses); `--version` prints the
  version.
- During a break: `Esc` or `Space` skips.
- Closing the window hides to tray (timer keeps running); real exit is
  in the tray menu → Exit.

## 7. Building from source (no packaging)

```
pip install pillow pystray
python t3r.py
```

The file also runs on Linux/macOS (no registry autostart; sounds are
Windows-only).
