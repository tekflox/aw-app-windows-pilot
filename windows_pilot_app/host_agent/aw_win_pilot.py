"""aw-windows-pilot's host-side agent — the only code that runs ON Windows.

Uploaded to ``%USERPROFILE%\\.aw-windows-pilot\\aw_win_pilot.py`` by
``win_pilot_provision`` and invoked once per tool call as::

    py -3 <path>\\aw_win_pilot.py <verb> --args-b64 <base64(json)>

**Why a script instead of PowerShell one-liners.** Every command crosses
aw-remote-hosts' exec channel, and each crossing pays a real round trip
(~2s on a warm host, far worse on a cold PowerShell 5.1 — see the
aw-remote-host-windows notes). Enumerating windows, focusing one, clicking
and screenshotting as four one-liners is four round trips and four
quoting minefields; here it is one call into one process. Arguments arrive
**base64-encoded** for exactly that second reason: PowerShell escapes with
a backtick, not a backslash, and a JSON payload passed literally through
``-Command`` is a reliable way to lose a quote.

**Why it can drive the desktop at all.** aw-remote-host's exec on Windows
runs inside the logged-in user's *interactive* session (verified on
DESKTOP-DRMKFBT: ``SessionId=1``, same session as ``explorer.exe``). A
process in session 0 — the shape a Windows *service* would give you — has
no desktop, and every screenshot below would come back black while every
input call silently did nothing. ``pilot_status`` reports the session id
precisely so that failure is loud instead of mysterious.

**Dependencies are deliberately thin**: Pillow for the screen grab and
``playwright`` for the browser half. Everything else — window enumeration,
focus, move/resize, mouse, keyboard, clipboard — is ``ctypes`` against
``user32``/``kernel32``, which is always present. Fewer wheels to install
on someone's laptop is fewer ways for provisioning to half-succeed.

Output contract: a single JSON object on stdout, preceded by the marker
line ``<<<AW_WIN_PILOT_JSON>>>``. Windows loves to decorate stdout (pip
warnings, a stray BOM, a DLL notice), and the marker lets the caller take
only what this script actually returned.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

VERSION = "0.1.2"
MARKER = "<<<AW_WIN_PILOT_JSON>>>"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ---------------------------------------------------------------------------
# DPI
# ---------------------------------------------------------------------------

def _make_dpi_aware() -> str:
    """Opt into per-monitor DPI awareness before anything reads a coordinate.

    Without this, Windows lies to a non-aware process: on a 200%-scaled
    display (a Surface, most modern laptops) ``GetWindowRect`` reports
    virtualised logical pixels while the screen grab comes back at the real
    physical resolution. Screenshot and click coordinates then disagree by
    exactly the scale factor, and every click lands in the wrong half of the
    screen — the kind of bug that reads as "the agent can't aim" rather than
    "the process forgot to declare DPI awareness".
    """
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        return "per-monitor"
    except Exception:
        try:
            user32.SetProcessDPIAware()
            return "system"
        except Exception:
            return "none"


# ---------------------------------------------------------------------------
# Window enumeration / manipulation
# ---------------------------------------------------------------------------

SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE, SW_SHOW = 6, 3, 9, 5
WM_CLOSE = 0x0010

_ENUM_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_rect(hwnd: int) -> dict:
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {"x": rect.left, "y": rect.top,
            "width": rect.right - rect.left, "height": rect.bottom - rect.top}


def _window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_name(pid: int) -> str:
    """Best-effort image name for a pid, via the always-present Toolhelp API.

    psutil would be one more wheel to install for one string, and tasklist
    would be one more subprocess per window.
    """
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return ""
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return ""
        while True:
            if entry.th32ProcessID == pid:
                return entry.szExeFile
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return ""
    finally:
        kernel32.CloseHandle(snapshot)


def _is_cloaked(hwnd: int) -> bool:
    """True for windows the shell hides but never destroys.

    Every UWP/store app leaves a permanently-invisible ``ApplicationFrameHost``
    shell behind (Mail, Settings, Calculator — even when they were never
    opened), and ``IsWindowVisible`` happily reports those as visible. Listing
    them means an agent picks "Settings" off the list and focuses a window
    that isn't on screen. DWM's cloak bit is the only reliable discriminator.
    """
    DWMWA_CLOAKED = 14
    cloaked = ctypes.c_int(0)
    try:
        ctypes.WinDLL("dwmapi").DwmGetWindowAttribute(
            wt.HWND(hwnd), ctypes.c_uint(DWMWA_CLOAKED),
            ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    except Exception:
        return False
    return bool(cloaked.value)


def list_windows(args: dict) -> dict:
    include_all = bool(args.get("include_all"))
    match = (args.get("title_contains") or "").lower()
    foreground = user32.GetForegroundWindow()
    found: list[dict] = []

    def _cb(hwnd, _lparam):
        if not include_all:
            if not user32.IsWindowVisible(hwnd):
                return True
            if _is_cloaked(hwnd):
                return True
        title = _window_title(hwnd)
        if not include_all and not title:
            return True
        if match and match not in title.lower():
            return True
        rect = _window_rect(hwnd)
        pid = _window_pid(hwnd)
        found.append({
            "hwnd": int(hwnd), "title": title, "class": _window_class(hwnd),
            "pid": pid, "process": _process_name(pid), **rect,
            "minimized": bool(user32.IsIconic(hwnd)),
            "maximized": bool(user32.IsZoomed(hwnd)),
            "foreground": int(hwnd) == int(foreground),
        })
        return True

    user32.EnumWindows(_ENUM_PROC(_cb), 0)
    found.sort(key=lambda w: (not w["foreground"], w["title"].lower()))
    return {"count": len(found), "windows": found}


def _resolve_hwnd(args: dict) -> int:
    """Accept either an explicit hwnd or a title substring.

    Titles are what an agent actually has after a screenshot; hwnds are what
    a previous ``list_windows`` returned. Supporting both means the caller
    never has to make a round trip purely to translate one into the other.
    """
    hwnd = args.get("hwnd")
    if hwnd:
        return int(hwnd)
    title = (args.get("title") or "").strip()
    if not title:
        raise ValueError("pass either hwnd or title")
    matches = list_windows({"title_contains": title})["windows"]
    if not matches:
        raise ValueError(f"no visible window whose title contains {title!r}")
    return int(matches[0]["hwnd"])


def focus_window(args: dict) -> dict:
    hwnd = _resolve_hwnd(args)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # AttachThreadInput is the documented way around Windows' foreground lock:
    # SetForegroundWindow is refused outright for a process that does not own
    # the current foreground window, and it fails *silently* (returns 0, no
    # error) — so without this, focus_window reports success and the click
    # that follows lands in whatever was already on top.
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    current_thread = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(current_thread, target_thread, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        user32.AttachThreadInput(current_thread, target_thread, False)
    time.sleep(0.15)
    ok = int(user32.GetForegroundWindow()) == int(hwnd)
    return {"hwnd": hwnd, "title": _window_title(hwnd), "focused": ok,
            **_window_rect(hwnd)}


def window_action(args: dict) -> dict:
    hwnd = _resolve_hwnd(args)
    action = (args.get("action") or "").lower()
    if action == "minimize":
        user32.ShowWindow(hwnd, SW_MINIMIZE)
    elif action == "maximize":
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
    elif action == "restore":
        user32.ShowWindow(hwnd, SW_RESTORE)
    elif action == "close":
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return {"hwnd": hwnd, "action": action, "closed": True}
    elif action == "move":
        rect = _window_rect(hwnd)
        user32.MoveWindow(
            hwnd, int(args.get("x", rect["x"])), int(args.get("y", rect["y"])),
            int(args.get("width", rect["width"])),
            int(args.get("height", rect["height"])), True)
    else:
        raise ValueError(f"unknown action {action!r} — "
                         "use minimize/maximize/restore/close/move")
    time.sleep(0.1)
    return {"hwnd": hwnd, "action": action, "title": _window_title(hwnd),
            **_window_rect(hwnd)}


# ---------------------------------------------------------------------------
# Input — SendInput
# ---------------------------------------------------------------------------

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF = {
    "move": 0x0001, "absolute": 0x8000, "wheel": 0x0800, "hwheel": 0x01000,
    "left_down": 0x0002, "left_up": 0x0004,
    "right_down": 0x0008, "right_up": 0x0010,
    "middle_down": 0x0020, "middle_up": 0x0040,
}
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_EXTENDEDKEY = 0x0002, 0x0004, 0x0001

VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
    "win": 0x5B, "windows": 0x5B, "meta": 0x5B, "capslock": 0x14,
    "printscreen": 0x2C, "menu": 0x5D,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}
# Keys that live on the extended part of the keyboard. Sent without
# KEYEVENTF_EXTENDEDKEY, the arrows and navigation cluster are interpreted as
# their numpad twins by some apps — Down arrow types "2" in a spreadsheet.
EXTENDED = {0x26, 0x28, 0x25, 0x27, 0x24, 0x23, 0x21, 0x22, 0x2D, 0x2E, 0x5B, 0x5D, 0x2C}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _send(*inputs: _INPUT) -> None:
    array = (_INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput delivered {sent}/{len(inputs)} events "
                      f"(error {ctypes.get_last_error()}) — a UAC-elevated "
                      "window in the foreground blocks input from a "
                      "non-elevated process")


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    return _INPUT(type=INPUT_MOUSE, u=_INPUTUNION(
        mi=_MOUSEINPUT(dx, dy, data, flags, 0, None)))


def _screen_size() -> tuple[int, int]:
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    return (user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def _move_to(x: int, y: int) -> None:
    """Absolute cursor move.

    SetCursorPos rather than a MOUSEEVENTF_ABSOLUTE SendInput: the absolute
    flag takes a 0..65535 normalised coordinate over the *primary* monitor
    only, so on a multi-monitor desktop every point on the second screen is
    unreachable. SetCursorPos takes real virtual-desktop pixels.
    """
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.02)


def click(args: dict) -> dict:
    button = (args.get("button") or "left").lower()
    if button not in ("left", "right", "middle"):
        raise ValueError(f"unknown button {button!r}")
    count = max(1, int(args.get("count") or 1))
    x, y = args.get("x"), args.get("y")

    hwnd = args.get("hwnd") or args.get("title")
    if hwnd and x is not None and y is not None and args.get("relative"):
        # Coordinates read off a per-window screenshot are window-relative;
        # SetCursorPos wants virtual-desktop pixels.
        rect = _window_rect(_resolve_hwnd(args))
        x, y = rect["x"] + int(x), rect["y"] + int(y)

    if x is not None and y is not None:
        _move_to(int(x), int(y))
    for _ in range(count):
        _send(_mouse(MOUSEEVENTF[f"{button}_down"]),
              _mouse(MOUSEEVENTF[f"{button}_up"]))
        if count > 1:
            time.sleep(0.06)  # inside the default 500ms double-click window
    point = wt.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return {"clicked": True, "button": button, "count": count,
            "x": point.x, "y": point.y}


def move_mouse(args: dict) -> dict:
    _move_to(int(args["x"]), int(args["y"]))
    return {"x": int(args["x"]), "y": int(args["y"])}


def scroll(args: dict) -> dict:
    amount = int(args.get("amount") or -3)
    horizontal = bool(args.get("horizontal"))
    if args.get("x") is not None and args.get("y") is not None:
        _move_to(int(args["x"]), int(args["y"]))
    flag = MOUSEEVENTF["hwheel" if horizontal else "wheel"]
    for _ in range(abs(amount)):
        _send(_mouse(flag, data=120 if amount > 0 else -120))
        time.sleep(0.02)
    return {"scrolled": amount, "horizontal": horizontal}


def type_text(args: dict) -> dict:
    """Type a literal string, one UTF-16 code unit at a time.

    KEYEVENTF_UNICODE rather than VkKeyScan-per-character: the latter maps
    through the *active keyboard layout*, so a Portuguese "ã" or "ç" comes
    out as whatever key happens to sit there on a US layout — and emoji or
    CJK are simply unreachable. Unicode injection bypasses the layout
    entirely. Surrogate pairs fall out naturally because each unit is sent
    on its own.
    """
    text = args.get("text") or ""
    delay = float(args.get("delay_ms") or 8) / 1000.0
    raw = text.encode("utf-16-le")
    units = [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]
    for code in units:
        _send(_INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(
            ki=_KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, None))))
        _send(_INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(
            ki=_KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None))))
        time.sleep(delay)
    if args.get("submit"):
        press_keys({"keys": "enter"})
    return {"typed": len(text), "code_units": len(units)}


def _vk_for(token: str) -> int:
    token = token.strip().lower()
    if token in VK:
        return VK[token]
    if len(token) == 1:
        code = user32.VkKeyScanW(ctypes.c_wchar(token))
        if code == -1:
            raise ValueError(f"key {token!r} is not on the active layout")
        return code & 0xFF
    raise ValueError(f"unknown key {token!r}")


def press_keys(args: dict) -> dict:
    """Press a chord like ``ctrl+shift+esc`` or a sequence like ``win r``.

    Whitespace separates chords, ``+`` joins the keys inside one — so
    ``"win+r"`` opens Run and ``"win r"`` taps Win then r, which are very
    different things.
    """
    spec = (args.get("keys") or "").strip()
    if not spec:
        raise ValueError("keys is required, e.g. 'ctrl+c' or 'win+r'")
    pressed = []
    for chord in spec.split():
        codes = [_vk_for(part) for part in chord.split("+") if part]
        for code in codes:
            flags = KEYEVENTF_EXTENDEDKEY if code in EXTENDED else 0
            _send(_INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(
                ki=_KEYBDINPUT(code, 0, flags, 0, None))))
        for code in reversed(codes):  # release in reverse: modifiers last
            flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if code in EXTENDED else 0)
            _send(_INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(
                ki=_KEYBDINPUT(code, 0, flags, 0, None))))
        pressed.append(chord)
        time.sleep(0.05)
    return {"pressed": pressed}


# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------

def screenshot(args: dict) -> dict:
    from PIL import ImageGrab

    out = args.get("out_path") or os.path.join(
        os.path.expanduser("~"), ".aw-windows-pilot", "shot.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    target = None
    region = args.get("region")
    if args.get("hwnd") or args.get("title"):
        hwnd = _resolve_hwnd(args)
        if args.get("focus", True):
            focus_window({"hwnd": hwnd})
        rect = _window_rect(hwnd)
        if region:
            # A region given alongside a window is read as an offset INSIDE
            # that window, which is how a caller means it ("the top strip of
            # Edge"). Silently ignoring one of two arguments the caller
            # deliberately passed is worse than either interpretation.
            origin_x, origin_y = rect["x"] + int(region["x"]), rect["y"] + int(region["y"])
            target = (origin_x, origin_y,
                      min(origin_x + int(region["width"]), rect["x"] + rect["width"]),
                      min(origin_y + int(region["height"]), rect["y"] + rect["height"]))
        else:
            target = (rect["x"], rect["y"],
                      rect["x"] + rect["width"], rect["y"] + rect["height"])
    elif region:
        target = (int(region["x"]), int(region["y"]),
                  int(region["x"]) + int(region["width"]),
                  int(region["y"]) + int(region["height"]))

    image = ImageGrab.grab(bbox=target, all_screens=target is None)
    if image.mode != "RGB":
        image = image.convert("RGB")

    # Downscale before writing, not after: a 3000×2000 Surface screenshot is
    # ~4 MB of PNG, and every byte of it crosses the tunnel to the workspace
    # and then the model's context. 1600px wide is still comfortably legible
    # for reading UI text.
    max_width = int(args.get("max_width") or 1600)
    scale = 1.0
    if max_width and image.width > max_width:
        scale = max_width / image.width
        image = image.resize((max_width, max(1, round(image.height * scale))))

    image.save(out, "PNG", optimize=True)
    width, height = _screen_size()
    return {"path": out, "bytes": os.path.getsize(out),
            "width": image.width, "height": image.height,
            "scale": round(scale, 4),
            "captured_region": list(target) if target else [0, 0, width, height],
            "note": ("Coordinates in this image are scaled by "
                     f"{round(scale, 4)} — divide by it before clicking."
                     if scale != 1.0 else
                     "Image is 1:1 with screen pixels; click these coordinates directly.")}


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

def clipboard(args: dict) -> dict:
    """Read or write the clipboard through PowerShell.

    The Win32 clipboard API demands a message pump and careful global-memory
    handling from whichever thread opens it; ``Get-Clipboard`` is two orders
    of magnitude less code for a feature that is mostly a convenience.
    """
    text = args.get("text")
    if text is None:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        return {"text": (result.stdout or "").rstrip("\r\n")}
    payload = base64.b64encode(text.encode("utf-8")).decode()
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString("
         f"[Convert]::FromBase64String('{payload}')))"],
        capture_output=True, text=True)
    return {"set": len(text)}


# ---------------------------------------------------------------------------
# Browser — Playwright over CDP
# ---------------------------------------------------------------------------

DEFAULT_CDP_PORT = 9222


def _browser_paths(name: str) -> list[str]:
    local = os.environ.get("LOCALAPPDATA", "")
    pf, pf86 = os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")
    if name == "edge":
        return [os.path.join(p, r"Microsoft\Edge\Application\msedge.exe")
                for p in (pf86, pf, local) if p]
    return [os.path.join(p, r"Google\Chrome\Application\chrome.exe")
            for p in (pf, pf86, local) if p]


def _cdp_alive(port: int, timeout: float = 1.5) -> dict | None:
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def browser_launch(args: dict) -> dict:
    """Start (or reuse) a CDP-enabled browser on the user's desktop.

    **The profile question, which decides whether this is useful at all.**
    Chrome 136+ and current Edge refuse ``--remote-debugging-port`` when the
    browser is pointed at its *default* user-data-dir — a deliberate change
    after infostealer malware started attaching to people's signed-in
    browsers. So there is no supported way to bolt CDP onto the exact Chrome
    window the user already has open.

    What this does instead is give the pilot its own **persistent** profile
    directory on the user's machine (default
    ``%LOCALAPPDATA%\\aw-windows-pilot\\browser-profile``). It is a real
    browser window on the real desktop, and its cookies and sessions survive
    across calls and reboots — so the user signs into a site once, in that
    window, and the agent is signed in from then on. Pass
    ``user_data_dir`` to point at some other profile you keep for this.
    """
    port = int(args.get("port") or DEFAULT_CDP_PORT)
    existing = _cdp_alive(port)
    if existing and not args.get("force_restart"):
        return {"reused": True, "port": port, "browser": existing.get("Browser"),
                "user_data_dir": args.get("user_data_dir") or "(already running)"}

    channel = (args.get("browser") or "edge").lower()
    exe = next((p for p in _browser_paths(channel) if os.path.exists(p)), None)
    if not exe:
        other = "chrome" if channel == "edge" else "edge"
        exe = next((p for p in _browser_paths(other) if os.path.exists(p)), None)
        if not exe:
            raise RuntimeError("neither Edge nor Chrome found in the usual "
                               "install locations — pass browser_path")
        channel = other
    exe = args.get("browser_path") or exe

    profile = args.get("user_data_dir") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "aw-windows-pilot", "browser-profile")
    os.makedirs(profile, exist_ok=True)

    cmd = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
           "--no-first-run", "--no-default-browser-check",
           "--remote-allow-origins=*"]
    if args.get("url"):
        cmd.append(args["url"])
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: the exec channel closes its
    # job as soon as this script returns, and a child in the same group would
    # be torn down with it — the browser has to outlive the call that started
    # it.
    subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200,
                     close_fds=True)

    deadline = time.time() + float(args.get("wait_s") or 25)
    while time.time() < deadline:
        info = _cdp_alive(port)
        if info:
            return {"reused": False, "port": port, "browser": info.get("Browser"),
                    "executable": exe, "channel": channel, "user_data_dir": profile}
        time.sleep(0.5)
    raise RuntimeError(
        f"{channel} was launched but never opened CDP on port {port}. The "
        "usual cause is an already-running instance of that browser sharing "
        "the same profile — close it, or use a different user_data_dir.")


def _page(playwright_browser, args: dict):
    """The page a tool acts on: an explicit index, else the last one opened.

    "Last" rather than "first" because a click that opens a new tab should
    leave the agent looking at the tab it just opened.
    """
    contexts = playwright_browser.contexts
    pages = [p for c in contexts for p in c.pages]
    if not pages:
        return contexts[0].new_page() if contexts else playwright_browser.new_page()
    index = args.get("tab")
    if index is not None:
        try:
            return pages[int(index)]
        except IndexError:
            raise ValueError(f"tab {index} does not exist ({len(pages)} open)")
    return pages[-1]


def _with_browser(args, fn):
    from playwright.sync_api import sync_playwright

    port = int(args.get("port") or DEFAULT_CDP_PORT)
    if not _cdp_alive(port):
        raise RuntimeError(
            f"no CDP browser on port {port} — call win_browser_launch first")
    with sync_playwright() as pw:
        # connect_over_cdp attaches to the already-running browser rather than
        # starting one, which is the whole point: the window stays on the
        # user's desktop with their session in it, and nothing here needs
        # Playwright's own downloaded browsers.
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        try:
            return fn(browser)
        finally:
            browser.close()  # closes the CDP socket, NOT the user's browser


_SNAPSHOT_JS = r"""
() => {
  const out = [];
  let n = 0;
  const sel = 'a,button,input,select,textarea,[role=button],[role=link],' +
              '[role=textbox],[role=checkbox],[role=tab],[onclick],[contenteditable=true]';
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const ref = 'e' + (++n);
    el.setAttribute('data-aw-ref', ref);
    const label = (el.getAttribute('aria-label') || el.value ||
                   el.innerText || el.getAttribute('placeholder') ||
                   el.getAttribute('title') || '').trim().slice(0, 120);
    out.push({ref, tag: el.tagName.toLowerCase(),
              type: el.getAttribute('type') || undefined,
              role: el.getAttribute('role') || undefined,
              text: label,
              x: Math.round(r.x), y: Math.round(r.y),
              width: Math.round(r.width), height: Math.round(r.height)});
    if (n >= 250) break;
  }
  return {url: location.href, title: document.title,
          text: document.body ? document.body.innerText.slice(0, 6000) : '',
          elements: out};
}
"""


def browser_snapshot(args: dict) -> dict:
    """Text + interactive elements of the current page, each with a ref.

    Refs are stamped onto the DOM as ``data-aw-ref`` attributes, so
    ``browser_click({"ref": "e12"})`` resolves without the agent having to
    invent a CSS selector — and without this script holding any state
    between calls, which it cannot do (each call is a fresh process).
    """
    def _run(browser):
        page = _page(browser, args)
        return {"tab_count": sum(len(c.pages) for c in browser.contexts),
                **page.evaluate(_SNAPSHOT_JS)}
    return _with_browser(args, _run)


def browser_navigate(args: dict) -> dict:
    url = args.get("url")
    if not url:
        raise ValueError("url is required")

    def _run(browser):
        page = _page(browser, args)
        if args.get("new_tab"):
            page = (browser.contexts[0] if browser.contexts else browser).new_page()
        page.goto(url, wait_until=args.get("wait_until") or "domcontentloaded",
                  timeout=float(args.get("timeout_s") or 30) * 1000)
        return {"url": page.url, "title": page.title()}
    return _with_browser(args, _run)


def _locator(page, args: dict):
    if args.get("ref"):
        return page.locator(f"[data-aw-ref='{args['ref']}']")
    if args.get("selector"):
        return page.locator(args["selector"])
    if args.get("text"):
        return page.get_by_text(args["text"], exact=False).first
    raise ValueError("pass one of ref / selector / text")


def browser_click(args: dict) -> dict:
    def _run(browser):
        page = _page(browser, args)
        locator = _locator(page, args)
        locator.click(timeout=float(args.get("timeout_s") or 15) * 1000)
        page.wait_for_timeout(400)
        return {"clicked": True, "url": page.url, "title": page.title()}
    return _with_browser(args, _run)


def browser_type(args: dict) -> dict:
    def _run(browser):
        page = _page(browser, args)
        locator = _locator(page, args)
        timeout = float(args.get("timeout_s") or 15) * 1000
        if args.get("clear", True):
            locator.fill("", timeout=timeout)
        locator.type(args.get("text") or "", delay=25, timeout=timeout)
        if args.get("submit"):
            locator.press("Enter")
            page.wait_for_timeout(600)
        return {"typed": len(args.get("text") or ""), "url": page.url}
    return _with_browser(args, _run)


def browser_eval(args: dict) -> dict:
    def _run(browser):
        page = _page(browser, args)
        return {"result": page.evaluate(args.get("script") or "() => null")}
    return _with_browser(args, _run)


def browser_screenshot(args: dict) -> dict:
    out = args.get("out_path") or os.path.join(
        os.path.expanduser("~"), ".aw-windows-pilot", "page.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    def _run(browser):
        page = _page(browser, args)
        if args.get("ref") or args.get("selector"):
            _locator(page, args).screenshot(path=out)
        else:
            page.screenshot(path=out, full_page=bool(args.get("full_page")))
        return {"path": out, "bytes": os.path.getsize(out),
                "url": page.url, "title": page.title()}
    return _with_browser(args, _run)


def browser_tabs(args: dict) -> dict:
    action = (args.get("action") or "list").lower()

    def _run(browser):
        pages = [p for c in browser.contexts for p in c.pages]
        if action == "new":
            target = (browser.contexts[0] if browser.contexts else browser).new_page()
            if args.get("url"):
                target.goto(args["url"], wait_until="domcontentloaded")
            pages = [p for c in browser.contexts for p in c.pages]
        elif action == "close":
            pages[int(args.get("tab", len(pages) - 1))].close()
            pages = [p for c in browser.contexts for p in c.pages]
        elif action == "select":
            pages[int(args["tab"])].bring_to_front()
        elif action != "list":
            raise ValueError(f"unknown action {action!r}")
        return {"count": len(pages),
                "tabs": [{"index": i, "url": p.url, "title": p.title()}
                         for i, p in enumerate(pages)]}
    return _with_browser(args, _run)


# ---------------------------------------------------------------------------
# Status / provisioning
# ---------------------------------------------------------------------------

def _installed_version(distribution: str) -> str | None:
    """Installed version of a distribution, or None if it is not importable.

    Asks ``importlib.metadata`` rather than reading ``module.__version__``:
    ``playwright`` exposes no ``__version__``, so the attribute approach
    reports it as missing however correctly it is installed — which reads as
    a failed provision when nothing failed.
    """
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None
    except Exception:
        return None


def pilot_status(args: dict) -> dict:
    session = ctypes.c_ulong()
    kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(),
                                  ctypes.byref(session))
    width, height = _screen_size()
    interactive = bool(user32.GetForegroundWindow())
    return {
        "agent_version": VERSION,
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "user": os.environ.get("USERNAME", ""),
        "computer": os.environ.get("COMPUTERNAME", ""),
        "session_id": int(session.value),
        "has_desktop": interactive,
        "dpi_awareness": _DPI_MODE,
        "virtual_screen": {"width": width, "height": height},
        "deps": {"pillow": _installed_version("pillow"),
                 "playwright": _installed_version("playwright")},
        "cdp": _cdp_alive(int(args.get("port") or DEFAULT_CDP_PORT)),
        "warning": (None if interactive else
                    "No foreground window: this process has no desktop. Screen "
                    "capture will be black and input will go nowhere. The host "
                    "agent must run in the logged-in interactive session."),
    }


def provision(args: dict) -> dict:
    """Install the two wheels the tools need, into the user site-packages.

    ``--user`` on purpose: this is somebody's personal machine, not a
    container, and an app should not be writing into a system Python's
    site-packages or silently creating a venv the user did not ask for.
    """
    packages = ["pillow"]
    if args.get("browser", True):
        packages.append("playwright")
    cmd = [sys.executable, "-m", "pip", "install", "--user", "--upgrade",
           "--disable-pip-version-check", "--no-input", *packages]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=900)

    # Re-check from a FRESH interpreter, not from this one. Python computes
    # sys.path at startup, and on a first provision the per-user
    # site-packages directory did not exist yet — so the packages pip just
    # installed are genuinely unimportable *here*, and an in-process check
    # reports a perfectly successful install as `pillow: null`. Which is
    # exactly what the first live run of this app did.
    probe = subprocess.run(
        [sys.executable, "-c",
         "import json;from importlib.metadata import version,PackageNotFoundError\n"
         "def v(d):\n"
         "    try: return version(d)\n"
         "    except PackageNotFoundError: return None\n"
         "print(json.dumps({'pillow': v('pillow'), 'playwright': v('playwright')}))"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120)
    try:
        deps = json.loads(probe.stdout.strip().splitlines()[-1])
    except Exception:
        deps = {"pillow": None, "playwright": None}

    status = pilot_status({})
    status["deps"] = deps
    return {"command": " ".join(cmd), "exit_code": result.returncode,
            "stdout": (result.stdout or "")[-4000:],
            "stderr": (result.stderr or "")[-2000:],
            "status_after": status}


VERBS = {
    "pilot_status": pilot_status,
    "provision": provision,
    "list_windows": list_windows,
    "focus_window": focus_window,
    "window_action": window_action,
    "screenshot": screenshot,
    "click": click,
    "move_mouse": move_mouse,
    "scroll": scroll,
    "type_text": type_text,
    "press_keys": press_keys,
    "clipboard": clipboard,
    "browser_launch": browser_launch,
    "browser_navigate": browser_navigate,
    "browser_snapshot": browser_snapshot,
    "browser_click": browser_click,
    "browser_type": browser_type,
    "browser_eval": browser_eval,
    "browser_screenshot": browser_screenshot,
    "browser_tabs": browser_tabs,
}

_DPI_MODE = _make_dpi_aware()


def main() -> int:
    parser = argparse.ArgumentParser(prog="aw_win_pilot")
    parser.add_argument("verb", choices=sorted(VERBS))
    parser.add_argument("--args-b64", default="")
    parsed = parser.parse_args()

    raw = parsed.args_b64
    args = json.loads(base64.b64decode(raw).decode("utf-8")) if raw else {}

    try:
        payload = {"ok": True, "verb": parsed.verb, "result": VERBS[parsed.verb](args)}
    except Exception as exc:  # noqa: BLE001 — every failure is reported as data
        payload = {"ok": False, "verb": parsed.verb,
                   "error": f"{type(exc).__name__}: {exc}"}

    sys.stdout.write("\n" + MARKER + "\n")
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.write("\n")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
