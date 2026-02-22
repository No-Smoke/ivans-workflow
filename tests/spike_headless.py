"""Phase 0 — Headless Dispatch Validation Spike.

Proves the headless `claude -p` mechanics work before touching IWO production code.
These are integration tests requiring:
  - `claude` CLI installed (v2.x+)
  - tmux running
  - eBatt project at ~/Nextcloud/PROJECTS/ebatt-ai/ebatt/

Run: python3 -m pytest tests/spike_headless.py -v -s
"""

import json
import subprocess
import time
import os
from pathlib import Path

import pytest

EBATT_ROOT = Path.home() / "Nextcloud/PROJECTS/ebatt-ai/ebatt"
SKILL_FILE = EBATT_ROOT / ".claude/skills/boris-builder-agent/SKILL.md"
CLAUDE_BIN = "claude"
TIMEOUT = 120  # generous timeout for claude -p

# Strip CLAUDECODE env var to avoid "nested session" detection when running
# subprocess.run() from inside an existing Claude Code session.
# This does NOT affect production IWO — it dispatches via tmux panes (clean env).
CLEAN_ENV = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


def parse_claude_json(stdout: str) -> dict:
    """Parse claude -p --output-format json output.

    The CLI returns a JSON *array* of event objects, not a single dict.
    Each event has a 'type' field.  Key events:
      - {"type": "system", "subtype": "init", "session_id": "..."}
      - {"type": "result", "result": "...", "session_id": "..."}

    Returns a dict with 'session_id' and 'result' extracted from the array.
    """
    events = json.loads(stdout)
    if isinstance(events, dict):
        # Fallback: maybe a future CLI version returns a single dict
        return events

    assert isinstance(events, list), f"Expected list, got {type(events)}"

    parsed: dict = {}
    for evt in events:
        if not isinstance(evt, dict):
            continue
        if evt.get("type") == "system" and evt.get("subtype") == "init":
            parsed["session_id"] = evt.get("session_id", "")
        if evt.get("type") == "result":
            parsed["result"] = evt.get("result", "")
            # Result events also carry session_id
            if "session_id" in evt:
                parsed.setdefault("session_id", evt["session_id"])

    return parsed


@pytest.fixture(scope="module")
def ebatt_dir():
    """Ensure eBatt project directory exists."""
    assert EBATT_ROOT.exists(), f"eBatt root not found: {EBATT_ROOT}"
    assert (EBATT_ROOT / "CLAUDE.md").exists(), "CLAUDE.md not found in eBatt root"
    return EBATT_ROOT


@pytest.fixture(scope="module")
def skill_file():
    """Ensure builder skill file exists."""
    assert SKILL_FILE.exists(), f"Builder skill file not found: {SKILL_FILE}"
    return SKILL_FILE


# ---------------------------------------------------------------------------
# Test 1: Basic headless invocation
# ---------------------------------------------------------------------------
class TestBasicInvocation:
    """Test that claude -p works from the eBatt directory and returns JSON."""

    def test_basic_invocation(self, ebatt_dir):
        """Run claude -p with a simple prompt, verify JSON output with session_id."""
        result = subprocess.run(
            [CLAUDE_BIN, "-p", "Reply with exactly: HEADLESS_OK", "--output-format", "json"],
            capture_output=True, text=True, timeout=TIMEOUT,
            cwd=str(ebatt_dir), env=CLEAN_ENV,
        )

        print(f"Exit code: {result.returncode}")
        print(f"stdout (first 500): {result.stdout[:500]}")
        print(f"stderr (first 500): {result.stderr[:500]}")

        assert result.returncode == 0, f"claude -p failed: {result.stderr}"

        data = parse_claude_json(result.stdout)
        assert "session_id" in data, f"No session_id in parsed output: {data.keys()}"
        assert "result" in data, f"No result in parsed output: {data.keys()}"
        print(f"session_id: {data['session_id']}")
        print(f"result (first 200): {str(data['result'])[:200]}")


# ---------------------------------------------------------------------------
# Test 2: Skill injection via --append-system-prompt-file
# ---------------------------------------------------------------------------
class TestSkillInjection:
    """Test that --append-system-prompt-file injects role context."""

    def test_skill_injection(self, ebatt_dir, skill_file):
        """Run with builder SKILL.md appended, verify role awareness."""
        result = subprocess.run(
            [
                CLAUDE_BIN, "-p",
                "What agent role have you been assigned? Reply with just the role name.",
                "--output-format", "json",
                "--append-system-prompt-file", str(skill_file),
            ],
            capture_output=True, text=True, timeout=TIMEOUT,
            cwd=str(ebatt_dir), env=CLEAN_ENV,
        )

        print(f"Exit code: {result.returncode}")
        print(f"stdout (first 500): {result.stdout[:500]}")
        print(f"stderr (first 500): {result.stderr[:500]}")

        assert result.returncode == 0, f"claude -p with skill failed: {result.stderr}"

        data = parse_claude_json(result.stdout)
        result_text = str(data.get("result", "")).lower()
        print(f"Role response: {result_text}")
        # Builder skill should make the agent aware of its role
        assert any(kw in result_text for kw in ("builder", "build", "implement")), \
            f"Agent didn't recognize builder role: {result_text}"


