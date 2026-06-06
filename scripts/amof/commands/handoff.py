from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..app_paths import ensure_app_roots, get_app_paths

MAX_PAYLOAD_UTF8_BYTES = 40000
PAYLOAD_KIND_MAP = {
    "selected-text": "selected_text",
    "last-response": "last_response",
}
METADATA_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,63})$")


@dataclass(frozen=True)
class PreparedPayload:
    text: str
    character_count: int
    utf8_byte_count: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "character_count": self.character_count,
            "utf8_byte_count": self.utf8_byte_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class PreparedHandoffPacket:
    schema_version: int
    handoff_id: str
    source: str
    target: str
    payload_kind: str
    payload: PreparedPayload
    state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "handoff_id": self.handoff_id,
            "source": self.source,
            "target": self.target,
            "payload_kind": self.payload_kind,
            "payload": self.payload.to_dict(),
            "state": self.state,
        }


@dataclass(frozen=True)
class PreparedHandoffReceipt:
    status: str
    handoff_id: str
    packet_path: str
    character_count: int
    utf8_byte_count: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "handoff_id": self.handoff_id,
            "packet_path": self.packet_path,
            "character_count": self.character_count,
            "utf8_byte_count": self.utf8_byte_count,
            "sha256": self.sha256,
        }


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _emit_json_stdout(payload: dict[str, Any]) -> None:
    sys.stdout.write(_canonical_json(payload))
    sys.stdout.write("\n")


def _stderr(message: str) -> None:
    sys.stderr.write(message)
    if not message.endswith("\n"):
        sys.stderr.write("\n")


def _validate_metadata_label(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError(f"{field_name} is required.")
    if not METADATA_LABEL_RE.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must match {METADATA_LABEL_RE.pattern!r} and remain metadata only."
        )
    return normalized


def _payload_kind_from_cli(value: str) -> str:
    normalized = str(value or "").strip().lower()
    payload_kind = PAYLOAD_KIND_MAP.get(normalized)
    if not payload_kind:
        raise ValueError("payload kind is not supported.")
    return payload_kind


def _read_single_stdin_payload() -> PreparedPayload:
    raw = sys.stdin.buffer.read()
    if not raw:
        raise ValueError("payload stdin is empty.")
    if b"\x00" in raw:
        raise ValueError("payload stdin must not contain NUL bytes.")
    if len(raw) > MAX_PAYLOAD_UTF8_BYTES:
        raise ValueError(
            f"payload exceeds {MAX_PAYLOAD_UTF8_BYTES} UTF-8 bytes; received {len(raw)} bytes."
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("payload stdin must be valid UTF-8.") from exc
    return PreparedPayload(
        text=text,
        character_count=len(text),
        utf8_byte_count=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _render_preview(
    *, source: str, target: str, payload_kind: str, payload: PreparedPayload
) -> str:
    lines = [
        "[handoff] Preview",
        f"source: {source}",
        f"target: {target}",
        f"payload_kind: {payload_kind}",
        f"character_count: {payload.character_count}",
        f"utf8_byte_count: {payload.utf8_byte_count}",
        f"sha256: {payload.sha256}",
        "--- BEGIN PAYLOAD ---",
        payload.text,
        "--- END PAYLOAD ---",
    ]
    return "\n".join(lines) + "\n"


def _handoff_outbox_dir() -> Path:
    return get_app_paths().data_root / "handoff" / "outbox"


def _ensure_operator_only_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _generate_handoff_id(payload: PreparedPayload) -> str:
    return f"handoff-{time.time_ns():x}-{payload.sha256[:12]}"


def _write_packet(packet: PreparedHandoffPacket) -> Path:
    ensure_app_roots()
    handoff_root = _ensure_operator_only_dir(_handoff_outbox_dir().parent)
    outbox = _ensure_operator_only_dir(handoff_root / "outbox")
    packet_path = outbox / f"{packet.handoff_id}.json"
    payload = _canonical_json(packet.to_dict()) + "\n"
    fd = os.open(packet_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.chmod(packet_path, 0o600)
    return packet_path


def _build_packet(
    *, source: str, target: str, payload_kind: str, payload: PreparedPayload
) -> PreparedHandoffPacket:
    return PreparedHandoffPacket(
        schema_version=1,
        handoff_id=_generate_handoff_id(payload),
        source=source,
        target=target,
        payload_kind=payload_kind,
        payload=payload,
        state="prepared",
    )


def cmd_handoff_prepare(args: Any) -> int:
    try:
        source = _validate_metadata_label(
            str(getattr(args, "source", "")), field_name="source"
        )
        target = _validate_metadata_label(
            str(getattr(args, "target", "")), field_name="target"
        )
        payload_kind = _payload_kind_from_cli(str(getattr(args, "payload_kind", "")))
        payload = _read_single_stdin_payload()
    except ValueError as exc:
        _stderr(f"[handoff] {exc}")
        return 1

    _stderr(
        _render_preview(
            source=source, target=target, payload_kind=payload_kind, payload=payload
        )
    )
    if not bool(getattr(args, "confirm", False)):
        _stderr(
            "[handoff] Preview only; no packet written. Re-run with --confirm to write one local outbox packet."
        )
        return 0

    packet = _build_packet(
        source=source, target=target, payload_kind=payload_kind, payload=payload
    )
    packet_path = _write_packet(packet)
    receipt = PreparedHandoffReceipt(
        status="prepared",
        handoff_id=packet.handoff_id,
        packet_path=str(packet_path),
        character_count=payload.character_count,
        utf8_byte_count=payload.utf8_byte_count,
        sha256=payload.sha256,
    )
    _emit_json_stdout(receipt.to_dict())
    return 0


def cmd_handoff(args: Any) -> int:
    action = str(getattr(args, "handoff_cmd", "") or "").strip()
    if action == "prepare":
        return cmd_handoff_prepare(args)
    _stderr("Usage: amof handoff prepare [options]")
    return 1
