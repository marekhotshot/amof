"""Persistence for runs: JSON file per run."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from amof.app_paths import runs_dir
from .postgres_utils import (
    connect_postgres,
    get_database_url,
    get_storage_backend,
    load_psycopg2,
    postgres_storage_configured,
)
from .run_manager import RunRecord, RunEvent, RUN_STATUS_QUEUED


def _run_to_json(r: RunRecord) -> Dict[str, Any]:
    return {
        "run_id": r.run_id,
        "ecosystem": r.ecosystem,
        "action": r.action,
        "command": r.command,
        "status": r.status,
        "created_at": r.created_at,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "exit_code": r.exit_code,
        "session_id": r.session_id,
        "session_snapshot": r.session_snapshot,
        "loop_state": r.loop_state,
        "events": [
            {
                "timestamp": e.timestamp,
                "level": e.level,
                "type": e.type,
                "message": e.message,
                "run_id": e.run_id,
                "payload": e.payload,
            }
            for e in r.events
        ],
    }


def _run_from_json(data: Dict[str, Any]) -> RunRecord:
    events = [
        RunEvent(
            timestamp=e["timestamp"],
            level=e["level"],
            type=e["type"],
            message=e["message"],
            run_id=e["run_id"],
            payload=e.get("payload"),
        )
        for e in data.get("events", [])
    ]
    return RunRecord(
        run_id=data["run_id"],
        ecosystem=data["ecosystem"],
        action=data["action"],
        command=data.get("command", []),
        status=data.get("status", RUN_STATUS_QUEUED),
        created_at=data["created_at"],
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        exit_code=data.get("exit_code"),
        events=events,
        session_id=data.get("session_id"),
        session_snapshot=data.get("session_snapshot"),
        loop_state=data.get("loop_state"),
    )


def _run_summary_from_json(data: Dict[str, Any]) -> RunRecord:
    return RunRecord(
        run_id=data["run_id"],
        ecosystem=data["ecosystem"],
        action=data["action"],
        command=data.get("command", []),
        status=data.get("status", RUN_STATUS_QUEUED),
        created_at=data["created_at"],
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        exit_code=data.get("exit_code"),
        events=[],
        session_id=data.get("session_id"),
        session_snapshot=None,
        loop_state=data.get("loop_state"),
    )


class RunStore:
    """Store runs in Postgres when configured, otherwise one JSON file per run."""

    def __init__(self, base_dir: Optional[str] = None) -> None:
        if base_dir is None:
            base_dir = os.environ.get("AMOF_RUNS_DIR") or str(runs_dir())
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.storage_backend = get_storage_backend()
        self.database_url = get_database_url()
        self._psycopg2 = None
        self._postgres_ready = False
        self._legacy_runs_migrated = False
        if postgres_storage_configured():
            try:
                self._psycopg2 = load_psycopg2()
                if self._psycopg2 is None:
                    raise RuntimeError("psycopg2 is unavailable")
                self._ensure_postgres_schema()
                self._postgres_ready = True
            except Exception:
                self._postgres_ready = False

    def _path(self, run_id: str) -> Path:
        return self.base_dir / f"{run_id}.json"

    def save(self, run: RunRecord) -> None:
        if self._use_postgres():
            self._save_postgres(run)
            return
        self._save_file(run)

    def load(self, run_id: str) -> Optional[RunRecord]:
        if self._use_postgres():
            self._migrate_legacy_files()
            run = self._load_postgres(run_id)
            if run is not None:
                return run
        return self._load_file(run_id)

    def list_run_ids(self) -> List[str]:
        if self._use_postgres():
            self._migrate_legacy_files()
            run_ids = set(self._list_run_ids_postgres())
            run_ids.update(self._list_run_ids_file())
            return sorted(run_ids)
        return self._list_run_ids_file()

    def list_runs_summary(
        self,
        ecosystem: Optional[str] = None,
        action: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[RunRecord]:
        if self._use_postgres():
            self._migrate_legacy_files()
            summaries = {run.run_id: run for run in self._list_runs_summary_postgres(ecosystem, action, status, limit)}
            for run in self._list_runs_summary_file(ecosystem, action, status, limit):
                summaries.setdefault(run.run_id, run)
            runs = list(summaries.values())
            runs.sort(key=lambda run: run.created_at, reverse=True)
            return runs[:limit]
        return self._list_runs_summary_file(ecosystem, action, status, limit)

    def _use_postgres(self) -> bool:
        return self._postgres_ready

    def _connect(self):
        if self._psycopg2 is None:
            raise RuntimeError("Postgres run store is not available")
        return connect_postgres(self._psycopg2)

    def _save_file(self, run: RunRecord) -> None:
        self._path(run.run_id).write_text(
            json.dumps(_run_to_json(run), indent=0),
            encoding="utf-8",
        )

    def _load_file(self, run_id: str) -> Optional[RunRecord]:
        p = self._path(run_id)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return _run_from_json(data)
        except Exception:
            return None

    def _list_run_ids_file(self) -> List[str]:
        if not self.base_dir.exists():
            return []
        return [
            f.stem for f in self.base_dir.glob("*.json")
            if f.is_file()
        ]

    def _list_runs_summary_file(
        self,
        ecosystem: Optional[str] = None,
        action: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[RunRecord]:
        runs: List[RunRecord] = []
        for path in self.base_dir.glob("*.json"):
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            run = _run_summary_from_json(data)
            if ecosystem is not None and run.ecosystem != ecosystem:
                continue
            if action is not None and run.action != action:
                continue
            if status is not None and run.status != status:
                continue
            runs.append(run)
        runs.sort(key=lambda run: run.created_at, reverse=True)
        return runs[:limit]

    def _ensure_postgres_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_runs (
                        run_id TEXT PRIMARY KEY,
                        ecosystem TEXT NOT NULL,
                        action TEXT NOT NULL,
                        command_json JSONB NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        started_at TEXT NULL,
                        finished_at TEXT NULL,
                        exit_code INTEGER NULL,
                        session_id TEXT NULL,
                        session_snapshot_json JSONB NULL,
                        loop_state_json JSONB NULL,
                        events_json JSONB NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE api_runs
                    ADD COLUMN IF NOT EXISTS session_snapshot_json JSONB NULL
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE api_runs
                    ADD COLUMN IF NOT EXISTS loop_state_json JSONB NULL
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS api_runs_created_at_idx
                    ON api_runs (created_at DESC)
                    """
                )

    def _save_postgres(self, run: RunRecord) -> None:
        payload = _run_to_json(run)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO api_runs
                        (run_id, ecosystem, action, command_json, status, created_at,
                         started_at, finished_at, exit_code, session_id, session_snapshot_json, loop_state_json, events_json)
                    VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
                    ON CONFLICT (run_id) DO UPDATE SET
                        ecosystem = EXCLUDED.ecosystem,
                        action = EXCLUDED.action,
                        command_json = EXCLUDED.command_json,
                        status = EXCLUDED.status,
                        created_at = EXCLUDED.created_at,
                        started_at = EXCLUDED.started_at,
                        finished_at = EXCLUDED.finished_at,
                        exit_code = EXCLUDED.exit_code,
                        session_id = EXCLUDED.session_id,
                        session_snapshot_json = EXCLUDED.session_snapshot_json,
                        loop_state_json = EXCLUDED.loop_state_json,
                        events_json = EXCLUDED.events_json
                    """,
                    (
                        payload["run_id"],
                        payload["ecosystem"],
                        payload["action"],
                        json.dumps(payload["command"]),
                        payload["status"],
                        payload["created_at"],
                        payload.get("started_at"),
                        payload.get("finished_at"),
                        payload.get("exit_code"),
                        payload.get("session_id"),
                        json.dumps(payload.get("session_snapshot")),
                        json.dumps(payload.get("loop_state") or {}),
                        json.dumps(payload["events"]),
                    ),
                )

    def _load_postgres(self, run_id: str) -> Optional[RunRecord]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT run_id, ecosystem, action, command_json, status, created_at,
                           started_at, finished_at, exit_code, session_id, session_snapshot_json, loop_state_json, events_json
                    FROM api_runs
                    WHERE run_id = %s
                    """,
                    (run_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return _run_from_row(row)

    def _list_run_ids_postgres(self) -> List[str]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT run_id FROM api_runs ORDER BY created_at DESC")
                return [str(row[0]) for row in cur.fetchall()]

    def _list_runs_summary_postgres(
        self,
        ecosystem: Optional[str] = None,
        action: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[RunRecord]:
        clauses = []
        params: List[Any] = []
        if ecosystem is not None:
            clauses.append("ecosystem = %s")
            params.append(ecosystem)
        if action is not None:
            clauses.append("action = %s")
            params.append(action)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        query = f"""
            SELECT run_id, ecosystem, action, command_json, status, created_at,
                   started_at, finished_at, exit_code, session_id, loop_state_json
            FROM api_runs
            {where}
            ORDER BY created_at DESC
            LIMIT %s
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                return [_run_summary_from_row(row) for row in cur.fetchall()]

    def _migrate_legacy_files(self) -> None:
        if self._legacy_runs_migrated:
            return
        for run_id in self._list_run_ids_file():
            run = self._load_file(run_id)
            if run is not None:
                self._save_postgres(run)
        self._legacy_runs_migrated = True


def _run_from_row(row: Any) -> RunRecord:
    (
        run_id,
        ecosystem,
        action,
        command_json,
        status,
        created_at,
        started_at,
        finished_at,
        exit_code,
        session_id,
        session_snapshot_json,
        loop_state_json,
        events_json,
    ) = row
    data = {
        "run_id": run_id,
        "ecosystem": ecosystem,
        "action": action,
        "command": command_json if isinstance(command_json, list) else json.loads(command_json),
        "status": status or RUN_STATUS_QUEUED,
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "session_id": session_id,
        "session_snapshot": session_snapshot_json if isinstance(session_snapshot_json, dict) else json.loads(session_snapshot_json) if session_snapshot_json is not None else None,
        "loop_state": loop_state_json if isinstance(loop_state_json, dict) else json.loads(loop_state_json or "{}"),
        "events": events_json if isinstance(events_json, list) else json.loads(events_json),
    }
    return _run_from_json(data)


def _run_summary_from_row(row: Any) -> RunRecord:
    (
        run_id,
        ecosystem,
        action,
        command_json,
        status,
        created_at,
        started_at,
        finished_at,
        exit_code,
        session_id,
        loop_state_json,
    ) = row
    data = {
        "run_id": run_id,
        "ecosystem": ecosystem,
        "action": action,
        "command": command_json if isinstance(command_json, list) else json.loads(command_json),
        "status": status or RUN_STATUS_QUEUED,
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "session_id": session_id,
        "loop_state": loop_state_json if isinstance(loop_state_json, dict) else json.loads(loop_state_json or "{}"),
    }
    return _run_summary_from_json(data)
