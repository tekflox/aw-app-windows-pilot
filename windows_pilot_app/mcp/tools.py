"""The ``aw-windows-pilot`` tool surface: schemas + handlers.

Every handler is a thin translation of MCP arguments into one
:func:`host_agent.call`. The interesting logic lives on the Windows side
(``host_agent/aw_win_pilot.py``); what lives here is the tool *contract* —
the descriptions an agent reads to decide what to call, which is the part
that actually determines whether the app is usable.

Two conventions run through all of them:

* **``remote_host_id`` is accepted per call.** The app has a configured
  default, but a workspace can have several linked Windows boxes and a
  single task can legitimately touch two of them.
* **Screenshots come back as workspace files, not base64.** They stage on
  the host's disk and are pulled through the dedicated download route,
  because the exec channel truncates at 1 MiB without saying so. The tool
  returns a path the agent then reads with its own file tool.
"""
from __future__ import annotations

import json
import time
from typing import Callable

from . import host_agent, remote_host

DEFAULT_SCREENSHOT_DIR = host_agent.DEFAULT_SCREENSHOT_DIR

_config_resolver: Callable[[], dict] = lambda: {}


def set_config_resolver(resolver: Callable[[], dict]) -> None:
    global _config_resolver
    _config_resolver = resolver


def current_config() -> dict:
    try:
        return _config_resolver() or {}
    except Exception:
        return {}


def _common(args: dict) -> dict:
    """The per-call knobs every tool shares, resolved against config."""
    config = current_config()
    return {
        "host_override": (args.get("remote_host_id") or "").strip() or None,
        "python_exe": (args.get("python_exe")
                       or config.get("python_exe")
                       or host_agent.DEFAULT_PYTHON),
    }


def _text(payload: dict | list) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


_HOST_ARG = {
    "remote_host_id": {
        "type": "string",
        "description": "Optional — target a different linked Windows host for "
                       "this one call (id from `aw-workspace-cli remote-hosts "
                       "hosts`). Defaults to the app's configured host.",
    },
}


def _schema(name: str, description: str, properties: dict,
            required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {**properties, **_HOST_ARG},
            "required": required or [],
        },
    }


# ---------------------------------------------------------------------------
# Setup / status
# ---------------------------------------------------------------------------

def _handle_status(args: dict) -> str:
    common = _common(args)
    try:
        result = host_agent.call("pilot_status", {"port": args.get("port")},
                                 timeout=120, **common)
    except Exception as exc:  # noqa: BLE001 — an unprovisioned host is normal
        return _text({
            "ready": False,
            "error": str(exc),
            "next_step": "Run win_pilot_provision once against this host — it "
                         "uploads the pilot agent and installs Pillow and "
                         "Playwright into the user's Python.",
            "agent_version_expected": host_agent.agent_version(),
        })
    deps = result.get("deps") or {}
    return _text({
        "ready": bool(deps.get("pillow")) and result.get("has_desktop", False),
        "browser_ready": bool(deps.get("playwright")),
        **result,
    })


def _handle_provision(args: dict) -> str:
    common = _common(args)
    ensured = host_agent.ensure_agent(force=bool(args.get("force")), **common)
    installed = host_agent.call(
        "provision", {"browser": args.get("browser", True)},
        timeout=int(args.get("timeout_s") or 900), **common)
    status = installed.get("status_after") or {}
    deps = status.get("deps") or {}
    return _text({
        "agent_uploaded": ensured.get("uploaded", False),
        "agent_version": ensured.get("agent_version"),
        "pip_exit_code": installed.get("exit_code"),
        "deps": deps,
        "ready": bool(deps.get("pillow")) and status.get("has_desktop", False),
        "session_id": status.get("session_id"),
        "warning": status.get("warning"),
        "pip_output_tail": (installed.get("stdout") or "")[-1200:],
        "pip_errors": (installed.get("stderr") or "")[-600:],
    })


