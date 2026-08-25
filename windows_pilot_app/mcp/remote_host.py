"""Talk to a Windows machine through **aw-remote-hosts**.

Same transport contract aw-app-android-studio and aw-app-crispal use — a
small stdlib client against aw-backend's remote-hosts routes::

    {remote_backend_url}/api/workspaces/{remote_workspace}/remote-hosts/{remote_host_id}/exec
    {remote_backend_url}/api/workspaces/{remote_workspace}/remote-hosts/{remote_host_id}/fs/upload
    {remote_backend_url}/api/workspaces/{remote_workspace}/remote-hosts/{remote_host_id}/fs/download
    Authorization: Bearer {remote_token}

Copied rather than imported: that code lives in a different app's package,
which is not on this process's path, and the two will drift for good reasons
(this one resolves ``%USERPROFILE%`` instead of ``$HOME``).

Config is read through a resolver the plugin installs, not captured at
import, so a value saved in Settings takes effect on the very next tool call
with no restart. Nothing is cached except the resolved home directory, per
host.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable


class NotConfigured(RuntimeError):
    """remote_backend_url / remote_workspace / remote_token / remote_host_id
    are not all present, after any per-call override.

    Raised explicitly instead of falling back to a plausible default URL: a
    wrong-but-plausible default is what let the monolith's dead remote-agent
    bridge fail quietly for months.
    """


_config_resolver: Callable[[], dict] = lambda: {}


def set_config_resolver(resolver: Callable[[], dict]) -> None:
    global _config_resolver
    _config_resolver = resolver


def _cfg(host_override: str | None = None) -> tuple[str, str, str, str]:
    try:
        values = _config_resolver() or {}
    except Exception:
        values = {}
    base = (values.get("remote_backend_url") or "").rstrip("/")
    workspace = values.get("remote_workspace") or ""
    token = values.get("remote_token") or ""
    host_id = host_override or values.get("remote_host_id") or ""
    missing = [name for name, value in (
        ("remote_backend_url", base), ("remote_workspace", workspace),
        ("remote_token", token), ("remote_host_id", host_id)) if not value]
    if missing:
        raise NotConfigured(
            "aw-remote-hosts is not configured — missing " + ", ".join(missing)
            + ". Open the Windows Pilot app's Settings and fill them in "
            "(remote_host_id can also be passed per-call).")
    return base, workspace, token, host_id


def configured(host_override: str | None = None) -> bool:
    try:
        _cfg(host_override)
        return True
    except NotConfigured:
        return False


def missing_settings(host_override: str | None = None) -> str:
    """Which settings are absent, for an error message; "" when all present."""
    try:
        _cfg(host_override)
        return ""
    except NotConfigured as exc:
        return str(exc)


def _url(path: str, params: dict | None = None,
         host_override: str | None = None) -> str:
    base, workspace, _token, host_id = _cfg(host_override)
    url = f"{base}/api/workspaces/{workspace}/remote-hosts/{host_id}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


def _call(method: str, path: str, *, body: dict | None = None,
          params: dict | None = None, timeout: float = 60.0,
          host_override: str | None = None) -> dict:
    _base, _ws, token, _host = _cfg(host_override)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _url(path, params, host_override), data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 **({"Content-Type": "application/json"} if data is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        raise RuntimeError(f"remote-host {method} {path} -> {exc.code}: {detail}") from exc
    return json.loads(raw) if raw.strip() else {}


def exec(command: str, timeout: int = 60,
         host_override: str | None = None) -> tuple[str, str, int]:
    """Run ``command`` on the linked host, blocking until it finishes.

    aw-remote-hosts' exec is start-then-wait; the two-step is hidden behind
    the plain ``(stdout, stderr, exit_code)`` every caller here wants.

    On a Windows host the command reaches Windows PowerShell 5.1, whose
    startup alone can cost 10-15s on a cold machine (it was ~2s on the box
    this app was built against). Budget timeouts accordingly — a slow reply
    is not a hang.
    """
    started = _call("POST", "/exec", body={"command": command, "timeout_s": timeout},
                    host_override=host_override)
    job_id = started.get("job_id")
    if not job_id:
        raise RuntimeError(f"remote-host exec did not return a job_id: {started}")
    result = _call("POST", f"/exec/{job_id}/wait", body={"timeout_s": timeout},
                   timeout=timeout + 30.0, host_override=host_override)
    return (result.get("stdout") or "", result.get("stderr") or "",
            int(result.get("exit_code") or 0))


_HOME_CACHE: dict[str, str] = {}


def home(host_override: str | None = None) -> str:
    """The Windows host's ``%USERPROFILE%``, resolved once per host.

    Cached because every tool call needs it to build the agent's path, and
    it is one full exec round trip — the difference between one and two
    round trips per call, on a channel where a round trip is the dominant
    cost.
    """
    _base, _ws, _token, host_id = _cfg(host_override)
    if host_id not in _HOME_CACHE:
        out, _err, code = exec("$env:USERPROFILE", timeout=60,
                               host_override=host_override)
        resolved = out.strip().splitlines()[0].strip() if out.strip() else ""
        if code != 0 or not resolved:
            raise RuntimeError(
                "could not resolve %USERPROFILE% on the remote host — is it "
                f"a Windows host, and is it online? (exit {code}) {out}{_err}")
        _HOME_CACHE[host_id] = resolved
    return _HOME_CACHE[host_id]


def forget_home(host_override: str | None = None) -> None:
    """Drop the cached home for a host (used by the /test route)."""
    try:
        _base, _ws, _token, host_id = _cfg(host_override)
    except NotConfigured:
        return
    _HOME_CACHE.pop(host_id, None)


def upload(local_path: str, remote_path: str, timeout: float = 300.0,
           host_override: str | None = None) -> dict:
    """Stream a local file to ``remote_path`` on the linked host.

    aw-backend verifies sha256 end to end, so a truncated transfer fails
    loudly here instead of leaving a half-written agent script that then
    dies with a SyntaxError nobody can explain.
    """
    _base, _ws, token, _host = _cfg(host_override)
    with open(local_path, "rb") as handle:
        payload = handle.read()
    req = urllib.request.Request(
        _url("/fs/upload", {"path": remote_path}, host_override), data=payload,
        method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/octet-stream",
                 "Content-Length": str(len(payload))})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        raise RuntimeError(f"upload of {local_path} -> {remote_path} failed "
                           f"({exc.code}): {detail}") from exc
    return json.loads(raw) if raw.strip() else {}


def download(remote_path: str, local_path: str, timeout: float = 300.0,
             host_override: str | None = None) -> int:
    """Stream a file OFF the linked host into ``local_path``; returns bytes.

    Screenshots must come back this way, never through exec: aw-remote-hosts
    caps a job's stdout at 1 MiB and reports exit_code -1 past it, so a
    base64'd PNG over exec silently arrives as exactly 1048576 characters of
    valid-looking data — a half-read image with no loud error. Stage the
    file on the host's own disk, then pull it here.
    """
    _base, _ws, token, _host = _cfg(host_override)
    req = urllib.request.Request(
        _url("/fs/download", {"path": remote_path}, host_override),
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        raise RuntimeError(f"download of {remote_path} failed ({exc.code}): "
                           f"{detail}") from exc
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    tmp = local_path + ".part"
    with open(tmp, "wb") as handle:
        handle.write(payload)
    os.replace(tmp, local_path)
    return len(payload)
