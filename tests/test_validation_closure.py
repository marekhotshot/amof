"""Unit tests for amof.validation_closure/v1 (T1–T13 style)."""

from __future__ import annotations

import copy
import unittest

from scripts.amof.execution_backends.validation_closure import (
    SCHEMA_ID,
    attach_post_verify,
    build_validation_summary,
    derive_validation_closure,
    map_acceptance_to_legacy_status,
)


class ValidationClosureDeriveTests(unittest.TestCase):
    def test_t1_completed_all_required_passed(self) -> None:
        closure = derive_validation_closure(
            execution_status="completed",
            validation_gates=["unit", "lint"],
            heuristic_status="passed",
            structured_results=[
                {"validation_id": "unit", "required": True, "state": "PASSED"},
                {"validation_id": "lint", "required": True, "state": "PASSED"},
            ],
        )
        self.assertEqual(closure["schema"], SCHEMA_ID)
        self.assertEqual(closure["acceptance_state"], "PASS")
        self.assertEqual(closure["validation_status"], "PASSED")
        self.assertEqual(closure["required_count"], 2)
        self.assertEqual(closure["passed_count"], 2)

    def test_t2_completed_required_not_run_unverified(self) -> None:
        closure = derive_validation_closure(
            execution_status="completed",
            validation_gates=["unit"],
            heuristic_status="not_run",
        )
        self.assertEqual(closure["acceptance_state"], "UNVERIFIED")
        self.assertNotEqual(closure["acceptance_state"], "PASS")
        self.assertEqual(closure["validation_status"], "NOT_RUN")
        self.assertEqual(closure["not_run_count"], 1)
        self.assertEqual(map_acceptance_to_legacy_status("UNVERIFIED"), "not_run")

    def test_t3_required_failed(self) -> None:
        closure = derive_validation_closure(
            execution_status="completed",
            validation_gates=["unit"],
            heuristic_status="failed",
        )
        self.assertEqual(closure["acceptance_state"], "FAIL")
        self.assertEqual(closure["validation_status"], "FAILED")
        self.assertEqual(closure["failed_count"], 1)

    def test_t4_required_blocked(self) -> None:
        closure = derive_validation_closure(
            execution_status="completed",
            validation_gates=["gate_a"],
            heuristic_status="not_run",
            structured_results=[
                {"validation_id": "gate_a", "required": True, "state": "BLOCKED"},
            ],
        )
        self.assertEqual(closure["acceptance_state"], "BLOCKED")
        self.assertEqual(closure["validation_status"], "BLOCKED")

    def test_t5_empty_gates_not_run_unverified(self) -> None:
        closure = derive_validation_closure(
            execution_status="completed",
            validation_gates=None,
            heuristic_status="not_run",
        )
        self.assertEqual(closure["required_count"], 0)
        self.assertEqual(closure["acceptance_state"], "UNVERIFIED")
        self.assertEqual(closure["validation_status"], "NOT_REQUIRED")
        self.assertNotEqual(closure["acceptance_state"], "PASS")

    def test_t6_empty_gates_passed_heuristic_pass(self) -> None:
        closure = derive_validation_closure(
            execution_status="completed",
            validation_gates=[],
            heuristic_status="passed",
        )
        self.assertEqual(closure["acceptance_state"], "PASS")
        self.assertEqual(closure["validation_status"], "PASSED")

    def test_t7_written_not_run_gates_present(self) -> None:
        closure = derive_validation_closure(
            execution_status="completed",
            validation_gates=["honesty_contract"],
            heuristic_status="not_run",
        )
        self.assertEqual(closure["acceptance_state"], "UNVERIFIED")
        self.assertEqual(closure["requirements"][0]["state"], "NOT_RUN")

    def test_t8_heuristic_passed_no_structured_still_unverified(self) -> None:
        """Gates present + heuristic passed without structured evidence → UNVERIFIED.

        Documented: prose/heuristic success is not per-gate executable evidence.
        """
        closure = derive_validation_closure(
            execution_status="completed",
            validation_gates=["unit", "lint"],
            heuristic_status="passed",
        )
        self.assertEqual(closure["acceptance_state"], "UNVERIFIED")
        self.assertEqual(closure["not_run_count"], 2)
        self.assertIn("without per-gate executable evidence", closure["notes"])

    def test_t9_prose_passed_with_gates_unverified(self) -> None:
        closure = derive_validation_closure(
            execution_status="completed",
            validation_gates=["honesty_contract"],
            heuristic_status="passed",
            tests_executed=["some_unrelated_check"],
        )
        self.assertEqual(closure["acceptance_state"], "UNVERIFIED")
        self.assertEqual(closure["requirements"][0]["state"], "NOT_RUN")

    def test_t10_post_verify_not_run_to_passed(self) -> None:
        base = derive_validation_closure(
            execution_status="completed",
            validation_gates=["honesty_contract"],
            heuristic_status="not_run",
        )
        self.assertEqual(base["acceptance_state"], "UNVERIFIED")
        original = copy.deepcopy(base)
        elevated = attach_post_verify(
            base,
            state="PASSED",
            evidence_refs=["evidence/post-verify.json"],
            verified_by="operator",
            verified_at="2026-08-11T00:00:00Z",
            verification_mode="manual_fixture",
        )
        self.assertEqual(base, original)  # immutable input
        self.assertEqual(elevated["acceptance_state"], "PASS")
        self.assertEqual(elevated["validation_status"], "PASSED")
        self.assertEqual(elevated["requirements"][0]["state"], "PASSED")
        self.assertIn("evidence/post-verify.json", elevated["acceptance_evidence_refs"])
        self.assertEqual(elevated["post_verify"]["verified_by"], "operator")

    def test_t11_post_verify_fail(self) -> None:
        base = derive_validation_closure(
            execution_status="completed",
            validation_gates=["honesty_contract"],
            heuristic_status="not_run",
        )
        failed = attach_post_verify(
            base,
            state="FAILED",
            evidence_refs=["evidence/post-verify-fail.json"],
            verified_by="ci",
            verified_at="2026-08-11T00:00:00Z",
            verification_mode="fixture",
        )
        self.assertEqual(failed["acceptance_state"], "FAIL")
        self.assertEqual(failed["validation_status"], "FAILED")

    def test_t12_multiple_gates_one_not_run(self) -> None:
        closure = derive_validation_closure(
            execution_status="completed",
            validation_gates=["a", "b"],
            heuristic_status="passed",
            structured_results=[
                {"validation_id": "a", "required": True, "state": "PASSED"},
                {"validation_id": "b", "required": True, "state": "NOT_RUN"},
            ],
        )
        self.assertEqual(closure["acceptance_state"], "UNVERIFIED")
        self.assertEqual(closure["validation_status"], "PARTIAL")

    def test_t13_null_missing_not_pass(self) -> None:
        for gates in (None, [], ["gate"]):
            closure = derive_validation_closure(
                execution_status="completed",
                validation_gates=gates,
                heuristic_status="not_run",
            )
            self.assertNotEqual(closure["acceptance_state"], "PASS")
            summary = build_validation_summary(closure)
            self.assertNotEqual(summary["status"], "passed")
            self.assertEqual(summary["acceptance_state"], closure["acceptance_state"])

    def test_b5_fixture_completed_not_run_then_post_verify(self) -> None:
        closure = derive_validation_closure(
            execution_status="completed",
            validation_gates=["honesty_contract"],
            heuristic_status="not_run",
        )
        self.assertEqual(closure["acceptance_state"], "UNVERIFIED")
        self.assertEqual(
            map_acceptance_to_legacy_status(closure["acceptance_state"]),
            "not_run",
        )
        after = attach_post_verify(
            closure,
            state="PASSED",
            evidence_refs=["fixtures/b5-post-verify.json"],
            verified_by="b5-fixture",
            verified_at="2026-08-11T00:00:00Z",
            verification_mode="b5",
        )
        self.assertEqual(after["acceptance_state"], "PASS")

    def test_execution_failed_unverified_unless_heuristic_failed(self) -> None:
        unverified = derive_validation_closure(
            execution_status="failed",
            validation_gates=["unit"],
            heuristic_status="not_run",
        )
        self.assertEqual(unverified["acceptance_state"], "UNVERIFIED")
        failed = derive_validation_closure(
            execution_status="failed",
            validation_gates=["unit"],
            heuristic_status="failed",
        )
        self.assertEqual(failed["acceptance_state"], "FAIL")


if __name__ == "__main__":
    unittest.main()