# ---------------------------------------------------------------------------
# Windows / desktop
# ---------------------------------------------------------------------------

def _handle_list_windows(args: dict) -> str:
    return _text(host_agent.call("list_windows", {
        "title_contains": args.get("title_contains"),
        "include_all": args.get("include_all", False),
    }, **_common(args)))


def _handle_focus_window(args: dict) -> str:
    return _text(host_agent.call("focus_window", {
        "hwnd": args.get("hwnd"), "title": args.get("title"),
    }, **_common(args)))


def _handle_window_action(args: dict) -> str:
    return _text(host_agent.call("window_action", {
        "hwnd": args.get("hwnd"), "title": args.get("title"),
        "action": args.get("action"),
        "x": args.get("x"), "y": args.get("y"),
        "width": args.get("width"), "height": args.get("height"),
    }, **_common(args)))


def _handle_screenshot(args: dict) -> str:
    common = _common(args)
    host_override = common["host_override"]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    remote_name = f"shot-{stamp}.png"
    remote_path = (f"{remote_host.home(host_override)}\\"
                   f"{host_agent.REMOTE_DIR}\\{remote_name}")

    result = host_agent.call("screenshot", {
        "out_path": remote_path,
        "hwnd": args.get("hwnd"), "title": args.get("title"),
        "region": args.get("region"), "focus": args.get("focus", True),
        "max_width": args.get("max_width"),
    }, timeout=180, **common)

    local_dir = current_config().get("screenshot_dir") or DEFAULT_SCREENSHOT_DIR
    local_path = args.get("local_path") or host_agent.local_capture_path(
        remote_name, local_dir)
    size = remote_host.download(result["path"], local_path,
                                host_override=host_override)
    return _text({
        "path": local_path, "bytes": size,
        "width": result.get("width"), "height": result.get("height"),
        "scale": result.get("scale"),
        "captured_region": result.get("captured_region"),
        "note": result.get("note"),
        "read_it": "Open this path with your file/image reader to see the "
                   "screen, then aim win_click at the coordinates you read "
                   "off it.",
    })


def _handle_click(args: dict) -> str:
    return _text(host_agent.call("click", {
        "x": args.get("x"), "y": args.get("y"),
        "button": args.get("button"), "count": args.get("count"),
        "hwnd": args.get("hwnd"), "title": args.get("title"),
        "relative": args.get("relative", False),
    }, **_common(args)))


def _handle_move_mouse(args: dict) -> str:
    return _text(host_agent.call("move_mouse", {
        "x": args["x"], "y": args["y"]}, **_common(args)))


def _handle_scroll(args: dict) -> str:
    return _text(host_agent.call("scroll", {
        "amount": args.get("amount"), "horizontal": args.get("horizontal", False),
        "x": args.get("x"), "y": args.get("y"),
    }, **_common(args)))


def _handle_type(args: dict) -> str:
    return _text(host_agent.call("type_text", {
        "text": args.get("text") or "", "submit": args.get("submit", False),
        "delay_ms": args.get("delay_ms"),
    }, timeout=int(args.get("timeout_s") or 180), **_common(args)))


def _handle_key(args: dict) -> str:
    return _text(host_agent.call("press_keys", {"keys": args.get("keys")},
                                 **_common(args)))


def _handle_clipboard(args: dict) -> str:
    return _text(host_agent.call("clipboard", {"text": args.get("text")},
                                 **_common(args)))


def _handle_run(args: dict) -> str:
    command = args.get("command")
    if not command:
        raise ValueError("command is required")
    common = _common(args)
    stdout, stderr, code = remote_host.exec(
        command, timeout=int(args.get("timeout_s") or 120),
        host_override=common["host_override"])
    return _text({"exit_code": code, "stdout": stdout[-8000:],
                  "stderr": stderr[-2000:]})


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

def _browser_args(args: dict) -> dict:
    config = current_config()
    return {
        "port": args.get("port") or config.get("cdp_port") or 9222,
        "tab": args.get("tab"),
    }


