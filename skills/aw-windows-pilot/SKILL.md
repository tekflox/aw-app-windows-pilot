---
name: aw-windows-pilot
description: Pilot a real Windows desktop reached through aw-remote-hosts — screenshot the screen, find and focus windows, click/type/press keys — and drive a Playwright browser that runs ON that machine, in a persistent profile carrying the user's own logins. Use whenever a task involves "my Windows machine", the Surface, automating a Windows app, filling in a site as the user, or anything that has to happen inside someone's actual desktop session rather than in this workspace.
---

# aw-windows-pilot — driving somebody's actual Windows machine

## What this is, and what it is not

Two piloting surfaces on one remote Windows box, both reached over
**aw-remote-hosts** (aw-backend's exec + file-transfer routes, no direct
network path to the machine):

| Surface | Tools | What it drives |
|---|---|---|
| **Desktop** | `win_screenshot`, `win_list_windows`, `win_focus_window`, `win_window_action`, `win_click`, `win_type`, `win_key`, `win_scroll`, `win_move_mouse`, `win_clipboard`, `win_run` | The real Windows session — any app, Explorer, a dialog, an installer |
| **Browser** | `win_browser_launch`, `win_browser_navigate`, `win_browser_snapshot`, `win_browser_click`, `win_browser_type`, `win_browser_eval`, `win_browser_tabs`, `win_browser_screenshot` | A Chromium browser window **on that desktop**, driven by Playwright running locally on the host |

Plus `win_pilot_status` and `win_pilot_provision` for setup.

Through aw-gateway the names are prefixed: `aw__aw_windows_pilot__win_screenshot`,
and so on.

**Do not confuse this with the workspace's other browser tools.** The
`playwright` MCP, `devctl_browser` and `mini_browser` all drive browsers that
live in *this* datacentre — different IP, no access to the user's logins,
invisible to the user. This app's browser is a window the user can watch on
their own screen, on their own network. That difference is usually the entire
reason a task lands here.

## Before anything else: `win_pilot_status`

```
win_pilot_status()
```

It answers the three questions that explain nearly every failure:

1. **`session_id`** — must be a real interactive session (1, 2, …). If it is
   **0**, the host's `aw-remote-host` is running as a service, which has no
   desktop: screenshots come back black and every click silently goes
   nowhere. No amount of retrying fixes that; the agent has to run in the
   logged-in session.
2. **`deps`** — `pillow` is required for any screenshot, `playwright` for the
   browser half. Both `null` means the host was never provisioned.
3. **`cdp`** — non-null when a piloted browser is already running, so you can
   skip `win_browser_launch`.

If it reports `ready: false`, run `win_pilot_provision()` once. It uploads
the host agent and pip-installs the two wheels; a few minutes the first time,
seconds afterwards. It is also how you upgrade the agent after the app
updates — safe to re-run.

## The desktop loop

The interaction model is **screenshot → read coordinates → act**. There is no
accessibility tree for native windows.

1. **`win_list_windows()`** — what is actually open. Returns title, process,
   `hwnd`, position and size, and which window has focus. Filter with
   `title_contains` when you know roughly what you are after.
2. **`win_focus_window(title="Excel")`** — *always* before typing. Input goes
   to whatever is focused, not to a window you merely named in a previous
   call, and the user may have clicked elsewhere between your calls.
3. **`win_screenshot()`** — writes a PNG into this workspace and returns the
   path. Read that path with your image reader. Pass `title=` or `hwnd=` to
   capture one window instead of the whole desktop; pass `region=` for a
   rectangle.
4. **`win_click(x=…, y=…)`** — coordinates read off that screenshot.

### The scale factor — the one that will bite you

Screenshots are downscaled to 1600px wide by default, because a Surface's
native capture is ~4 MB and every byte crosses the tunnel and then your
context. The result carries `scale` (e.g. `0.53`) and a `note` saying so.

**Coordinates you read off the image must be divided by `scale` before you
click them.** Click at the raw image coordinate and you land roughly halfway
to where you meant, in a way that looks like the mouse is broken rather than
like an arithmetic error. Pass `max_width: 0` when you would rather have full
resolution and no arithmetic.

### Typing

- `win_type(text="…")` sends real Unicode, so `ção`, `ü` and emoji arrive
  correctly whatever keyboard layout the machine has. Add `submit: true` for
  a trailing Enter.
- For anything long, **`win_clipboard(text="…")` then `win_key(keys="ctrl+v")`**
  is faster and far more reliable than typing hundreds of characters into an
  app that may drop input.
- `win_key` takes chords and sequences: `"ctrl+c"`, `"alt+tab"`,
  `"win+r"`, `"ctrl+shift+esc"`. Space separates groups — `"win+r"` opens
  Run, `"win r"` taps Win and then r, which are different things.

## Don't click your way through an app that can be scripted

Before piloting a GUI step by step, ask whether the app has an automation
surface — because on this channel a GUI is expensive. Every click, keystroke
and screenshot is a separate exec round trip, so building a spreadsheet cell
by cell is ~100 round trips and a hundred chances to misread a coordinate.
The same work through the app's own COM interface is **one `win_run` call**,
and it produces a better artefact: real formulas instead of pasted numbers.

