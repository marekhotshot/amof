from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.write_scope_proposals import (
    PATH_CLASS_EXPLICIT_REPOSITORY_ROOT,
    PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE,
    PATH_CLASS_MISSING_OR_INVALID,
    classify_repository_relative_scope_path,
)


class RepositoryRelativePathClassificationTests(unittest.TestCase):
    def test_root_level_file(self) -> None:
        classified = classify_repository_relative_scope_path("README.md")
        self.assertEqual(classified.path_class, PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE)
        self.assertEqual(classified.normalized, "README.md")

    def test_nested_file(self) -> None:
        classified = classify_repository_relative_scope_path(
            "services/operator-console/src/lib/status-response-bounds.ts"
        )
        self.assertEqual(classified.path_class, PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE)

    def test_nested_directory(self) -> None:
        classified = classify_repository_relative_scope_path("docs/")
        self.assertEqual(classified.path_class, PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE)
        self.assertEqual(classified.normalized, "docs/")

    def test_empty_string_is_missing_not_root(self) -> None:
        classified = classify_repository_relative_scope_path("")
        self.assertEqual(classified.path_class, PATH_CLASS_MISSING_OR_INVALID)
        self.assertEqual(classified.detail, "empty_path")

    def test_missing_flag(self) -> None:
        classified = classify_repository_relative_scope_path(None, missing=True)
        self.assertEqual(classified.path_class, PATH_CLASS_MISSING_OR_INVALID)
        self.assertEqual(classified.detail, "missing_path")

    def test_dot_is_explicit_repository_root_scope(self) -> None:
        classified = classify_repository_relative_scope_path(".")
        self.assertEqual(classified.path_class, PATH_CLASS_EXPLICIT_REPOSITORY_ROOT)

    def test_absolute_rejected(self) -> None:
        classified = classify_repository_relative_scope_path("/tmp/repo/README.md")
        self.assertEqual(classified.path_class, PATH_CLASS_MISSING_OR_INVALID)
        self.assertEqual(classified.detail, "absolute_path")

    def test_traversal_rejected(self) -> None:
        classified = classify_repository_relative_scope_path("../secret")
        self.assertEqual(classified.path_class, PATH_CLASS_MISSING_OR_INVALID)
        self.assertEqual(classified.detail, "traversal_segment")


if __name__ == "__main__":
    unittest.main()
