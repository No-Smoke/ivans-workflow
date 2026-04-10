"""Config regression tests — Phase 1 of 2026-04-10 fix plan."""
import os
from unittest.mock import patch

from iwo.config import IWOConfig as Config


def test_stall_auto_handoff_default_disabled():
    """Stall auto-handoff MUST default to False after the phantom cascade incident.

    Regression for 2026-04-10. Re-enabling requires explicit
    IWO_STALL_AUTO_HANDOFF_ENABLED=true in the environment.
    """
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("IWO_STALL_AUTO_HANDOFF_ENABLED", None)
        os.environ.pop("IWO_STALL_AUTO_HANDOFF", None)  # old key, should be ignored
        cfg = Config()
        assert cfg.stall_auto_handoff_enabled is False


def test_stall_auto_handoff_env_override_true():
    """Explicit env var still enables it (opt-in path)."""
    with patch.dict(os.environ, {"IWO_STALL_AUTO_HANDOFF_ENABLED": "true"}):
        cfg = Config()
        assert cfg.stall_auto_handoff_enabled is True


def test_old_env_key_ignored():
    """The old IWO_STALL_AUTO_HANDOFF key must NOT re-enable the feature.

    Prevents silent re-activation from stale shell envs.
    """
    with patch.dict(os.environ, {"IWO_STALL_AUTO_HANDOFF": "true"}, clear=False):
        os.environ.pop("IWO_STALL_AUTO_HANDOFF_ENABLED", None)
        cfg = Config()
        assert cfg.stall_auto_handoff_enabled is False
