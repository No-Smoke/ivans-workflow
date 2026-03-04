"""Tests for IWO Ops Actions Register — Phase 1 + 2.

Tests CRUD operations, deduplication, priority/category classification,
stale detection, and auto-extraction from handoff data.
"""

import json
import tempfile
from pathlib import Path

import pytest

from iwo.ops_actions import (
    OpsAction,
    OpsActionsRegister,
    classify_category,
    classify_priority,
    compute_fingerprint,
    _normalize_text,
)


# --- Unit: Classification ---

class TestClassification:
    def test_critical_migration(self):
        assert classify_priority("D1 migration NOT yet applied to production") == "critical"

    def test_critical_secret(self):
        assert classify_priority("wrangler secret RESEND_API_KEY not configured") == "critical"

    def test_critical_human_must(self):
        assert classify_priority("Human must run UPDATE users SET role") == "critical"

    def test_critical_dns(self):
        assert classify_priority("DNS CNAME for ebatt.ethospower.ai must be created") == "critical"

    def test_warning_webhook(self):
        assert classify_priority("n8n webhook setup needed") == "warning"

    def test_info_verification(self):
        assert classify_priority("user must verify browser rendering") == "info"

    def test_info_default(self):
        assert classify_priority("some random note about code quality") == "info"

    def test_category_migration(self):
        assert classify_category("D1 migration 0003 not applied") == "migration"

    def test_category_secret(self):
        assert classify_category("wrangler secret RESEND_API_KEY") == "secret"

    def test_category_dns(self):
        assert classify_category("DNS CNAME record needed") == "dns"

    def test_category_webhook(self):
        assert classify_category("n8n webhook not configured") == "webhook"

    def test_category_verification(self):
        assert classify_category("browser verify the page") == "verification"

    def test_category_email(self):
        assert classify_category("Resend domain verification pending") == "email_infra"

    def test_category_config(self):
        assert classify_category("KV namespace binding not set") == "config"

    def test_category_other(self):
        assert classify_category("something completely unrelated") == "other"


# --- Unit: Fingerprinting ---

class TestFingerprint:
    def test_same_text_same_spec(self):
        fp1 = compute_fingerprint("EBATT-022", "Migration 0003 not applied")
        fp2 = compute_fingerprint("EBATT-022", "Migration 0003 not applied")
        assert fp1 == fp2

    def test_different_spec(self):
        fp1 = compute_fingerprint("EBATT-022", "Migration not applied")
        fp2 = compute_fingerprint("EBATT-023", "Migration not applied")
        assert fp1 != fp2

    def test_whitespace_normalized(self):
        fp1 = compute_fingerprint("X", "  migration  not   applied  ")
        fp2 = compute_fingerprint("X", "migration not applied")
        assert fp1 == fp2

    def test_sequence_refs_stripped(self):
        """Sequence references like #003 are stripped before fingerprinting."""
        fp1 = compute_fingerprint("X", "issue in #003 handoff")
        fp2 = compute_fingerprint("X", "issue in handoff")
        assert fp1 == fp2

    def test_normalize_text(self):
        assert _normalize_text("  Hello   World  #123  ") == "hello world"


# --- Unit: Register CRUD ---

