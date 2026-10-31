from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def ts(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def iso(value: datetime) -> str:
    return ts(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ist_day(value: str | datetime):
    return ts(value).astimezone(IST).date()


def buffering_seconds(event: dict) -> float:
    try:
        return (ts(event["sent_at"]) - ts(event["occurred_at"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        return 0.0


def event_time(event: dict) -> datetime:
    """Estimate occurrence under a shared phone offset; never allow negative buffering.

    Receipt remains the knowledge cutoff. This estimate is not a trusted clock and
    buffered events require conservative outcome/eligibility treatment.
    """
    return ts(event["received_at"]) - timedelta(seconds=max(0.0, buffering_seconds(event)))


def event_is_buffered(event: dict, threshold_s: float = 600) -> bool:
    return buffering_seconds(event) > threshold_s


def jsonl(path: str | Path):
    with Path(path).open(encoding="utf-8") as stream:
        for n, line in enumerate(stream, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON at {Path(path).name}:{n}") from exc


def write_json(path: str | Path, value):
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(dest)


def write_jsonl(path: str | Path, rows: Iterable[dict]):
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    tmp.replace(dest)


def stable_id(prefix: str, *parts) -> str:
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]
    return f"{prefix}-{digest}"


def available(title: dict, at: datetime, withdrawn: dict | None = None) -> bool:
    if not title or ts(title["available_from"]) > at:
        return False
    if title.get("available_until") and at >= ts(title["available_until"]):
        return False
    cutoff = (withdrawn or {}).get(title["title_id"])
    return not cutoff or at < ts(cutoff)
