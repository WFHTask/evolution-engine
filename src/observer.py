from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class Observation:
    summary: str
    evidence: dict[str, Any]


def _parse_iso8601_utc(ts: str) -> datetime:
    # Accept "...Z"
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def _read_metrics_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("metrics.json must be a JSON object")
    schema_version = data.get("schema_version")
    if schema_version != "1.0.0":
        raise RuntimeError(f"metrics.json schema_version mismatch: {schema_version!r}")
    generated_at = data.get("generated_at")
    if not isinstance(generated_at, str):
        raise RuntimeError("metrics.json generated_at must be a string")
    age_sec = (datetime.now(timezone.utc) - _parse_iso8601_utc(generated_at)).total_seconds()
    if age_sec > 60:
        raise RuntimeError(f"metrics.json stale: generated_at age {int(age_sec)}s > 60s")
    return data


def _run_script(script: Path) -> str:
    if not script.exists():
        raise RuntimeError(f"script not found: {script}")
    proc = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"script failed: {script} (exit {proc.returncode}) {err}".strip())
    return out


def observe(evidence_sources: list[str], repo_root: Path) -> Observation:
    evidence: dict[str, Any] = {}
    summary_lines: list[str] = []

    for src in evidence_sources:
        src = str(src)
        if src == "github_api":
            evidence[src] = {"available": False, "note": "V1 placeholder"}
            summary_lines.append("- github_api: placeholder (not implemented in V1)")
            continue

        p = (repo_root / src).resolve()

        if p.name == "metrics.json" and p.suffix == ".json":
            metrics = _read_metrics_json(p)
            evidence[src] = metrics
            accounts = metrics.get("accounts", [])
            prs = metrics.get("prs", [])
            budget = metrics.get("budget", {})
            recent_events = metrics.get("recent_events", [])
            summary_lines.append(
                f"- metrics.json: accounts={len(accounts)} prs={len(prs)} recent_events={len(recent_events)} budget_daily_used={budget.get('daily_used_usd')}"
            )
            continue

        if p.suffix in {".sh", ".bash"} or p.name.endswith(".sh"):
            out = _run_script(p)
            evidence[src] = {"stdout": out}
            summary_lines.append(f"- script {src}: ok ({len(out)} chars)")
            continue

        if p.exists() and p.is_file():
            content = p.read_text(encoding="utf-8", errors="replace")
            evidence[src] = {"content": content}
            summary_lines.append(f"- file {src}: {len(content)} chars")
            continue

        raise RuntimeError(f"Unsupported evidence source or not found: {src}")

    summary = "\n".join(summary_lines)
    # Soft token control: truncate large evidence payloads while keeping summary.
    # Caller can further condense before sending to LLM.
    return Observation(summary=summary, evidence=evidence)


def build_context_text(observation: Observation, token_budget_chars: int = 20000) -> str:
    # Simple character-based budget to stay roughly under ~5k tokens.
    parts: list[str] = ["## Observation summary", observation.summary, "", "## Evidence"]
    blob = json.dumps(observation.evidence, ensure_ascii=False, indent=2)
    if len(blob) > token_budget_chars:
        blob = blob[: token_budget_chars] + "\n...TRUNCATED...\n"
    parts.append(blob)
    return "\n".join(parts)

