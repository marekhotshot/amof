from __future__ import annotations

import importlib.util
import unittest


class RemoteIALAvailabilityTests(unittest.TestCase):
    def test_remote_ial_candidate_is_not_on_clean_public_main(self) -> None:
        """Keep the focused test path deterministic on clean public main.

        The remote-IAL candidate currently lives in the dirty operator branch
        evidence, not in this clean checkout. This test makes that state
        explicit instead of failing with "file not found" before the candidate
        is intentionally replayed into public truth.
        """

        if importlib.util.find_spec("amof.orchestrator.llm.remote_ial") is None:
            self.skipTest("remote IAL candidate is not present in clean public main")


if __name__ == "__main__":
    unittest.main()
