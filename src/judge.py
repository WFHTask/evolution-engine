from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from llm import call_llm

if TYPE_CHECKING:
    from config_loader import ModelSpec

logger = logging.getLogger("evolution.judge")


class PrincipleScore(BaseModel):
    priority: int
    rule: str
    score: int = Field(ge=0, le=100)
    reasoning: str


class JudgeVerdict(BaseModel):
    verdict: Literal["PASS", "FAIL"]
    overall_score: int = Field(ge=0, le=100)
    principle_scores: list[PrincipleScore]
    top_risks: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning_summary: str


@dataclass
class JudgeResult:
    verdict: Optional[JudgeVerdict]
    raw_text: str
    parse_error: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0


def _read_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def run_judge(
    *,
    model_spec: "ModelSpec",
    mission: str,
    principles_text: str,
    evidence_text: str,
    patch_text: str,
    prompts_dir: Path,
    max_tokens: int = 1200,
) -> JudgeResult:
    system = _read_prompt(prompts_dir / "judge_system.md")
    user = "\n\n".join(
        [
            "# Mission",
            mission.strip(),
            "",
            "# Principles",
            principles_text.strip(),
            "",
            "# Evidence (raw)",
            evidence_text.strip(),
            "",
            "# Proposed patch (final diff only)",
            patch_text.strip(),
            "",
            "# Output",
            "Return ONLY a single JSON object matching the required schema.",
        ]
    )

    logger.info(
        "Judge calling model=%s [INDEPENDENT SESSION] — input: mission, principles, evidence, patch; NO actor reasoning",
        model_spec.name,
    )
    resp = call_llm(model_spec, system=system, user=user, max_tokens=max_tokens)
    logger.info("Judge response length=%d chars", len(resp.text))

    verdict = _parse_verdict(resp.text)
    if verdict is None:
        logger.warning("Judge returned invalid JSON — recording as parse error")
        return JudgeResult(
            verdict=None,
            raw_text=resp.text,
            parse_error="Failed to parse Judge JSON",
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
        )
    logger.info(
        "Judge verdict=%s overall_score=%d confidence=%.2f",
        verdict.verdict, verdict.overall_score, verdict.confidence,
    )
    return JudgeResult(
        verdict=verdict,
        raw_text=resp.text,
        parse_error=None,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
    )


def _parse_verdict(raw_text: str) -> Optional[JudgeVerdict]:
    try:
        data = json.loads(raw_text)
    except Exception:
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(raw_text[start : end + 1])
        except Exception:
            return None

    try:
        return JudgeVerdict.model_validate(data)
    except ValidationError:
        return None