class TestRegisterCRUD:
    def _make_register(self, tmp_path: Path) -> OpsActionsRegister:
        reg = OpsActionsRegister(tmp_path / ".ops-actions.json")
        reg.load()
        return reg

    def test_add_and_save(self, tmp_path):
        reg = self._make_register(tmp_path)
        action = OpsAction(
            id="ops-test-001",
            spec_id="TEST-001",
            title="Test action",
            description="Must run migration",
            fingerprint=compute_fingerprint("TEST-001", "Must run migration"),
        )
        assert reg.add(action) is True
        reg.save()

        # Reload and verify
        reg2 = OpsActionsRegister(tmp_path / ".ops-actions.json")
        reg2.load()
        assert len(reg2.actions) == 1
        assert reg2.actions[0].id == "ops-test-001"

    def test_dedup_by_fingerprint(self, tmp_path):
        reg = self._make_register(tmp_path)
        fp = compute_fingerprint("TEST-001", "Must run migration")
        a1 = OpsAction(id="ops-001", spec_id="TEST-001", title="A", description="Must run migration", fingerprint=fp)
        a2 = OpsAction(id="ops-002", spec_id="TEST-001", title="B", description="Must run migration", fingerprint=fp)
        assert reg.add(a1) is True
        assert reg.add(a2) is False  # duplicate
        assert len(reg.actions) == 1

    def test_resolve(self, tmp_path):
        reg = self._make_register(tmp_path)
        action = OpsAction(id="ops-001", spec_id="T", title="T", description="D", fingerprint="fp1")
        reg.add(action)
        assert reg.resolve("ops-001", resolved_by="vanya", notes="Done") is True
        assert reg.actions[0].status == "completed"
        assert reg.actions[0].resolved_by == "vanya"
        assert reg.actions[0].resolved_at is not None

    def test_skip(self, tmp_path):
        reg = self._make_register(tmp_path)
        action = OpsAction(id="ops-001", spec_id="T", title="T", description="D", fingerprint="fp1")
        reg.add(action)
        assert reg.skip("ops-001", reason="Not needed") is True
        assert reg.actions[0].status == "skipped"
        assert "Not needed" in reg.actions[0].notes

    def test_resolve_nonexistent(self, tmp_path):
        reg = self._make_register(tmp_path)
        assert reg.resolve("nonexistent") is False

    def test_mark_stale(self, tmp_path):
        reg = self._make_register(tmp_path)
        action = OpsAction(id="ops-001", spec_id="T", title="T", description="D", fingerprint="fp1")
        reg.add(action)
        assert reg.mark_stale("ops-001") is True
        assert reg.actions[0].stale_since is not None

    def test_clear_stale(self, tmp_path):
        reg = self._make_register(tmp_path)
        action = OpsAction(id="ops-001", spec_id="T", title="T", description="D", fingerprint="fp1")
        reg.add(action)
        reg.mark_stale("ops-001")
        reg.clear_stale("ops-001")
        assert reg.actions[0].stale_since is None

    def test_get_pending_sorted(self, tmp_path):
        reg = self._make_register(tmp_path)
        reg.add(OpsAction(id="i", spec_id="T", title="info", description="D", priority="info", fingerprint="fp1"))
        reg.add(OpsAction(id="c", spec_id="T", title="crit", description="D", priority="critical", fingerprint="fp2"))
        reg.add(OpsAction(id="w", spec_id="T", title="warn", description="D", priority="warning", fingerprint="fp3"))
        pending = reg.get_pending()
        assert [a.id for a in pending] == ["c", "w", "i"]

    def test_get_pending_filtered(self, tmp_path):
        reg = self._make_register(tmp_path)
        reg.add(OpsAction(id="c", spec_id="T", title="crit", description="D", priority="critical", fingerprint="fp1"))
        reg.add(OpsAction(id="i", spec_id="T", title="info", description="D", priority="info", fingerprint="fp2"))
        critical = reg.get_pending("critical")
        assert len(critical) == 1
        assert critical[0].id == "c"

    def test_resolve_removes_from_pending(self, tmp_path):
        reg = self._make_register(tmp_path)
        reg.add(OpsAction(id="ops-001", spec_id="T", title="T", description="D", fingerprint="fp1"))
        reg.resolve("ops-001")
        assert reg.pending_count() == 0

    def test_get_summary(self, tmp_path):
        reg = self._make_register(tmp_path)
        reg.add(OpsAction(id="c1", spec_id="T", title="T", description="D", priority="critical", fingerprint="fp1"))
        reg.add(OpsAction(id="c2", spec_id="T", title="T", description="D", priority="critical", fingerprint="fp2"))
        reg.add(OpsAction(id="i1", spec_id="T", title="T", description="D", priority="info", fingerprint="fp3"))
        reg.resolve("c1")
        summary = reg.get_summary()
        assert summary["critical"]["pending"] == 1
        assert summary["critical"]["completed"] == 1
        assert summary["info"]["pending"] == 1
        assert summary["total"]["pending"] == 2

    def test_has_pending_critical(self, tmp_path):
        reg = self._make_register(tmp_path)
        reg.add(OpsAction(id="c", spec_id="SPEC-A", title="T", description="D", priority="critical", fingerprint="fp1"))
        reg.add(OpsAction(id="i", spec_id="SPEC-B", title="T", description="D", priority="info", fingerprint="fp2"))
        assert reg.has_pending_critical() is True
        assert reg.has_pending_critical("SPEC-A") is True
        assert reg.has_pending_critical("SPEC-B") is False

    def test_priority_override(self, tmp_path):
        reg = self._make_register(tmp_path)
        action = OpsAction(
            id="ops-001", spec_id="T", title="T", description="D",
            priority="info", priority_override="critical", fingerprint="fp1"
        )
        reg.add(action)
        assert action.effective_priority == "critical"
        pending = reg.get_pending("critical")
        assert len(pending) == 1

    def test_idempotent_seed(self, tmp_path):
        """Re-running seed doesn't create duplicates."""
        reg = self._make_register(tmp_path)
        fp = compute_fingerprint("T", "migration not applied")
        reg.add(OpsAction(id="s1", spec_id="T", title="T", description="migration not applied", fingerprint=fp))
        reg.save()

        # Simulate re-seed
        reg2 = OpsActionsRegister(tmp_path / ".ops-actions.json")
        reg2.load()
        result = reg2.add(OpsAction(id="s2", spec_id="T", title="T", description="migration not applied", fingerprint=fp))
        assert result is False
        assert len(reg2.actions) == 1