# ---------------------------------------------------------------------------
# Test 3: Session resumption
# ---------------------------------------------------------------------------
class TestSessionResume:
    """Test session resumption with --resume."""

    def test_session_resume(self, ebatt_dir):
        """Start a session, capture session_id, resume and verify context."""
        # First invocation — establish a session with a unique marker
        marker = f"SPIKE_MARKER_{int(time.time())}"
        r1 = subprocess.run(
            [CLAUDE_BIN, "-p", f"Remember this marker: {marker}. Reply OK.",
             "--output-format", "json"],
            capture_output=True, text=True, timeout=TIMEOUT,
            cwd=str(ebatt_dir), env=CLEAN_ENV,
        )

        assert r1.returncode == 0, f"First invocation failed: {r1.stderr}"
        d1 = parse_claude_json(r1.stdout)
        assert "session_id" in d1, f"No session_id in first invocation: {d1.keys()}"
        session_id = d1["session_id"]
        print(f"Session ID: {session_id}")

        # Resume the session
        r2 = subprocess.run(
            [CLAUDE_BIN, "--resume", session_id, "-p",
             "What marker did I ask you to remember? Reply with just the marker.",
             "--output-format", "json"],
            capture_output=True, text=True, timeout=TIMEOUT,
            cwd=str(ebatt_dir), env=CLEAN_ENV,
        )

        print(f"Resume exit code: {r2.returncode}")
        print(f"Resume stdout (first 500): {r2.stdout[:500]}")

        assert r2.returncode == 0, f"Resume failed: {r2.stderr}"
        d2 = parse_claude_json(r2.stdout)
        result_text = str(d2.get("result", ""))
        print(f"Resumed result: {result_text}")
        assert marker in result_text, \
            f"Session didn't preserve context. Expected {marker} in: {result_text}"


# ---------------------------------------------------------------------------
# Test 4: tmux pane launch
# ---------------------------------------------------------------------------
class TestTmuxPaneLaunch:
    """Test launching claude -p inside a tmux pane."""

    @pytest.fixture
    def test_pane(self):
        """Create a temporary tmux session+pane for testing."""
        session_name = "iwo-spike-test"
        # Kill any existing test session
        subprocess.run(["tmux", "kill-session", "-t", session_name],
                       capture_output=True)
        # Create new session
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-x", "200", "-y", "50"],
            check=True,
        )
        time.sleep(0.5)

        yield session_name

        # Cleanup
        subprocess.run(["tmux", "kill-session", "-t", session_name],
                       capture_output=True)

    def test_tmux_pane_launch(self, test_pane, ebatt_dir):
        """Launch claude -p in a tmux pane, verify it runs and exits."""
        session_name = test_pane
        log_file = f"/tmp/iwo-spike-test-{int(time.time())}.log"

        # Build the command to run in the pane
        cmd = (
            f"cd {ebatt_dir} && "
            f"claude -p 'Reply with exactly: PANE_OK' "
            f"--output-format json "
            f"> {log_file} 2>&1"
        )

        # Send the command to the pane
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, cmd, "Enter"],
            check=True,
        )

        # Wait for claude to finish (poll pane_current_command)
        max_wait = TIMEOUT
        elapsed = 0
        poll_interval = 2.0
        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            result = subprocess.run(
                ["tmux", "list-panes", "-t", session_name,
                 "-F", "#{pane_current_command}"],
                capture_output=True, text=True,
            )
            current_cmd = result.stdout.strip()
            print(f"  [{elapsed:.0f}s] pane_current_command: {current_cmd}")

            if current_cmd in ("bash", "zsh", "sh", "fish"):
                break
        else:
            pytest.fail(f"claude -p didn't exit within {max_wait}s")

        # Verify log file was created with output
        log_path = Path(log_file)
        assert log_path.exists(), f"Log file not created: {log_file}"
        log_content = log_path.read_text()
        print(f"Log content (first 500): {log_content[:500]}")
        assert len(log_content) > 0, "Log file is empty"

        # Parse JSON output — could be a JSON array or NDJSON lines
        data = None
        # First try: entire content is a JSON array
        try:
            data = parse_claude_json(log_content.strip())
        except (json.JSONDecodeError, AssertionError):
            pass

        # Fallback: try NDJSON (line-by-line)
        if not data or "result" not in data:
            for line in log_content.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and "result" in obj:
                        data = {"result": obj["result"]}
                        break
                    if isinstance(obj, list):
                        data = parse_claude_json(line)
                        if "result" in data:
                            break
                except (json.JSONDecodeError, AssertionError):
                    continue

        assert data is not None and "result" in data, \
            f"No parseable result in log output (first 500): {log_content[:500]}"
        print(f"Pane launch result: {data['result']}")

        # Cleanup log
        log_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Test 5: Idle detection after process exit
