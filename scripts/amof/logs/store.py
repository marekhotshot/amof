"""Durable JSONL-backed structured log storage."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import List

from .records import StructuredLogRecord


class StructuredLogStore:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self.base_dir / f"{run_id}.jsonl"

    def append(self, record: StructuredLogRecord) -> None:
        with open(self._path(record.run_id), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), default=str) + "\n")

    def read(self, run_id: str) -> List[StructuredLogRecord]:
        path = self._path(run_id)
        if not path.exists():
            return []
        rows: List[StructuredLogRecord] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rows.append(StructuredLogRecord(**json.loads(line)))
        return rows
