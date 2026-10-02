"""The cloud-session guard: what a Claude Code cloud session may not do in this repository.

`.claude/settings.json` runs `.claude/hooks/cloud-guard.js` before every Bash command and file
edit, but only when CLAUDE_CODE_REMOTE is "true", which Claude Code sets in cloud sessions and
never locally. In a cloud session the guard refuses merges, releases, tag creation and tag
pushes, pushes to main, force-pushes and deletions, writes through `gh api`, docker, ollama, and
edits to `.claude/` itself; it allows everything else, including pushing the session's own
branch, which is how a cloud session delivers its work. In a local session it refuses nothing.

The guard is a convenience in front of the server-side rules (the branch rule on main, the tag
rulesets), not a wall: a command it does not recognise passes. These tests pin what it does
recognise, both ways.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / ".claude" / "hooks" / "cloud-guard.js"
SETTINGS = REPO / ".claude" / "settings.json"
NODE = shutil.which("node")

if NODE is None and os.environ.get("CI"):
    pytest.fail("node is needed to test the cloud guard and CI must have it", pytrace=False)
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

REFUSED_COMMANDS = [
    "gh pr merge 23 --rebase",
    "cd /repo && gh pr merge --auto --squash",
    "gh release create v0.6.0 --notes x",
    "gh release edit v0.5.0 --draft=false",
    "gh release delete v0.5.0 --yes",
    "gh api -X PUT repos/o/r/pulls/23/merge",
    "gh api --method DELETE repos/o/r/git/refs/tags/v0.5.0",
    "gh api repos/o/r/git/refs -f ref=refs/tags/v9 -f sha=abc",
    "gh api repos/o/r/releases --input body.json",
    "git tag v0.6.0",
    "git tag -a v0.6.0 -m release",
    "git tag -d v0.5.0",
    "git -C /repo tag -f v0.5.0 HEAD",
    "git push origin v0.6.0 --tags",
    "git push --follow-tags",
    "git push origin refs/tags/v0.6.0",
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin claude/work:main",
    "git push --force origin claude/work",
    "git push -f",
    "git push --force-with-lease origin claude/work",
    "git push origin +claude/work",
    "git push origin --delete old-branch",
    "git push origin :old-branch",
    "git push --mirror",
    "docker run --rm -it ubuntu",
    "docker compose up -d",
    "ollama run some-model",
    "curl -s http://127.0.0.1:11434/api/tags",
    "rm .claude/settings.json",
    "sed -i 's/x/y/' .claude/hooks/cloud-guard.js",
]

ALLOWED_COMMANDS = [
    "git status",
    "git push -u origin claude/fix-docstring",
    "git push origin HEAD:claude/fix-docstring",
    "git push",
    "git tag",
    "git tag -l 'v*'",
    "git tag --list",
    "gh pr create --base main --head claude/x --title t --body b",
    "gh pr view 23 --json state",
    "gh pr checks 23",
    "gh release view v0.5.0",
    "gh release list",
    "gh api repos/o/r/rulesets",
    "python -m pytest -q",
    "cat .claude/settings.json",
    "grep -n merge .claude/hooks/cloud-guard.js",
]


def run_guard(hook_input: dict[str, Any], remote: bool) -> subprocess.CompletedProcess[str]:
    """The hook exactly as settings.json wires it: node, its args, the hook_input on stdin."""
    hook = json.loads(SETTINGS.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_REMOTE"}
    env["CLAUDE_PROJECT_DIR"] = str(REPO)
    if remote:
        env["CLAUDE_CODE_REMOTE"] = "true"
    assert NODE is not None
    return subprocess.run(
        [NODE, *hook["args"]],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        timeout=30,
    )


def bash(command: str) -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def test_settings_wire_the_guard_for_bash_and_edits_with_no_shell() -> None:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    (entry,) = settings["hooks"]["PreToolUse"]
    assert set(entry["matcher"].split("|")) == {
        "Bash",
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
    }
    (hook,) = entry["hooks"]
    assert (
        hook["type"] == "command" and hook["command"] == "node"
    )  # exec form: no shell on any platform
    assert "CLAUDE_CODE_REMOTE" in hook["args"][-1] and "cloud-guard.js" in hook["args"][-1]
    # No permission rule here: it would bind the owner's local sessions too.
    assert "permissions" not in settings


@pytest.mark.parametrize("command", REFUSED_COMMANDS)
def test_a_cloud_session_is_refused(command: str) -> None:
    result = run_guard(bash(command), remote=True)
    assert result.returncode == 2, (command, result.stdout, result.stderr)
    assert result.stderr.startswith("cloud-guard: "), result.stderr


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_a_cloud_session_may(command: str) -> None:
    result = run_guard(bash(command), remote=True)
    assert result.returncode == 0, (command, result.stderr)


@pytest.mark.parametrize("command", REFUSED_COMMANDS + ALLOWED_COMMANDS)
def test_a_local_session_is_never_refused(command: str) -> None:
    result = run_guard(bash(command), remote=False)
    assert (result.returncode, result.stderr) == (0, ""), command


@pytest.mark.parametrize(
    "path",
    [
        ".claude/settings.json",
        ".claude/hooks/cloud-guard.js",
        "/home/user/repo/.claude/settings.local.json",
    ],
)
def test_a_cloud_session_may_not_edit_the_guard(path: str) -> None:
    for tool in ("Edit", "Write", "MultiEdit"):
        hook_input = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": {"file_path": path},
        }
        assert run_guard(hook_input, remote=True).returncode == 2, (tool, path)
        assert run_guard(hook_input, remote=False).returncode == 0, (tool, path)


def test_a_cloud_session_may_edit_the_code() -> None:
    hook_input = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": "/home/user/repo/src/uas_workbench/life/cue.py"},
    }
    assert run_guard(hook_input, remote=True).returncode == 0


def test_a_malformed_hook_input_in_the_cloud_is_refused_not_waved_through() -> None:
    result = run_guard({"tool_name": "Bash"}, remote=True)  # no command at all
    assert result.returncode == 0  # nothing to judge
    proc = subprocess.run(
        [
            NODE or "node",
            *json.loads(SETTINGS.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0][
                "args"
            ],
        ],
        input="not json",
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "CLAUDE_CODE_REMOTE": "true", "CLAUDE_PROJECT_DIR": str(REPO)},
    )
    assert proc.returncode == 2 and proc.stderr.startswith("cloud-guard: ")
