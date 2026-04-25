from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from llm import call_llm

if TYPE_CHECKING:
    from config_loader import ModelSpec

logger = logging.getLogger("evolution.actor")


@dataclass
class ActorResult:
    patch: str
    rationale: str
    raw_text: str
    input_tokens: int = 0
    output_tokens: int = 0


def _read_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def run_actor(
    *,
    model_spec: "ModelSpec",
    mission: str,
    principles_text: str,
    context_text: str,
    prompts_dir: Path,
    max_tokens: int = 1800,
) -> ActorResult:
    system = _read_prompt(prompts_dir / "actor_system.md")
    user = "\n\n".join(
        [
            "# Mission",
            mission.strip(),
            "",
            "# Principles",
            principles_text.strip(),
            "",
            "# Context",
            context_text.strip(),
        ]
    )

    logger.info("Actor calling model=%s", model_spec.name)
    resp = call_llm(model_spec, system=system, user=user, max_tokens=max_tokens)
    logger.info("Actor response length=%d chars", len(resp.text))
    logger.debug("Actor raw response:\n%s", resp.text)

    try:
        patch, rationale = _extract_patch_and_rationale(resp.text)
    except RuntimeError as e:
        logger.error("Actor parse failed. Raw response was:\n%s", resp.text)
        raise
    return ActorResult(
        patch=patch,
        rationale=rationale,
        raw_text=resp.text,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
    )


def _extract_patch_and_rationale(raw_text: str) -> tuple[str, str]:
    """Extract unified diff and rationale from Actor's fenced-block response."""
    def pull_fence(tag: str) -> Optional[str]:
        start = raw_text.find(f"```{tag}")
        if start == -1:
            return None
        start = raw_text.find("\n", start)
        if start == -1:
            return None
        end = raw_text.find("```", start + 1)
        if end == -1:
            return None
        return raw_text[start + 1 : end].strip()

    patch = pull_fence("diff") or pull_fence("") or ""
    rationale = pull_fence("text") or ""

    if not patch.strip():
        raise RuntimeError("Actor did not produce a patch (expected fenced diff block)")
    if not rationale.strip():
        rationale = "N/A"
    return patch, rationale
