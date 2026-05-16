"""Shared Postgres helpers for AMOF API persistence layers."""

from __future__ import annotations

import os
from typing import Dict, Optional


def get_storage_backend() -> str:
    return (os.environ.get("AMOF_STORAGE_BACKEND") or "local").strip().lower()


def get_database_url() -> str:
    return (os.environ.get("AMOF_DATABASE_URL") or "").strip()


def postgres_storage_requested() -> bool:
    return get_storage_backend() == "postgres"


def postgres_storage_configured() -> bool:
    return postgres_storage_requested() and bool(get_database_url())


def load_psycopg2():
    try:
        import psycopg2  # type: ignore

        return psycopg2
    except Exception:
        return None


def connect_postgres(psycopg2_module: Optional[object] = None):
    module = psycopg2_module or load_psycopg2()
    database_url = get_database_url()
    if module is None or not database_url:
        raise RuntimeError("Postgres storage is not configured")
    return module.connect(database_url)


def probe_postgres_connection(psycopg2_module: Optional[object] = None) -> Dict[str, object]:
    if not postgres_storage_requested():
        return {
            "configured": False,
            "healthy": False,
            "reason": "storage_backend_not_postgres",
        }

    if not get_database_url():
        return {
            "configured": False,
            "healthy": False,
            "reason": "database_url_missing",
        }

    module = psycopg2_module or load_psycopg2()
    if module is None:
        return {
            "configured": True,
            "healthy": False,
            "reason": "psycopg2_unavailable",
        }

    try:
        with connect_postgres(module) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except Exception:
        return {
            "configured": True,
            "healthy": False,
            "reason": "connection_failed",
        }

    return {
        "configured": True,
        "healthy": True,
        "reason": None,
    }
