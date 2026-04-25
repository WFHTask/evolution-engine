from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class HistoryEvent:
    ts: str
    type: str
    payload: dict[str, Any]


class HistoryWriter:
    def __init__(self, path: Path):
        self.path = path

    def append(self, event_type: str, payload: dict[str, Any], ts: Optional[str] = None) -> None:
        event = HistoryEvent(ts=ts or utc_now_iso(), type=event_type, payload=payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")