def _handle_browser_launch(args: dict) -> str:
    config = current_config()
    return _text(host_agent.call("browser_launch", {
        **_browser_args(args),
        "browser": args.get("browser") or config.get("browser") or "edge",
        "browser_path": args.get("browser_path") or config.get("browser_path"),
        "user_data_dir": args.get("user_data_dir") or config.get("browser_user_data_dir"),
        "url": args.get("url"),
        "force_restart": args.get("force_restart", False),
    }, timeout=int(args.get("timeout_s") or 180), **_common(args)))


def _handle_browser_navigate(args: dict) -> str:
    return _text(host_agent.call("browser_navigate", {
        **_browser_args(args), "url": args.get("url"),
        "new_tab": args.get("new_tab", False),
        "wait_until": args.get("wait_until"),
    }, timeout=180, **_common(args)))


def _handle_browser_snapshot(args: dict) -> str:
    return _text(host_agent.call("browser_snapshot", _browser_args(args),
                                 timeout=180, **_common(args)))


def _handle_browser_click(args: dict) -> str:
    return _text(host_agent.call("browser_click", {
        **_browser_args(args), "ref": args.get("ref"),
        "selector": args.get("selector"), "text": args.get("text"),
    }, timeout=180, **_common(args)))


def _handle_browser_type(args: dict) -> str:
    return _text(host_agent.call("browser_type", {
        **_browser_args(args), "ref": args.get("ref"),
        "selector": args.get("selector"), "text": args.get("text") or "",
        "submit": args.get("submit", False), "clear": args.get("clear", True),
    }, timeout=180, **_common(args)))


def _handle_browser_eval(args: dict) -> str:
    return _text(host_agent.call("browser_eval", {
        **_browser_args(args), "script": args.get("script"),
    }, timeout=180, **_common(args)))


def _handle_browser_tabs(args: dict) -> str:
    return _text(host_agent.call("browser_tabs", {
        **_browser_args(args), "action": args.get("action") or "list",
        "url": args.get("url"),
    }, timeout=180, **_common(args)))


def _handle_browser_screenshot(args: dict) -> str:
    common = _common(args)
    host_override = common["host_override"]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    remote_name = f"page-{stamp}.png"
    remote_path = (f"{remote_host.home(host_override)}\\"
                   f"{host_agent.REMOTE_DIR}\\{remote_name}")
    result = host_agent.call("browser_screenshot", {
        **_browser_args(args), "out_path": remote_path,
        "ref": args.get("ref"), "selector": args.get("selector"),
        "full_page": args.get("full_page", False),
    }, timeout=180, **common)
    local_dir = current_config().get("screenshot_dir") or DEFAULT_SCREENSHOT_DIR
    local_path = args.get("local_path") or host_agent.local_capture_path(
        remote_name, local_dir)
    size = remote_host.download(result["path"], local_path,
                                host_override=host_override)
    return _text({"path": local_path, "bytes": size, "url": result.get("url"),
                  "title": result.get("title")})


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_PY_ARG = {
    "python_exe": {
        "type": "string",
        "description": "Optional — how to invoke Python on the host, e.g. "
                       "`py -3.12` or a full path. Defaults to `py -3`.",
    },
}

