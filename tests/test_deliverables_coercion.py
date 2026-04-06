"""Tests for Deliverables boolean field coercion.

Validates that typecheckPassed, lintPassed, and buildPassed accept
booleans, None, and string values — coercing strings to the correct type
so the pipeline doesn't stall on Pydantic validation errors.
"""

import pytest
from iwo.parser import Deliverables


class TestCheckStatusCoercion:
    """Verify coerce_check_status field_validator on all three fields."""

    FIELDS = ["typecheckPassed", "lintPassed", "buildPassed"]

    @pytest.mark.parametrize("field", FIELDS)
    def test_bool_true_accepted(self, field):
        d = Deliverables(**{field: True})
        assert getattr(d, field) is True

    @pytest.mark.parametrize("field", FIELDS)
    def test_bool_false_accepted(self, field):
        d = Deliverables(**{field: False})
        assert getattr(d, field) is False

    @pytest.mark.parametrize("field", FIELDS)
    def test_none_accepted(self, field):
        d = Deliverables(**{field: None})
        assert getattr(d, field) is None

    @pytest.mark.parametrize("field", FIELDS)
    def test_not_run_coerced_to_none(self, field):
        d = Deliverables(**{field: "not run"})
        assert getattr(d, field) is None

    @pytest.mark.parametrize("field", FIELDS)
    def test_not_executed_coerced_to_none(self, field):
        d = Deliverables(**{field: "not executed"})
        assert getattr(d, field) is None

    @pytest.mark.parametrize("field", FIELDS)
    def test_skipped_coerced_to_none(self, field):
        d = Deliverables(**{field: "skipped"})
        assert getattr(d, field) is None

    @pytest.mark.parametrize("field", FIELDS)
    def test_na_coerced_to_none(self, field):
        d = Deliverables(**{field: "n/a"})
        assert getattr(d, field) is None

    @pytest.mark.parametrize("field", FIELDS)
    def test_empty_string_coerced_to_none(self, field):
        d = Deliverables(**{field: ""})
        assert getattr(d, field) is None

    @pytest.mark.parametrize("field", FIELDS)
    @pytest.mark.parametrize("val", ["true", "passed", "pass", "yes", "ok"])
    def test_truthy_strings_coerced_to_true(self, field, val):
        d = Deliverables(**{field: val})
        assert getattr(d, field) is True

    @pytest.mark.parametrize("field", FIELDS)
    @pytest.mark.parametrize("val", ["false", "failed", "fail", "no", "error"])
    def test_falsy_strings_coerced_to_false(self, field, val):
        d = Deliverables(**{field: val})
        assert getattr(d, field) is False

    @pytest.mark.parametrize("field", FIELDS)
    def test_unknown_string_coerced_to_none(self, field):
        d = Deliverables(**{field: "something unexpected"})
        assert getattr(d, field) is None

    @pytest.mark.parametrize("field", FIELDS)
    def test_case_insensitive(self, field):
        d = Deliverables(**{field: "NOT RUN"})
        assert getattr(d, field) is None

    @pytest.mark.parametrize("field", FIELDS)
    def test_whitespace_stripped(self, field):
        d = Deliverables(**{field: "  not run  "})
        assert getattr(d, field) is None

    def test_default_is_none(self):
        d = Deliverables()
        assert d.typecheckPassed is None
        assert d.lintPassed is None
        assert d.buildPassed is None
