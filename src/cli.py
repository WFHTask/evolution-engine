from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from config_loader import load_config
from hard_stops import HardStopConfig, HardStops
from history import HistoryWriter
from observer import build_context_text, observe
from actor import run_actor
from judge import run_judge
from router import create_pr_from_patch


def _repo_root_from_config(config_path: Path) -> Path:
    return config_path.parent.resolve()


def _estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Conservative cost estimate using Claude Sonnet/Opus blended pricing.
    Actual rates vary by model; this ensures budget hard stop is functional."""
    return (input_tokens * 3 + output_tokens * 15) / 1_000_000


def _summarize_for_branch(rationale: str) -> str:
    """Extract a short slug from the rationale for the branch name."""
    import re
    text = rationale.lower().strip()
    for kw in ["rate limit", "rate-limit", "ratelimit"]:
        if kw in text:
            return "fix-rate-limit"
    for kw in ["timeout", "retry", "backoff"]:
        if kw in text:
            return f"fix-{kw}"
    words = re.findall(r"[a-z0-9]+", text)[:4]
    return "fix-" + "-".join(words) if words else "fix-patch"


def _principles_text(cfg) -> str:
    lines = []
    for p in sorted(cfg.principles, key=lambda x: x.priority):
        lines.append(f"- ({p.priority}) {p.rule}")
    return "\n".join(lines)


@click.group()
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable verbose logging")
def main(verbose: bool) -> None:
    """Evolution Engine V1 CLI."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), required=True)
def validate(config_path: Path) -> None:
    """Validate evolution.yml config."""
    try:
        loaded = load_config(config_path)
    except RuntimeError as e:
        raise click.ClickException(str(e))
    if loaded.actor_and_judge_same:
        click.echo("Warning: actor and judge should use different models", err=True)
    click.echo("Config valid")


@main.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), required=True)
def run(config_path: Path) -> None:
    """Run one evolution iteration."""
    try:
        loaded = load_config(config_path)
    except RuntimeError as e:
        raise click.ClickException(str(e))
    repo_root = _repo_root_from_config(config_path)

    history = HistoryWriter(repo_root / "evolution_history.jsonl")
    hard = HardStops(
        HardStopConfig(
            budget_hard_cap_usd=float(loaded.config.hard_stops.budget_hard_cap_usd),
            max_consecutive_failures=int(loaded.config.hard_stops.max_consecutive_failures),
            max_iterations_per_day=int(loaded.config.hard_stops.max_iterations_per_day),
            on_trigger=str(loaded.config.hard_stops.on_trigger),
        ),
        state_path=repo_root / ".evolution_state.json",
    )

    hard.check_or_raise()
    hard.record_iteration()

    # Observe
    try:
        obs = observe(list(loaded.config.evidence_sources), repo_root=repo_root)
    except RuntimeError as e:
        hard.record_failure()
        history.append("observer_error", {"error": str(e)})
        raise click.ClickException(str(e))
    context_text = build_context_text(obs)
    history.append("observation", {"summary": obs.summary})

    # Actor
    principles_text = _principles_text(loaded.config)
    prompts_dir = repo_root / "prompts"
    try:
        actor_res = run_actor(
            model_spec=loaded.config.models.actor,
            mission=loaded.config.mission,
            principles_text=principles_text,
            context_text=context_text,
            prompts_dir=prompts_dir,
        )
    except RuntimeError as e:
        hard.record_failure()
        history.append("actor_error", {"error": str(e)})
        raise click.ClickException(str(e))
    history.append("actor", {
        "rationale": actor_res.rationale,
        "patch": actor_res.patch,
        "raw_text": actor_res.raw_text,
        "tokens": {"input": actor_res.input_tokens, "output": actor_res.output_tokens},
    })
    hard.record_cost(_estimate_cost_usd(actor_res.input_tokens, actor_res.output_tokens))

    # Judge (NO actor reasoning)
    try:
        judge_res = run_judge(
            model_spec=loaded.config.models.judge,
            mission=loaded.config.mission,
            principles_text=principles_text,
            evidence_text=json.dumps(obs.evidence, ensure_ascii=False, indent=2),
            patch_text=actor_res.patch,
            prompts_dir=prompts_dir,
            max_tokens=2400,
        )
    except RuntimeError as e:
        hard.record_failure()
        history.append("judge_error", {"error": str(e)})
        raise click.ClickException(str(e))
    history.append(
        "judge",
        {
            "raw_text": judge_res.raw_text,
            "parse_error": judge_res.parse_error,
            "verdict": judge_res.verdict.model_dump() if judge_res.verdict else None,
            "tokens": {"input": judge_res.input_tokens, "output": judge_res.output_tokens},
        },
    )
    hard.record_cost(_estimate_cost_usd(judge_res.input_tokens, judge_res.output_tokens))

    if judge_res.verdict is None:
        hard.record_failure()
        raise click.ClickException("Judge output invalid JSON (see history)")

    if judge_res.verdict.confidence < 0.5:
        click.echo("Warning: judge confidence < 0.5; human review strongly recommended", err=True)

    if judge_res.verdict.verdict != "PASS":
        hard.record_failure()
        click.echo(f"FAIL (overall_score={judge_res.verdict.overall_score})")
        click.echo(judge_res.verdict.reasoning_summary)
        if judge_res.verdict.top_risks:
            click.echo("Top risks: " + "; ".join(judge_res.verdict.top_risks))
        return

    # PASS — build descriptive branch name
    from datetime import date as _date
    _slug = _summarize_for_branch(actor_res.rationale)
    title = f"evolution: {_slug}"
    branch = f"evolution/{_slug}-{_date.today().isoformat().replace('-', '')}"
    body = "\n".join(
        [
            "## Summary",
            actor_res.rationale.strip() or "N/A",
            "",
            "## Judge reasoning_summary",
            judge_res.verdict.reasoning_summary.strip(),
        ]
    )

    if loaded.config.github is None:
        hard.record_failure()
        history.append("router_error", {"error": "Missing 'github' section in config"})
        raise click.ClickException(
            "Missing 'github' section in evolution.yml — required for PR creation."
        )

    try:
        pr = create_pr_from_patch(
            patch_text=actor_res.patch,
            title=title,
            body=body,
            branch=branch,
            github_cfg=loaded.config.github,
        )
    except RuntimeError as e:
        hard.record_failure()
        history.append("router_error", {"error": str(e)})
        raise click.ClickException(str(e))
    history.append("router", {"action": pr.action, "pr_url": pr.pr_url, "details": pr.details})
    hard.record_success()
    click.echo(pr.pr_url or "PR created")


@main.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), required=True)
def history(config_path: Path) -> None:
    """Print evolution history (JSONL)."""
    repo_root = _repo_root_from_config(config_path)
    p = repo_root / "evolution_history.jsonl"
    if not p.exists():
        click.echo("(no history)")
        return
    sys.stdout.write(p.read_text(encoding="utf-8"))


@main.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), required=True)
def reset(config_path: Path) -> None:
    """Reset hard stop halted state."""
    repo_root = _repo_root_from_config(config_path)
    loaded = load_config(config_path)
    hard = HardStops(
        HardStopConfig(
            budget_hard_cap_usd=float(loaded.config.hard_stops.budget_hard_cap_usd),
            max_consecutive_failures=int(loaded.config.hard_stops.max_consecutive_failures),
            max_iterations_per_day=int(loaded.config.hard_stops.max_iterations_per_day),
            on_trigger=str(loaded.config.hard_stops.on_trigger),
        ),
        state_path=repo_root / ".evolution_state.json",
    )
    hard.reset_halt()
    click.echo("Reset ok")