TOOLS: list[dict] = [
    _schema(
        "win_pilot_status",
        "Is this Windows host ready to be piloted? Reports the logged-in user, "
        "the Windows SESSION ID (must be a real interactive session — from "
        "session 0 every screenshot is black and every click goes nowhere), "
        "screen size, which dependencies are installed, and whether a "
        "CDP browser is already running. Call this first when anything "
        "behaves oddly.",
        {**_PY_ARG, "port": {"type": "integer",
                             "description": "CDP port to probe (default 9222)."}},
    ),
    _schema(
        "win_pilot_provision",
        "One-time setup for a Windows host: uploads the pilot agent and pip-"
        "installs Pillow (screen capture) and Playwright (browser piloting) "
        "into the user's Python. Safe to re-run — it is how you upgrade the "
        "agent after the app updates. Takes a few minutes the first time.",
        {**_PY_ARG,
         "browser": {"type": "boolean",
                     "description": "Install Playwright too (default true). "
                                    "Set false for desktop piloting only."},
         "force": {"type": "boolean",
                   "description": "Re-upload the agent even if the host's copy "
                                  "is already current."},
         "timeout_s": {"type": "integer",
                       "description": "Seconds to allow pip (default 900)."}},
    ),
    _schema(
        "win_list_windows",
        "List the open top-level windows on the user's desktop — title, "
        "process, hwnd, position/size, and which one has focus. This is how "
        "you find out what is actually on screen before piloting anything; "
        "the hwnd or title it returns is what every other window tool takes.",
        {"title_contains": {"type": "string",
                            "description": "Only windows whose title contains "
                                           "this (case-insensitive)."},
         "include_all": {"type": "boolean",
                         "description": "Include hidden/untitled windows too. "
                                        "Off by default — the desktop is full "
                                        "of invisible shell windows."},
         **_PY_ARG},
    ),
    _schema(
        "win_focus_window",
        "Bring a window to the foreground (restoring it if minimized) so that "
        "keystrokes and clicks land in it. Always focus before win_type or "
        "win_key — input goes to whatever is focused, not to a window you "
        "merely named.",
        {"hwnd": {"type": "integer", "description": "From win_list_windows."},
         "title": {"type": "string",
                   "description": "Substring of the title, if you have no hwnd. "
                                  "First match wins."},
         **_PY_ARG},
    ),
    _schema(
        "win_window_action",
        "Minimize, maximize, restore, close, or move/resize a window. "
        "Maximizing before you screenshot is often the cheapest way to make a "
        "cramped app readable.",
        {"hwnd": {"type": "integer"}, "title": {"type": "string"},
         "action": {"type": "string",
                    "enum": ["minimize", "maximize", "restore", "close", "move"]},
         "x": {"type": "integer"}, "y": {"type": "integer"},
         "width": {"type": "integer"}, "height": {"type": "integer"},
         **_PY_ARG},
        ["action"],
    ),
    _schema(
        "win_screenshot",
        "Capture the Windows desktop (or one window, or a region) and save the "
        "PNG into this workspace, returning its path — read that path to "
        "actually see the screen. Downscaled to 1600px wide by default; the "
        "result includes the scale factor, so divide a coordinate you read off "
        "the image by it before passing it to win_click.",
        {"hwnd": {"type": "integer",
                  "description": "Capture just this window (it gets focused first)."},
         "title": {"type": "string",
                   "description": "Capture the window whose title contains this."},
         "region": {"type": "object",
                    "description": "Capture a rectangle: {x, y, width, height}. "
                                   "In screen pixels on its own; relative to "
                                   "the named window when combined with "
                                   "hwnd/title.",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                                   "width": {"type": "integer"},
                                   "height": {"type": "integer"}}},
         "focus": {"type": "boolean",
                   "description": "Focus the target window before capturing "
                                  "(default true). Set false to photograph a "
                                  "background window without disturbing the user."},
         "max_width": {"type": "integer",
                       "description": "Downscale to this width (default 1600). "
                                      "Pass 0 for full resolution."},
         "local_path": {"type": "string",
                        "description": "Absolute path to write instead of the "
                                       "app's screenshot directory."},
         **_PY_ARG},
    ),
    _schema(
        "win_click",
        "Click at a screen coordinate — the coordinates you read off a "
        "win_screenshot (remember the scale factor). Supports left/right/middle "
        "and double-clicks.",
        {"x": {"type": "integer"}, "y": {"type": "integer"},
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "count": {"type": "integer", "description": "2 for a double-click."},
         "hwnd": {"type": "integer"}, "title": {"type": "string"},
         "relative": {"type": "boolean",
                      "description": "Treat x/y as relative to the named "
                                     "window's top-left instead of the screen."},
         **_PY_ARG},
    ),
    _schema(
        "win_move_mouse",
        "Move the mouse pointer without clicking — for hovers that reveal a "
        "menu or tooltip before you screenshot.",
        {"x": {"type": "integer"}, "y": {"type": "integer"}, **_PY_ARG},
        ["x", "y"],
    ),
    _schema(
        "win_scroll",
        "Scroll the wheel. Negative scrolls down (the usual direction for "
        "reading further), positive scrolls up.",
        {"amount": {"type": "integer", "description": "Notches; default -3."},
         "horizontal": {"type": "boolean"},
         "x": {"type": "integer", "description": "Point at this first."},
         "y": {"type": "integer"}, **_PY_ARG},
    ),
    _schema(
        "win_type",
        "Type literal text into whatever window has focus. Sent as Unicode, so "
        "accented and non-Latin characters arrive correctly regardless of the "
        "machine's keyboard layout. Focus the target window first.",
        {"text": {"type": "string"},
         "submit": {"type": "boolean", "description": "Press Enter afterwards."},
         "delay_ms": {"type": "integer",
                      "description": "Per-character delay (default 8ms). Raise "
                                     "it for apps that drop fast input."},
         "timeout_s": {"type": "integer"},
         **_PY_ARG},
        ["text"],
    ),
    _schema(
        "win_key",
        "Press keys or a chord: 'enter', 'ctrl+c', 'alt+tab', 'win+r', "
        "'ctrl+shift+esc'. Space-separated groups are pressed in sequence, so "
        "'win+r' opens Run whereas 'win r' taps Win then r.",
        {"keys": {"type": "string"}, **_PY_ARG},
        ["keys"],
    ),
    _schema(
        "win_clipboard",
        "Read the Windows clipboard, or write to it by passing text. Writing "
        "then pressing ctrl+v is far more reliable than typing a long string "
        "character by character.",
        {"text": {"type": "string",
                  "description": "Omit to read; provide to write."},
         **_PY_ARG},
    ),
    _schema(
        "win_run",
        "Run a PowerShell command on the Windows host and return its output. "
        "The escape hatch for anything the piloting tools do not cover — "
        "launching an app, querying a registry key, listing a directory.",
        {"command": {"type": "string"},
         "timeout_s": {"type": "integer", "description": "Default 120."}},
        ["command"],
    ),
    # -- browser ------------------------------------------------------------
    _schema(
        "win_browser_launch",
        "Open (or reuse) a Playwright-controllable browser window on the "
        "user's desktop. It uses a PERSISTENT profile directory, so logins and "
        "cookies survive across calls and reboots — sign in once in that "
        "window and the agent stays signed in. Call this before any other "
        "win_browser_* tool.",
        {"browser": {"type": "string", "enum": ["edge", "chrome"],
                     "description": "Default edge; falls back to whichever is "
                                    "installed."},
         "browser_path": {"type": "string",
                          "description": "Full path to the browser exe, if it "
                                         "is somewhere unusual."},
         "user_data_dir": {"type": "string",
                           "description": "Profile directory on the host. "
                                          "Defaults to the app's own persistent "
                                          "profile under %LOCALAPPDATA%."},
         "url": {"type": "string", "description": "Open this page on launch."},
         "port": {"type": "integer", "description": "CDP port (default 9222)."},
         "force_restart": {"type": "boolean"},
         "timeout_s": {"type": "integer"},
         **_PY_ARG},
    ),
    _schema(
        "win_browser_navigate",
        "Navigate the piloted browser's active tab (or a new one) to a URL. "
        "Follow it with win_browser_snapshot to read the page that loaded.",
        {"url": {"type": "string"}, "new_tab": {"type": "boolean"},
         "tab": {"type": "integer", "description": "Act on this tab index."},
         "wait_until": {"type": "string",
                        "enum": ["load", "domcontentloaded", "networkidle"]},
         "port": {"type": "integer"}, **_PY_ARG},
        ["url"],
    ),
    _schema(
        "win_browser_snapshot",
        "Read the current page: its URL, title, visible text, and every "
        "interactive element with a stable `ref`. Pass a ref to "
        "win_browser_click / win_browser_type instead of guessing a CSS "
        "selector. Much cheaper and more reliable than screenshotting the page.",
        {"tab": {"type": "integer"}, "port": {"type": "integer"}, **_PY_ARG},
    ),
    _schema(
        "win_browser_click",
        "Click an element in the browser, by `ref` (from win_browser_snapshot), "
        "a CSS `selector`, or visible `text`.",
        {"ref": {"type": "string"}, "selector": {"type": "string"},
         "text": {"type": "string"}, "tab": {"type": "integer"},
         "port": {"type": "integer"}, **_PY_ARG},
    ),
    _schema(
        "win_browser_type",
        "Type into a browser field identified by `ref`, `selector` or `text`, "
        "optionally submitting with Enter.",
        {"ref": {"type": "string"}, "selector": {"type": "string"},
         "text": {"type": "string"}, "submit": {"type": "boolean"},
         "clear": {"type": "boolean", "description": "Clear the field first "
                                                     "(default true)."},
         "tab": {"type": "integer"}, "port": {"type": "integer"}, **_PY_ARG},
        ["text"],
    ),
    _schema(
        "win_browser_eval",
        "Evaluate a JavaScript function in the page, e.g. "
        "\"() => document.title\". Returns whatever it returns.",
        {"script": {"type": "string"}, "tab": {"type": "integer"},
         "port": {"type": "integer"}, **_PY_ARG},
        ["script"],
    ),
    _schema(
        "win_browser_tabs",
        "List, open, close or switch tabs in the piloted browser. Tab indices "
        "from `list` are what the `tab` argument on the other browser tools "
        "takes; without one they act on the most recently opened tab.",
        {"action": {"type": "string", "enum": ["list", "new", "close", "select"]},
         "tab": {"type": "integer"}, "url": {"type": "string"},
         "port": {"type": "integer"}, **_PY_ARG},
    ),
    _schema(
        "win_browser_screenshot",
        "Screenshot the browser page (or one element) and save it into this "
        "workspace, returning the path.",
        {"ref": {"type": "string"}, "selector": {"type": "string"},
         "full_page": {"type": "boolean"}, "tab": {"type": "integer"},
         "local_path": {"type": "string"}, "port": {"type": "integer"},
         **_PY_ARG},
    ),
]

