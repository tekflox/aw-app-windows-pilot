# aw-app-windows-pilot

Pilot a real Windows desktop from any agent in this workspace — screenshot
the screen, list and focus windows, click, type, press keys — plus a
**Playwright that runs on that machine**, driving a browser window the user
can see, in a profile that keeps their logins.

21 tools, gateway-prefixed `aw__aw_windows_pilot__*`. Tier-1 (in-process):
no container, no port, no subprocess. Every call is an HTTPS request to
aw-backend's remote-hosts routes.

## Why it exists

This workspace already has three ways to drive a browser — the `playwright`
MCP, `devctl_browser`, `mini_browser` — and all three drive a browser that
lives *here*: a different IP, none of the user's cookies, invisible to the
user. None of them can open the app on someone's laptop, click through a
desktop installer, or fill in a site as the person who is signed into it.

That is the gap. `aw-app-windows-pilot` puts the automation **on the
machine**, in the logged-in session, and leaves only a thin JSON protocol
crossing the network.

## Install

```bash
aw-workspace-cli marketplace install windows-pilot
```

Open **Windows Pilot** in the Apps grid and fill in:

| Setting | What it is |
|---|---|
| aw-backend URL | The aw-backend fronting your linked hosts, e.g. `https://api.aw.tekflox.com` |
| Calling workspace slug | *This* workspace's slug (`aw`) — **not** the slug the Windows host is registered under. aw-backend resolves ownership server-side; using the host's slug returns 401. |
| Windows host id | From `aw-workspace-cli remote-hosts hosts` — pick the one with `os: windows`. |
| Bearer token | This workspace's `AW_WORKSPACE_HOST_TOKEN`. Goes to the secret store, never to plain config. |

Then **Test the connection**, then **Provision the host** (once per machine).

## The two halves

**Desktop** — `win_screenshot`, `win_list_windows`, `win_focus_window`,
`win_window_action`, `win_click`, `win_move_mouse`, `win_scroll`, `win_type`,
`win_key`, `win_clipboard`, `win_run`.

**Browser** — `win_browser_launch`, `win_browser_navigate`,
`win_browser_snapshot`, `win_browser_click`, `win_browser_type`,
`win_browser_eval`, `win_browser_tabs`, `win_browser_screenshot`.

Plus `win_pilot_status` and `win_pilot_provision`.

Full usage guidance, including the etiquette of driving a machine somebody
may be sitting in front of, is in `skills/aw-windows-pilot/SKILL.md`.

## How it works

```
agent ── aw-gateway ── this app (Tier-1, in aw-workspace)
                            │  HTTPS: /exec, /fs/upload, /fs/download
                            ▼
                        aw-backend ── reverse tunnel ── aw-remote-host on Windows
                                                            │
                                                   py -3 aw_win_pilot.py <verb>
                                                            │
                                          ctypes/user32 · Pillow · Playwright
```

`windows_pilot_app/host_agent/aw_win_pilot.py` is the only code that runs on
Windows. It is **uploaded, not installed**: `host_agent.ensure_agent()`
compares the host's reported `agent_version` against the copy in this repo
and re-uploads when they differ, so updating the app updates the host with no
user action.

One tool call is one exec round trip. Arguments cross as base64 (PowerShell
escapes with a backtick, and a literal JSON argument loses a quote sooner or
later); results come back after a `<<<AW_WIN_PILOT_JSON>>>` marker, because
Windows decorates stdout freely and unrelated noise should not turn a
successful call into a parse error.

### Two constraints worth knowing before you extend it

**The exec must land in an interactive session.** aw-remote-host on Windows
runs in the logged-in user's session (verified `SessionId=1` on
DESKTOP-DRMKFBT). A process in session 0 — what a Windows *service* would
give you — has no desktop: every screenshot is black and every click goes
nowhere, silently. `win_pilot_status` reports the session id so that failure
is loud.

**Screenshots never come back through exec.** aw-remote-hosts caps a job's
stdout at 1 MiB and reports `exit_code: -1` past it, so a base64'd PNG
arrives as exactly 1048576 characters of valid-looking data. Images stage on
the host's disk and are pulled through the dedicated download route.

### The browser profile

`win_browser_launch` opens Edge (or Chrome) with `--remote-debugging-port`
against a **dedicated persistent profile**, and Playwright attaches over CDP.
Chrome 136+ and current Edge refuse remote debugging on the browser's
*default* profile — a deliberate change after infostealer malware started
attaching to signed-in browsers — so attaching to the window the user already
has open is not possible. The persistent profile gets the same practical
result one step later: the user signs in once in the piloted window, and it
stays signed in.

## Dependencies on the host

Deliberately two wheels: **Pillow** (screen capture) and **playwright**
(browser). Window enumeration, focus, move/resize, mouse, keyboard and
clipboard are `ctypes` against `user32`/`kernel32`, which is always there —
fewer wheels is fewer ways for provisioning to half-succeed on somebody's
laptop. Both install with `pip --user`: this is a personal machine, not a
container, and an app should not be writing into a system Python or creating
a venv nobody asked for.

## Tests

```bash
python3 -m pytest tests/ -q
python3 tests/validate_manifest.py aw-app.json
```

The Windows half cannot be imported on Linux (it binds `user32` at import),
so it is checked structurally — parsed, with its `VERBS` table compared
against every verb this side calls. That is the failure that actually
happens: a verb added on one side of the bridge and not the other.