# ---------------------------------------------------------------------------
class TestIdleDetection:
    """Test that pane_current_command returns to shell after claude -p exits."""

    @pytest.fixture
    def test_pane(self):
        """Create a temporary tmux session for testing."""
        session_name = "iwo-spike-idle"
        subprocess.run(["tmux", "kill-session", "-t", session_name],
                       capture_output=True)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-x", "200", "-y", "50"],
            check=True,
        )
        time.sleep(0.5)
        yield session_name
        subprocess.run(["tmux", "kill-session", "-t", session_name],
                       capture_output=True)

    def test_idle_detection(self, test_pane, ebatt_dir):
        """After claude -p exits, pane_current_command should be 'bash'."""
        session_name = test_pane

        # Verify pane starts idle
        result = subprocess.run(
            ["tmux", "list-panes", "-t", session_name,
             "-F", "#{pane_current_command}"],
            capture_output=True, text=True,
        )
        initial_cmd = result.stdout.strip()
        print(f"Initial pane command: {initial_cmd}")
        assert initial_cmd in ("bash", "zsh", "sh", "fish"), \
            f"Pane not idle initially: {initial_cmd}"

        # Launch claude -p
        cmd = (
            f"cd {ebatt_dir} && "
            f"claude -p 'Reply OK' --output-format json > /dev/null 2>&1"
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, cmd, "Enter"],
            check=True,
        )

        # Brief wait then check that it's NOT bash (claude should be running)
        time.sleep(2)
        result = subprocess.run(
            ["tmux", "list-panes", "-t", session_name,
             "-F", "#{pane_current_command}"],
            capture_output=True, text=True,
        )
        during_cmd = result.stdout.strip()
        print(f"During execution pane command: {during_cmd}")
        # Note: might already have finished if fast. That's OK — we just log it.

        # Wait for completion
        max_wait = TIMEOUT
        elapsed = 0
        while elapsed < max_wait:
            time.sleep(2)
            elapsed += 2
            result = subprocess.run(
                ["tmux", "list-panes", "-t", session_name,
                 "-F", "#{pane_current_command}"],
                capture_output=True, text=True,
            )
            final_cmd = result.stdout.strip()
            if final_cmd in ("bash", "zsh", "sh", "fish"):
                print(f"Pane returned to idle ({final_cmd}) after {elapsed}s")
                break
        else:
            pytest.fail(f"Pane didn't return to idle within {max_wait}s")

        # THE KEY ASSERTION: pane is idle = deterministic idle detection
        assert final_cmd in ("bash", "zsh", "sh", "fish"), \
            f"Expected idle shell, got: {final_cmd}"


# ---------------------------------------------------------------------------
# Test 6: Error handling
# ---------------------------------------------------------------------------
class TestErrorHandling:
    """Test error handling for invalid claude -p invocations."""

    def test_error_handling(self, ebatt_dir):
        """Invalid invocation should produce documented exit codes and stderr."""
        # Test 1: Empty prompt (might succeed or fail depending on version)
        r1 = subprocess.run(
            [CLAUDE_BIN, "-p", "", "--output-format", "json"],
            capture_output=True, text=True, timeout=TIMEOUT,
            cwd=str(ebatt_dir), env=CLEAN_ENV,
        )
        print(f"Empty prompt — exit code: {r1.returncode}")
        print(f"Empty prompt — stderr: {r1.stderr[:300]}")
        print(f"Empty prompt — stdout: {r1.stdout[:300]}")

        # Test 2: Invalid flag
        r2 = subprocess.run(
            [CLAUDE_BIN, "-p", "test", "--invalid-flag-xyz"],
            capture_output=True, text=True, timeout=30,
            cwd=str(ebatt_dir), env=CLEAN_ENV,
        )
        print(f"Invalid flag — exit code: {r2.returncode}")
        print(f"Invalid flag — stderr: {r2.stderr[:300]}")
        # Invalid flag should produce non-zero exit code
        assert r2.returncode != 0, "Expected non-zero exit for invalid flag"

        # Test 3: Nonexistent skill file
        r3 = subprocess.run(
            [CLAUDE_BIN, "-p", "test",
             "--append-system-prompt-file", "/nonexistent/SKILL.md",
             "--output-format", "json"],
            capture_output=True, text=True, timeout=30,
            cwd=str(ebatt_dir), env=CLEAN_ENV,
        )
        print(f"Bad skill file — exit code: {r3.returncode}")
        print(f"Bad skill file — stderr: {r3.stderr[:300]}")
        # Document behavior — may exit non-zero or ignore
        print(f"\n=== Error Handling Summary ===")
        print(f"Empty prompt: exit={r1.returncode}")
        print(f"Invalid flag: exit={r2.returncode}")
        print(f"Bad skill file: exit={r3.returncode}")
