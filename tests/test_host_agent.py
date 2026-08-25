"""Tests for the workspace half — the bridge and the tool surface.

The Windows half (``host_agent/aw_win_pilot.py``) cannot be imported here:
it binds ``user32`` at import time and this is Linux. It is checked
structurally instead, by parsing it — which is enough to catch the failure
that actually happens, a verb existing on one side of the bridge and not the
other.
"""
from __future__ import annotations

import ast
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from windows_pilot_app.mcp import host_agent, remote_host, tools  # noqa: E402


# ---------------------------------------------------------------------------
# The bridge's two ends agree
# ---------------------------------------------------------------------------

def _agent_verbs() -> set[str]:
    """The keys of the agent's VERBS dict, read without importing it."""
    tree = ast.parse(host_agent.local_agent_path().read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "VERBS" for t in node.targets)):
            return {k.value for k in node.value.keys}
    raise AssertionError("aw_win_pilot.py has no VERBS mapping")


def _verbs_called() -> set[str]:
    """Every literal verb this side passes to host_agent.call()."""
    called = set()
    for path in (Path(host_agent.__file__).parent / "tools.py",
                 Path(host_agent.__file__)):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", None) == "call"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)):
                called.add(node.args[0].value)
    return called


def test_every_verb_this_side_calls_exists_on_the_host():
    missing = _verbs_called() - _agent_verbs()
    assert not missing, f"tools call verbs the host agent does not implement: {missing}"


def test_agent_version_is_readable_without_importing_a_windows_module():
    assert host_agent.agent_version().count(".") == 2


def test_marker_matches_the_agent_source():
    source = host_agent.local_agent_path().read_text(encoding="utf-8")
    assert f'MARKER = "{host_agent.MARKER}"' in source


# ---------------------------------------------------------------------------
# Output parsing — the noise-tolerance that keeps Windows stdout from
# turning a successful call into a parse error.
# ---------------------------------------------------------------------------

def test_parse_ignores_noise_before_the_marker():
    stdout = ("WARNING: pip is being invoked by an old script wrapper\r\n"
              + host_agent.MARKER + "\n"
              + json.dumps({"ok": True, "verb": "x", "result": {"a": 1}}))
    assert host_agent._parse(stdout, "", 0, "x") == {"a": 1}


def test_parse_takes_the_last_marker_when_output_repeats_one():
    payload = json.dumps({"ok": True, "verb": "x", "result": {"which": "second"}})
    stdout = (f"{host_agent.MARKER}\n{{\"ok\": true, \"result\": {{}}}}\n"
              f"{host_agent.MARKER}\n{payload}")
    assert host_agent._parse(stdout, "", 0, "x") == {"which": "second"}


def test_parse_surfaces_the_hosts_own_error_text():
    stdout = host_agent.MARKER + "\n" + json.dumps(
        {"ok": False, "verb": "click", "error": "ValueError: pass either hwnd or title"})
    with pytest.raises(RuntimeError, match="pass either hwnd or title"):
        host_agent._parse(stdout, "", 1, "click")


def test_parse_explains_a_missing_agent_rather_than_raising_json_errors():
    with pytest.raises(RuntimeError, match="no result marker"):
        host_agent._parse("'py' is not recognized", "", 9009, "pilot_status")


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------

def test_arguments_cross_powershell_as_base64():
    """The payload must contain no quote, backtick or dollar sign.

    That is the whole reason for the encoding: PowerShell escapes with a
    backtick and expands ``$``, so a literal JSON argument is one stray
    character away from a ParserError that looks unrelated to the call.
    """
    args = {"text": "he said \"olá\" & `that` costs $5", "keys": "ctrl+c"}
    encoded = base64.b64encode(
        json.dumps(args, ensure_ascii=False).encode("utf-8")).decode()
    assert not set(encoded) & set("\"'`$;|&<>")
    assert json.loads(base64.b64decode(encoded).decode("utf-8")) == args


def test_powershell_wrapper_sets_utf8_on_both_sides():
    wrapped = host_agent._powershell("& py -3 'x.py' list_windows")
    assert "PYTHONIOENCODING='utf-8'" in wrapped          # what Python writes
    assert "[Console]::OutputEncoding" in wrapped          # what PowerShell reads


def test_capture_paths_are_absolute(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    path = host_agent.local_capture_path("shot.png", ".tmp/windows-pilot/")
    assert path.startswith(str(tmp_path))
    assert Path(path).parent.is_dir()


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

def test_manifest_advertises_exactly_the_tools_that_exist():
    manifest = json.loads(
        (Path(__file__).resolve().parent.parent / "aw-app.json").read_text())
    advertised = set(manifest["contributes"]["mcp"]["provides"])
    assert advertised == {t["name"] for t in tools.TOOLS}


def test_every_tool_accepts_a_per_call_host_override():
    for tool in tools.TOOLS:
        assert "remote_host_id" in tool["inputSchema"]["properties"], tool["name"]


def test_every_tool_is_described_for_an_agent_that_has_never_seen_it():
    for tool in tools.TOOLS:
        assert len(tool["description"]) > 60, tool["name"]


def test_required_arguments_are_declared_as_properties():
    for tool in tools.TOOLS:
        schema = tool["inputSchema"]
        assert set(schema["required"]) <= set(schema["properties"]), tool["name"]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_missing_config_names_what_is_absent_instead_of_guessing_a_host():
    remote_host.set_config_resolver(lambda: {"remote_backend_url": "https://x"})
    message = remote_host.missing_settings()
    assert "remote_workspace" in message and "remote_token" in message
    assert not remote_host.configured()


def test_a_per_call_host_id_satisfies_the_host_requirement():
    remote_host.set_config_resolver(lambda: {
        "remote_backend_url": "https://x", "remote_workspace": "aw",
        "remote_token": "t"})
    assert not remote_host.configured()
    assert remote_host.configured("c76c606b0a2a5a8b")


def test_tools_fall_back_to_the_default_python_when_config_is_empty():
    tools.set_config_resolver(lambda: {})
    assert tools._common({})["python_exe"] == host_agent.DEFAULT_PYTHON
    assert tools._common({"python_exe": "py -3.12"})["python_exe"] == "py -3.12"