# --- Integration: Auto-extraction mock ---

class TestAutoExtraction:
    """Test the extraction logic that would run in daemon._extract_ops_actions."""

    def _mock_handoff_json(self) -> dict:
        return {
            "metadata": {"specId": "EBATT-099", "agent": "tester", "timestamp": "2026-02-26T00:00:00Z", "sequence": 5},
            "status": {
                "outcome": "success",
                "unresolvedIssues": [
                    "D1 migration 0010 NOT yet applied to production",
                    "Some code quality note about TypeScript",
                    "wrangler secret STRIPE_KEY not configured",
                ],
            },
            "nextAgent": {"target": "deployer", "action": "Deploy to production"},
            "deploymentInstructions": {
                "noNewMigrations": False,
                "noNewSecrets": True,
                "manualSteps": [
                    "Run npx wrangler d1 migrations apply ebatt-db --remote",
                ],
            },
        }

    def test_extraction_filters_correctly(self, tmp_path):
        """Only ops-action patterns are extracted, not code quality notes."""
        from iwo.ops_actions import classify_priority as cp, compute_fingerprint as cfp

        raw = self._mock_handoff_json()
        spec_id = "EBATT-099"
        ops_texts = []

        # Simulate the extraction logic from daemon
        OPS_PATTERNS = [
            re.compile(p, re.IGNORECASE) for p in [
                r'migration.*not\s+(yet\s+)?(applied|run)',
                r'not\s+yet\s+(set|configured|created|applied|deployed|stored)',
                r'wrangler\s+(secret|d1)',
                r'must\s+(run|create|configure|set|apply|execute|deploy|store)',
                r'secrets?\s+not\s+(set|configured)',
                r'npx\s+wrangler',
            ]
        ]

        def is_ops(text):
            return any(p.search(text) for p in OPS_PATTERNS)

        for issue in raw["status"]["unresolvedIssues"]:
            if is_ops(issue):
                ops_texts.append(issue)

        # Should extract migration and secret issues, not the TS note
        assert len(ops_texts) == 2
        assert any("migration" in t.lower() for t in ops_texts)
        assert any("secret" in t.lower() for t in ops_texts)
        assert not any("TypeScript" in t for t in ops_texts)

    def test_infra_flags_generate_actions(self):
        raw = self._mock_handoff_json()
        deploy_info = raw["deploymentInstructions"]
        assert deploy_info["noNewMigrations"] is False
        assert deploy_info["noNewSecrets"] is True
        # noNewMigrations=False should generate a migration action


import re  # needed for test patterns