Office, Explorer, WMI, the registry and most of Windows are scriptable this
way. Excel, as the worked example:

```powershell
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $true          # a real window on the user's screen
$excel.DisplayAlerts = $false
$wb = $excel.Workbooks.Add()
$ws = $wb.Worksheets.Item(1)
# ... fill, format, chart ...
$wb.SaveAs([Environment]::GetFolderPath("Desktop") + "\out.xlsx", 51)
```

Then use the piloting tools for what they *are* good at: `win_list_windows`
to find the window it opened, `win_window_action` to maximize it,
`win_screenshot` to confirm what the user is actually looking at. Script the
work, pilot the verification.

Four things that bit on the first real run:

- **Write a whole range in one call**, not cell by cell. Build a
  `New-Object "object[,]" $rows, $cols` and assign it to
  `$ws.Range("A1").Resize($rows,$cols).Value2`. Per-cell writes are slow, and
  PowerShell's COM adapter can refuse to put a Double into a cell it first
  saw hold a String (`Unable to cast object of type 'System.Double'`).
- **Parenthesise every computed index.** PowerShell reads
  `$data[0, $i + 1]` as `$data[(0, $i), 1]` and dies with "You cannot index
  into a 2 dimensional array with index [0,0,1]".
- **Wrap the whole thing in try/catch and `$excel.Quit()` on failure.** A
  script that dies halfway leaves an invisible orphan Excel holding an empty
  workbook, and the next run inherits the mess.
- **A string starting with `=` becomes a formula** when assigned through
  `Value2`, and Excel accepts English function names whatever the UI
  language is — so `"=SUM(B2:M2)"` is portable.

## The browser loop

```
win_browser_launch()                  # opens (or reuses) the piloted window
win_browser_navigate(url="https://…")
win_browser_snapshot()                # url, title, page text, refs
win_browser_click(ref="e14")
win_browser_type(ref="e7", text="…", submit=true)
```

**Prefer `win_browser_snapshot` over screenshotting the page.** It returns
the visible text plus every interactive element with a stable `ref`, which
you pass straight to click/type. No coordinates, no scale factor, no image
in your context. Screenshot the page only when the *appearance* is what
matters.

### The profile, and what "the user's session" really means

`win_browser_launch` opens a browser with a **dedicated persistent profile**
(`%LOCALAPPDATA%\aw-windows-pilot\browser-profile` by default), not the
user's everyday one. Chrome 136+ and current Edge refuse
`--remote-debugging-port` when pointed at the default profile — a deliberate
change after infostealer malware started attaching to signed-in browsers — so
attaching to the Chrome window the user already has open is not possible.

What the persistent profile buys is the same thing one step later: **cookies
and logins survive across calls and reboots**. The user signs into a site once
in the piloted window, and every later run is signed in. If a task needs a
login the profile does not have yet, say so and ask the user to sign in once
in that window — do not try to type their password.

## When something looks broken

| Symptom | Cause |
|---|---|
| Screenshot is black; clicks do nothing | `session_id: 0` — no desktop. Nothing here can fix it. |
| `no result marker` | The host agent is missing, or Python is not on the host's PATH. Run `win_pilot_provision`, or set `python_exe`. |
| Clicks land in the wrong place | You forgot to divide by `scale`, or the window moved between screenshot and click — re-screenshot. |
| `SendInput delivered 0/2` | A UAC-elevated window is in the foreground. A non-elevated process cannot send input to it, by design. |
| `focus_window` reports `focused: false` | Something else owns the foreground. `win_list_windows` and look at what does — an open Start menu, or a modal you didn't expect (Office's "Sign in to set up Office" nag did this repeatedly). Close the blocker with `win_window_action`, then **click the target's title bar with `win_click`** — a real click takes foreground when `SetForegroundWindow` is refused. |
| Calls take 15-60s each | Windows PowerShell 5.1 startup on a cold host. Installing PowerShell 7 (`pwsh`) on the machine is the single biggest speed-up. Slow is not hung. |
| Browser tools say "no CDP browser" | `win_browser_launch` first. |
| Launch times out | Another instance of that browser already holds the same profile. Close it, or pass a different `user_data_dir`. |

## Etiquette — this is somebody's actual computer

There is a person who may be sitting in front of this machine.

- **Focusing a window steals their focus**, mid-sentence if they are typing.
  Pass `focus: false` to `win_screenshot` to photograph a background window
  without disturbing them.
- **Say what you are about to do** before a run that will visibly take over
  the screen, and prefer batching your actions over drip-feeding them.
- **Do not close windows or files you did not open**, and do not type into a
  document you were not asked to touch.
- **Never type a password** the user has not explicitly handed you for that
  purpose. If a site needs a login the piloted profile lacks, ask them to
  sign in once themselves.

## Targeting a different machine

Every tool takes `remote_host_id`. `aw-workspace-cli remote-hosts hosts`
lists the linked machines with their `os` — the Windows ones are what this
app can drive. The app's configured default is the Surface
(`DESKTOP-DRMKFBT`, `c76c606b0a2a5a8b`); Settings changes it permanently, the
per-call argument changes it once.

A newly targeted host needs its own `win_pilot_provision` run — the agent and
its dependencies live on each machine, not in this workspace.
