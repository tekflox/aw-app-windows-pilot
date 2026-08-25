"""The bridge between an MCP tool call here and ``aw_win_pilot.py`` there.

One tool call becomes exactly one exec round trip: this module makes sure
the host-side agent is present and current, then invokes a single verb and
parses the JSON it prints back.

Three decisions worth knowing about before changing anything here:

**Arguments travel as base64.** They cross a PowerShell ``-Command`` string,
and PowerShell escapes with a backtick rather than a backslash — a JSON
payload passed literally is a reliable way to lose a quote and get a
``ParserError`` that looks nothing like the real problem. Base64 has no
characters a shell cares about.

**Output is fenced by a marker.** Windows decorates stdout freely: a pip
warning, a BOM, a "Python was not found" shim notice. Taking everything
after ``<<<AW_WIN_PILOT_JSON>>>`` means unrelated noise cannot turn a
successful call into a JSON parse error.

**The agent is uploaded, not installed.** No installer, no package, no
version pinning ceremony — the script is one file, and
:func:`ensure_agent` re-uploads it whenever the host's copy reports a
different ``agent_version`` than the one in this repo. Updating the app is
therefore enough to update the host, with no user action.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path

from . import remote_host

log = logging.getLogger("aw_apps.windows-pilot")

MARKER = "<<<AW_WIN_PILOT_JSON>>>"
AGENT_FILENAME = "aw_win_pilot.py"
REMOTE_DIR = ".aw-windows-pilot"

DEFAULT_PYTHON = "py -3"
DEFAULT_SCREENSHOT_DIR = ".tmp/windows-pilot/"

# Hosts whose agent this process has already verified, mapped to the version
# it verified. Per-process only: a workspace restart re-checks, which costs
# one extra round trip on the first call and keeps a manually-deleted agent
# from making every later call fail.
_ENSURED: dict[str, str] = {}


def local_agent_path() -> Path:
    return Path(__file__).resolve().parent.parent / "host_agent" / AGENT_FILENAME


def agent_version() -> str:
    """The VERSION literal in the agent source, read without importing it.

    Importing is not an option — the module is Windows-only (it binds
    ``user32`` at import time) and this process is Linux.
    """
    text = local_agent_path().read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0"


def remote_agent_path(host_override: str | None = None) -> str:
    return f"{remote_host.home(host_override)}\\{REMOTE_DIR}\\{AGENT_FILENAME}"


def _powershell(command: str) -> str:
    """Wrap a command so the child's UTF-8 output survives the trip.

    Windows PowerShell 5.1 decodes a child process's stdout using
    ``[Console]::OutputEncoding``, which defaults to the OEM code page
    (437/850). Any non-ASCII character in a window title — every accented
    Portuguese word, every em dash in a page title — comes back mangled, and
    the JSON that carried it fails to parse. Both halves have to agree:
    PYTHONIOENCODING makes Python emit UTF-8, OutputEncoding makes
    PowerShell read it as UTF-8.
    """
    return ("$env:PYTHONIOENCODING='utf-8'; "
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            + command)


def _parse(stdout: str, stderr: str, exit_code: int, verb: str) -> dict:
    index = stdout.rfind(MARKER)
    if index == -1:
        detail = (stdout or stderr or "").strip()[-800:]
        raise RuntimeError(
            f"{verb}: the host agent produced no result marker (exit "
            f"{exit_code}). Usually the agent is missing or Python is not on "
            f"the host's PATH. Output was: {detail or '(nothing)'}")
    payload = json.loads(stdout[index + len(MARKER):].strip())
    if not payload.get("ok"):
        raise RuntimeError(f"{verb}: {payload.get('error') or 'failed on the host'}")
    return payload.get("result") or {}


def call(verb: str, args: dict | None = None, *, timeout: int = 120,
         host_override: str | None = None, python_exe: str | None = None,
         ensure: bool = True) -> dict:
    """Run one verb on the host agent and return its parsed result."""
    if ensure:
        ensure_agent(host_override=host_override, python_exe=python_exe)
    encoded = base64.b64encode(
        json.dumps(args or {}, ensure_ascii=False).encode("utf-8")).decode()
    script = remote_agent_path(host_override)
    python = python_exe or DEFAULT_PYTHON
    command = _powershell(f"& {python} '{script}' {verb} --args-b64 {encoded}")
    stdout, stderr, code = remote_host.exec(command, timeout=timeout,
                                            host_override=host_override)
    return _parse(stdout, stderr, code, verb)


def ensure_agent(host_override: str | None = None,
                 python_exe: str | None = None, force: bool = False) -> dict:
    """Upload the agent if the host has no copy, or an older one.

    Returns the host's ``pilot_status`` either way, so the caller that
    triggered the upload gets the machine's state for free.
    """
    version = agent_version()
    key = host_override or "default"
    if not force and _ENSURED.get(key) == version:
        return {"agent_version": version, "uploaded": False, "cached": True}

    status = None
    if not force:
        try:
            status = call("pilot_status", timeout=90, host_override=host_override,
                          python_exe=python_exe, ensure=False)
        except Exception as exc:  # noqa: BLE001 — absent/stale agent is the norm
            log.info("windows-pilot: host agent needs (re)uploading: %s", exc)

    if status and status.get("agent_version") == version:
        _ENSURED[key] = version
        return {"agent_version": version, "uploaded": False, "status": status}

    remote_host.upload(str(local_agent_path()), remote_agent_path(host_override),
                       host_override=host_override)
    status = call("pilot_status", timeout=120, host_override=host_override,
                  python_exe=python_exe, ensure=False)
    if status.get("agent_version") != version:
        raise RuntimeError(
            f"uploaded agent {version} but the host reports "
            f"{status.get('agent_version')} — a stale copy is shadowing it")
    _ENSURED[key] = version
    return {"agent_version": version, "uploaded": True, "status": status}


def workspace_root() -> str:
    return os.environ.get("AW_WORKSPACE_CONTAINER_DIR") or "/opt/aw-workspace"


def local_capture_path(filename: str, subdir: str | None = None) -> str:
    """Absolute path under the workspace's scratch dir for a pulled image.

    Absolute and resolved from ``AW_WORKSPACE_CONTAINER_DIR`` rather than
    relative: this process, the CLI and an agent's session all run from
    different working directories, and a relative path silently lands
    somewhere different for each of them.
    """
    base = subdir or DEFAULT_SCREENSHOT_DIR
    root = base if os.path.isabs(base) else os.path.join(workspace_root(), base)
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, filename)