HANDLERS: dict[str, Callable[[dict], str]] = {
    "win_pilot_status": _handle_status,
    "win_pilot_provision": _handle_provision,
    "win_list_windows": _handle_list_windows,
    "win_focus_window": _handle_focus_window,
    "win_window_action": _handle_window_action,
    "win_screenshot": _handle_screenshot,
    "win_click": _handle_click,
    "win_move_mouse": _handle_move_mouse,
    "win_scroll": _handle_scroll,
    "win_type": _handle_type,
    "win_key": _handle_key,
    "win_clipboard": _handle_clipboard,
    "win_run": _handle_run,
    "win_browser_launch": _handle_browser_launch,
    "win_browser_navigate": _handle_browser_navigate,
    "win_browser_snapshot": _handle_browser_snapshot,
    "win_browser_click": _handle_browser_click,
    "win_browser_type": _handle_browser_type,
    "win_browser_eval": _handle_browser_eval,
    "win_browser_tabs": _handle_browser_tabs,
    "win_browser_screenshot": _handle_browser_screenshot,
}

assert {t["name"] for t in TOOLS} == set(HANDLERS), (
    "every declared tool needs a handler and vice versa")

__all__ = ["TOOLS", "HANDLERS", "set_config_resolver", "current_config",
           "DEFAULT_SCREENSHOT_DIR"]
