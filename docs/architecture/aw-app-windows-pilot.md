---
repo: architecture
path: docs/architecture/aw-app-windows-pilot.md
source: generated
edited: false
checksum: sha256:0a5e87fe03e04b230dae16523e52f831eedd4c3ec2c385a9d0179349412964b1
---
# Windows Pilot

- **repo**: aw-app-windows-pilot
- **layer**: app
- **technologies**: python
- **health** (derived): planned

Pilot a real Windows desktop from any agent in this workspace — list and focus windows, screenshot the screen, click, type and press keys — plus a Playwright that drives a persistent browser profile on that machine, so the agent works inside the user's own logged-in sessions. Reaches the machine through aw-remote-hosts; no fixed host, every setting is per-call overridable.

## Connections
- `http` → **aw-workspace** — routes mounted at /api/apps/windows-pilot
- `stdio-mcp` → **mcp-gateway** — MCP surface aggregated by the gateway

## MCP tools
- `win_browser_click`
- `win_browser_eval`
- `win_browser_launch`
- `win_browser_navigate`
- `win_browser_screenshot`
- `win_browser_snapshot`
- `win_browser_tabs`
- `win_browser_type`
- `win_click`
- `win_clipboard`
- `win_focus_window`
- `win_key`
- `win_list_windows`
- `win_move_mouse`
- `win_pilot_provision`
- `win_pilot_status`
- `win_run`
- `win_screenshot`
- `win_scroll`
- `win_type`
- `win_window_action`

## Requirements
_none documented_
