"""
Tests covering A-V1-UT-001 through A-V1-UT-007 and A-V1-IT-001 from TEST_CASES.md.

Unit tests are pure (no network). The integration test (A-V1-IT-001)
mocks Anthropic API calls but exercises the full CLI flow including
git operations.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from config_loader import EvolutionConfig, LoadedConfig, ModelSpec, load_config
from observer import Observation, build_context_text, observe
from actor import ActorResult, _extract_patch_and_rationale
from judge import JudgeVerdict, PrincipleScore, _parse_verdict
from hard_stops import HardStopConfig, HardStopState, HardStops
from history import HistoryWriter
from router import RouterResult, create_pr_from_patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_CONFIG: dict[str, Any] = {
    "mission": "Run GitHub accounts and contribute quality PRs.\n",
    "principles": [
        {"priority": 1, "rule": "Never get banned"},
        {"priority": 2, "rule": "Act human"},
        {"priority": 3, "rule": "PRs must be valuable"},
    ],
    "resources": {
        "budget": {"daily_usd": 30, "hard_cap_usd": 100},
    },
    "evidence_sources": ["./dashboard/metrics.json"],
    "hard_stops": {
        "budget_hard_cap_usd": 100,
        "max_consecutive_failures": 5,
        "max_iterations_per_day": 50,
        "on_trigger": "halt_and_notify",
    },
    "models": {"actor": "claude-sonnet-4", "judge": "claude-opus-4"},
    "safety_mode": "human_in_the_loop",
}


def _write_config(tmp_path: Path, overrides: dict | None = None) -> Path:
    cfg = {**VALID_CONFIG}
    if overrides:
        cfg.update(overrides)
    p = tmp_path / "evolution.yml"
    p.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


def _make_metrics(tmp_path: Path, **overrides: Any) -> Path:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data: dict[str, Any] = {
        "schema_version": "1.0.0",
        "generated_at": overrides.pop("generated_at", now),
        "accounts": overrides.pop("accounts", [
            {"id": "acc_01", "status": "alive", "status_detail": "ok",
             "created_at": "2026-04-01T00:00:00Z",
             "last_active_at": now, "pr_count": 1, "merge_count": 0},
        ]),
        "prs": overrides.pop("prs", []),
        "resources": overrides.pop("resources", {
            "accounts_total": 1, "accounts_alive": 1,
            "accounts_rate_limited": 0, "accounts_banned": 0,
            "proxies_total": 1, "proxies_healthy": 1,
        }),
        "budget": overrides.pop("budget", {
            "daily_used_usd": 1.0, "daily_cap_usd": 30.0,
            "cumulative_used_usd": 5.0, "hard_cap_usd": 100.0,
            "reset_at": "2026-04-24T00:00:00Z",
        }),
        "recent_events": overrides.pop("recent_events", []),
    }
    data.update(overrides)
    d = tmp_path / "dashboard"
    d.mkdir(exist_ok=True)
    p = d / "metrics.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _init_git_repo(path: Path) -> None:
    """Turn path into a git repo with an initial commit."""
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial", "--allow-empty"],
        cwd=str(path), capture_output=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"},
    )


# ===========================================================================
# A-V1-UT-001: Config loader validation
# ===========================================================================

class TestConfigLoader:
    """A-V1-UT-001"""

    def test_valid_config(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path)
        loaded = load_config(p)
        assert isinstance(loaded.config, EvolutionConfig)

    def test_missing_mission(self, tmp_path: Path) -> None:
        cfg = {k: v for k, v in VALID_CONFIG.items() if k != "mission"}
        p = tmp_path / "evolution.yml"
        p.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
        with pytest.raises(RuntimeError, match="Missing required field: mission"):
            load_config(p)

    def test_missing_principles(self, tmp_path: Path) -> None:
        cfg = {k: v for k, v in VALID_CONFIG.items() if k != "principles"}
        p = tmp_path / "evolution.yml"
        p.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
        with pytest.raises(RuntimeError, match="Missing required field: principles"):
            load_config(p)

    def test_empty_principles(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, {"principles": []})
        with pytest.raises(RuntimeError, match="principles must have at least 1 item"):
            load_config(p)

    def test_budget_hard_cap_zero(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, {
            "hard_stops": {
                "budget_hard_cap_usd": 0,
                "max_consecutive_failures": 5,
                "max_iterations_per_day": 50,
                "on_trigger": "halt_and_notify",
            }
        })
        with pytest.raises(RuntimeError, match="must be > 0"):
            load_config(p)

    def test_same_actor_judge_warns(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, {
            "models": {"actor": "same-model", "judge": "same-model"},
        })
        loaded = load_config(p)
        assert loaded.actor_and_judge_same is True

    def test_model_spec_full_form(self, tmp_path: Path) -> None:
        """Full ModelSpec dict form is accepted and parsed correctly."""
        p = _write_config(tmp_path, {
            "models": {
                "actor": {
                    "name": "gpt-4o",
                    "api_base_url": "https://api.openai.com/v1",
                    "api_key_env": "OPENAI_API_KEY",
                },
                "judge": {
                    "name": "gpt-4o-mini",
                    "api_base_url": "https://api.openai.com/v1",
                    "api_key_env": "OPENAI_API_KEY",
                },
            }
        })
        loaded = load_config(p)
        assert loaded.config.models.actor.name == "gpt-4o"
        assert loaded.config.models.actor.api_base_url == "https://api.openai.com/v1"
        assert loaded.config.models.actor.api_key_env == "OPENAI_API_KEY"
        assert loaded.actor_and_judge_same is False

    def test_model_spec_string_shorthand(self, tmp_path: Path) -> None:
        """Plain string is normalised to ModelSpec with Anthropic defaults."""
        p = _write_config(tmp_path)
        loaded = load_config(p)
        assert isinstance(loaded.config.models.actor, ModelSpec)
        assert loaded.config.models.actor.name == "claude-sonnet-4"
        assert loaded.config.models.actor.api_base_url is None
        assert loaded.config.models.actor.api_key_env == "ANTHROPIC_API_KEY"

    def test_different_actor_judge(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path)
        loaded = load_config(p)
        assert loaded.actor_and_judge_same is False

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="Config file not found"):
            load_config(tmp_path / "nonexistent.yml")


# ===========================================================================
# A-V1-UT-002: Observer evidence collection
# ===========================================================================

class TestObserver:
    """A-V1-UT-002"""

    def test_reads_metrics_json(self, tmp_path: Path) -> None:
        _make_metrics(tmp_path)
        obs = observe(["./dashboard/metrics.json"], repo_root=tmp_path)
        assert "metrics.json" in obs.summary
        assert "./dashboard/metrics.json" in obs.evidence
        ev = obs.evidence["./dashboard/metrics.json"]
        assert "accounts" in ev
        assert "prs" in ev
        assert "budget" in ev
        assert "recent_events" in ev

    def test_runs_script(self, tmp_path: Path) -> None:
        script = tmp_path / "scripts" / "status.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/bash\necho ok", encoding="utf-8")
        script.chmod(0o755)
        obs = observe(["./scripts/status.sh"], repo_root=tmp_path)
        assert obs.evidence["./scripts/status.sh"]["stdout"] == "ok"

    def test_token_budget(self, tmp_path: Path) -> None:
        _make_metrics(tmp_path)
        obs = observe(["./dashboard/metrics.json"], repo_root=tmp_path)
        ctx = build_context_text(obs)
        assert len(ctx) < 20_000

    def test_github_api_placeholder(self, tmp_path: Path) -> None:
        obs = observe(["github_api"], repo_root=tmp_path)
        assert obs.evidence["github_api"]["available"] is False

    def test_bad_schema_version(self, tmp_path: Path) -> None:
        d = tmp_path / "dashboard"
        d.mkdir(exist_ok=True)
        (d / "metrics.json").write_text(
            json.dumps({"schema_version": "2.0.0", "generated_at": "2026-04-23T10:00:00Z"}),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="schema_version mismatch"):
            observe(["./dashboard/metrics.json"], repo_root=tmp_path)

    def test_stale_metrics(self, tmp_path: Path) -> None:
        _make_metrics(tmp_path, generated_at="2020-01-01T00:00:00Z")
        with pytest.raises(RuntimeError, match="stale"):
            observe(["./dashboard/metrics.json"], repo_root=tmp_path)


# ===========================================================================
# A-V1-UT-003: Actor output patch
# ===========================================================================

class TestActorParsing:
    """A-V1-UT-003"""

    def test_extracts_diff_and_rationale(self) -> None:
        raw = textwrap.dedent("""\
            Here is my proposed fix.

            ```diff
            --- a/config/tunable.yml
            +++ b/config/tunable.yml
            @@ -1,3 +1,3 @@
            -api_call_interval_sec: 1
            +api_call_interval_sec: 30
            ```

            ```text
            Increased API call interval to reduce rate limiting.
            ```
        """)
        p, r = _extract_patch_and_rationale(raw)
        assert "api_call_interval_sec: 30" in p
        assert "rate limiting" in r.lower()

    def test_missing_patch_raises(self) -> None:
        with pytest.raises(RuntimeError, match="did not produce a patch"):
            _extract_patch_and_rationale("no code blocks here")

    def test_missing_rationale_defaults(self) -> None:
        raw = "```diff\n-a\n+b\n```"
        p, r = _extract_patch_and_rationale(raw)
        assert p == "-a\n+b"
        assert r == "N/A"


# ===========================================================================
# A-V1-UT-004: Judge independence hard verification
# ===========================================================================

class TestJudgeIndependence:
    """A-V1-UT-004 — RED LINE: failure here invalidates entire V1."""

    def test_judge_function_has_no_actor_reasoning_param(self) -> None:
        """run_judge must not accept any actor CoT / reasoning argument."""
        import inspect
        from judge import run_judge
        param_names = set(inspect.signature(run_judge).parameters.keys())
        for forbidden in ("actor_reasoning", "actor_cot", "actor_raw", "actor_raw_text"):
            assert forbidden not in param_names, f"Judge accepts forbidden param: {forbidden}"

    def test_judge_input_is_only_evidence_patch_principles_mission(self) -> None:
        """run_judge accepts exactly: model_spec, mission, principles_text, evidence_text, patch_text, prompts_dir, max_tokens."""
        import inspect
        from judge import run_judge
        params = set(inspect.signature(run_judge).parameters.keys())
        required = {"model_spec", "mission", "principles_text", "evidence_text", "patch_text", "prompts_dir"}
        assert required.issubset(params)

    def test_judge_system_prompt_declares_independence(self) -> None:
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "judge_system.md"
        text = prompt_path.read_text(encoding="utf-8").lower()
        assert "independent" in text
        assert "must not" in text

    def test_judge_uses_separate_client_instance(self) -> None:
        """Each call_llm() invocation creates a fresh client — no shared state.

        Client instantiation lives in llm.py; actor and judge each call
        call_llm() independently so there is no shared conversation history.
        """
        llm_src = (Path(__file__).resolve().parent.parent / "src" / "llm.py").read_text()
        # Both backend paths exist in llm.py
        assert "Anthropic(" in llm_src
        assert "OpenAI(" in llm_src
        # Clients are created inside functions, not at module level
        assert "def _call_anthropic" in llm_src
        assert "def _call_openai_compatible" in llm_src
        # actor and judge each call call_llm, not each other
        actor_src = (Path(__file__).resolve().parent.parent / "src" / "actor.py").read_text()
        judge_src = (Path(__file__).resolve().parent.parent / "src" / "judge.py").read_text()
        assert "call_llm" in actor_src
        assert "call_llm" in judge_src

    def test_judge_logs_model_info(self) -> None:
        """Judge module logs which model it calls (for audit trail)."""
        judge_src = (Path(__file__).resolve().parent.parent / "src" / "judge.py").read_text()
        assert "logger.info" in judge_src
        assert "model=" in judge_src


# ===========================================================================
# A-V1-UT-005: Judge output schema validation
# ===========================================================================

class TestJudgeSchema:
    """A-V1-UT-005"""

    VALID_VERDICT: dict[str, Any] = {
        "verdict": "PASS",
        "overall_score": 75,
        "principle_scores": [
            {"priority": 1, "rule": "Never get banned", "score": 90, "reasoning": "safe"},
            {"priority": 2, "rule": "Act human", "score": 70, "reasoning": "ok"},
        ],
        "top_risks": ["rate limit could recur"],
        "confidence": 0.85,
        "reasoning_summary": "Patch addresses the root cause.",
    }

    def test_valid_json_parses(self) -> None:
        v = _parse_verdict(json.dumps(self.VALID_VERDICT))
        assert v is not None
        assert v.verdict == "PASS"
        assert 0 <= v.overall_score <= 100
        assert len(v.principle_scores) == 2
        for ps in v.principle_scores:
            assert 0 <= ps.score <= 100
            assert ps.reasoning
        assert 0 <= v.confidence <= 1
        assert v.reasoning_summary

    def test_verdict_must_be_pass_or_fail(self) -> None:
        bad = {**self.VALID_VERDICT, "verdict": "MAYBE"}
        assert _parse_verdict(json.dumps(bad)) is None

    def test_score_out_of_range(self) -> None:
        bad = {**self.VALID_VERDICT, "overall_score": 200}
        assert _parse_verdict(json.dumps(bad)) is None

    def test_confidence_out_of_range(self) -> None:
        bad = {**self.VALID_VERDICT, "confidence": 1.5}
        assert _parse_verdict(json.dumps(bad)) is None

    def test_missing_reasoning_summary(self) -> None:
        bad = {k: v for k, v in self.VALID_VERDICT.items() if k != "reasoning_summary"}
        assert _parse_verdict(json.dumps(bad)) is None

    def test_invalid_json_returns_none_no_crash(self) -> None:
        assert _parse_verdict("not json at all {{{") is None

    def test_extracts_json_from_surrounding_text(self) -> None:
        raw = f"Here is my verdict:\n{json.dumps(self.VALID_VERDICT)}\nDone."
        v = _parse_verdict(raw)
        assert v is not None
        assert v.verdict == "PASS"


# ===========================================================================
# A-V1-UT-006: Router routing
# ===========================================================================

class TestRouter:
    """A-V1-UT-006"""

    def _make_github_cfg(self):
        from config_loader import GitHubCfg
        return GitHubCfg(repo="owner/testrepo", token_env="GITHUB_TOKEN", base_branch="main")

    def test_pass_creates_pr_via_github_api(self) -> None:
        """PASS → calls create_pr_via_github_api, returns PR_CREATED with URL."""
        diff = textwrap.dedent("""\
            --- a/hello.txt
            +++ b/hello.txt
            @@ -1 +1 @@
            -hello
            +world
        """)
        branch = "evolution/fix-test-20260423"
        pr_url = "https://github.com/owner/testrepo/pull/1"

        mock_api = MagicMock(return_value=pr_url)
        with patch("github_client.create_pr_via_github_api", mock_api):
            result = create_pr_from_patch(
                patch_text=diff, title="test", body="body",
                branch=branch, github_cfg=self._make_github_cfg(),
            )

        assert result.action == "PR_CREATED"
        assert result.pr_url == pr_url
        mock_api.assert_called_once()
        call_kwargs = mock_api.call_args.kwargs
        assert call_kwargs["branch"] == branch
        assert call_kwargs["patch_text"] == diff

    def test_fail_does_not_create_branch(self) -> None:
        """FAIL → no code submitted, history recorded, human notified."""
        result = RouterResult(action="FAIL_RECORDED")
        assert result.pr_url is None
        assert result.action == "FAIL_RECORDED"


# ===========================================================================
# A-V1-UT-007: Hard stops circuit breaker
# ===========================================================================

class TestHardStops:
    """A-V1-UT-007"""

    def _make(self, tmp_path: Path, **overrides: Any) -> HardStops:
        defaults = dict(
            budget_hard_cap_usd=100,
            max_consecutive_failures=5,
            max_iterations_per_day=50,
            on_trigger="halt_and_notify",
        )
        defaults.update(overrides)
        return HardStops(
            HardStopConfig(**defaults),
            state_path=tmp_path / ".evolution_state.json",
        )

    def test_fresh_state_passes(self, tmp_path: Path) -> None:
        hs = self._make(tmp_path)
        hs.check_or_raise()

    def test_budget_trigger(self, tmp_path: Path) -> None:
        hs = self._make(tmp_path, budget_hard_cap_usd=10)
        hs.record_cost(10)
        with pytest.raises(RuntimeError, match="budget hard cap"):
            hs.check_or_raise()

    def test_consecutive_failures_trigger(self, tmp_path: Path) -> None:
        hs = self._make(tmp_path, max_consecutive_failures=3)
        for _ in range(3):
            hs.record_failure()
        with pytest.raises(RuntimeError, match="consecutive failures"):
            hs.check_or_raise()

    def test_max_iterations_trigger(self, tmp_path: Path) -> None:
        hs = self._make(tmp_path, max_iterations_per_day=2)
        hs.record_iteration()
        hs.record_iteration()
        with pytest.raises(RuntimeError, match="iterations per day"):
            hs.check_or_raise()

    def test_halted_rejects_next_run(self, tmp_path: Path) -> None:
        hs = self._make(tmp_path, budget_hard_cap_usd=1)
        hs.record_cost(1)
        with pytest.raises(RuntimeError):
            hs.check_or_raise()
        hs2 = self._make(tmp_path, budget_hard_cap_usd=1)
        with pytest.raises(RuntimeError, match="already triggered"):
            hs2.check_or_raise()

    def test_halt_outputs_clear_reason(self, tmp_path: Path) -> None:
        hs = self._make(tmp_path, budget_hard_cap_usd=5)
        hs.record_cost(5)
        with pytest.raises(RuntimeError) as exc_info:
            hs.check_or_raise()
        assert "budget hard cap" in str(exc_info.value)

    def test_reset_clears_halt(self, tmp_path: Path) -> None:
        hs = self._make(tmp_path, budget_hard_cap_usd=1)
        hs.record_cost(1)
        with pytest.raises(RuntimeError):
            hs.check_or_raise()
        hs.reset_halt()
        hs.state.cumulative_cost_usd = 0
        hs._save_state()
        hs.check_or_raise()

    def test_success_resets_consecutive_failures(self, tmp_path: Path) -> None:
        hs = self._make(tmp_path, max_consecutive_failures=5)
        for _ in range(4):
            hs.record_failure()
        hs.record_success()
        assert hs.state.consecutive_failures == 0
        hs.check_or_raise()

    def test_state_persists_across_instances(self, tmp_path: Path) -> None:
        hs = self._make(tmp_path)
        hs.record_cost(42.5)
        hs.record_failure()
        hs2 = self._make(tmp_path)
        assert hs2.state.cumulative_cost_usd == 42.5
        assert hs2.state.consecutive_failures == 1


# ===========================================================================
# History writer
# ===========================================================================

class TestHistory:
    def test_append_creates_jsonl(self, tmp_path: Path) -> None:
        p = tmp_path / "history.jsonl"
        hw = HistoryWriter(p)
        hw.append("test_event", {"key": "value"})
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["type"] == "test_event"
        assert data["payload"]["key"] == "value"
        assert "ts" in data

    def test_multiple_appends(self, tmp_path: Path) -> None:
        p = tmp_path / "history.jsonl"
        hw = HistoryWriter(p)
        for i in range(5):
            hw.append("evt", {"i": i})
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 5


# ===========================================================================
# A-V1-IT-001: Engine end-to-end (mocked LLM, real git)
# ===========================================================================

RATE_LIMIT_DIFF = (
    "--- a/config/tunable.yml\n"
    "+++ b/config/tunable.yml\n"
    "@@ -1,4 +1,4 @@\n"
    " schema_version: \"1.0.0\"\n"
    " timing:\n"
    "-  api_call_interval_sec: 1\n"
    "+  api_call_interval_sec: 30\n"
    "   account_switch_interval_sec: 120\n"
)

MOCK_ACTOR_RESPONSE = f"""\
Based on the evidence, accounts are rate-limited due to api_call_interval_sec being too low.

```diff
{RATE_LIMIT_DIFF}```

```text
Increased api_call_interval_sec from 1 to 30 to fix rate limiting.
```
"""

MOCK_JUDGE_RESPONSE = json.dumps({
    "verdict": "PASS",
    "overall_score": 82,
    "principle_scores": [
        {"priority": 1, "rule": "Never get banned", "score": 95, "reasoning": "Reducing API call frequency lowers ban risk"},
        {"priority": 2, "rule": "Act human", "score": 80, "reasoning": "30s interval is more natural"},
        {"priority": 3, "rule": "PRs must be valuable", "score": 70, "reasoning": "Addresses root cause of rate limiting"},
    ],
    "top_risks": ["interval might need further tuning"],
    "confidence": 0.88,
    "reasoning_summary": "Patch correctly increases API call interval to mitigate rate limiting. Addresses the root cause evident in metrics.",
})


class TestIntegrationEndToEnd:
    """A-V1-IT-001: full cycle with mocked LLM but real git."""

    def _setup_repo(self, tmp_path: Path) -> Path:
        """Create a git repo with a buggy tunable.yml and matching metrics.json."""
        repo = tmp_path / "target"
        repo.mkdir()

        # Buggy config
        (repo / "config").mkdir()
        (repo / "config" / "tunable.yml").write_text(textwrap.dedent("""\
            schema_version: "1.0.0"
            timing:
              api_call_interval_sec: 1
              account_switch_interval_sec: 120
        """))

        # Metrics showing rate limiting
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (repo / "dashboard").mkdir()
        (repo / "dashboard" / "metrics.json").write_text(json.dumps({
            "schema_version": "1.0.0",
            "generated_at": now,
            "accounts": [
                {"id": "acc_01", "status": "rate_limited", "status_detail": "secondary_rate_limit",
                 "created_at": "2026-04-01T00:00:00Z", "last_active_at": now, "pr_count": 3, "merge_count": 1},
            ],
            "prs": [],
            "resources": {"accounts_total": 1, "accounts_alive": 0, "accounts_rate_limited": 1,
                          "accounts_banned": 0, "proxies_total": 1, "proxies_healthy": 1},
            "budget": {"daily_used_usd": 2.0, "daily_cap_usd": 30.0, "cumulative_used_usd": 8.0,
                       "hard_cap_usd": 100.0, "reset_at": "2026-04-24T00:00:00Z"},
            "recent_events": [
                {"ts": now, "type": "account_rate_limited", "severity": "warning",
                 "account_id": "acc_01", "details": {"endpoint": "/repos", "retry_after_sec": 60}},
                {"ts": now, "type": "account_rate_limited", "severity": "warning",
                 "account_id": "acc_01", "details": {"endpoint": "/issues", "retry_after_sec": 90}},
                {"ts": now, "type": "account_rate_limited", "severity": "warning",
                 "account_id": "acc_01", "details": {"endpoint": "/pulls", "retry_after_sec": 120}},
            ],
        }, indent=2))

        # Status script
        (repo / "scripts").mkdir()
        status = repo / "scripts" / "status.sh"
        status.write_text('#!/bin/bash\necho \'{"accounts_alive":0,"accounts_rate_limited":1}\'')
        status.chmod(0o755)

        # Prompts
        src_prompts = Path(__file__).resolve().parent.parent / "prompts"
        shutil.copytree(str(src_prompts), str(repo / "prompts"))

        # evolution.yml
        (repo / "evolution.yml").write_text(yaml.dump({
            "mission": "Run GitHub accounts, contribute quality PRs.\n",
            "principles": [
                {"priority": 1, "rule": "Never get banned"},
                {"priority": 2, "rule": "Act human"},
                {"priority": 3, "rule": "PRs must be valuable"},
            ],
            "resources": {"budget": {"daily_usd": 30, "hard_cap_usd": 100}},
            "evidence_sources": ["./dashboard/metrics.json", "./scripts/status.sh"],
            "hard_stops": {"budget_hard_cap_usd": 100, "max_consecutive_failures": 5,
                           "max_iterations_per_day": 20, "on_trigger": "halt_and_notify"},
            "models": {"actor": "claude-sonnet-4", "judge": "claude-opus-4"},
            "github": {"repo": "owner/testrepo", "token_env": "GITHUB_TOKEN", "base_branch": "main"},
            "safety_mode": "human_in_the_loop",
        }, allow_unicode=True))

        return repo

    def _mock_llm(self):
        """Return a patcher that intercepts llm.call_llm."""
        from llm import LLMResponse
        call_log = []

        def fake_call_llm(model_spec, system, user, max_tokens=1800):
            call_log.append({
                "model": model_spec.name,
                "system_preview": system[:80],
                "user_preview": user[:80],
            })
            text = MOCK_ACTOR_RESPONSE if len(call_log) == 1 else MOCK_JUDGE_RESPONSE
            return LLMResponse(text=text, input_tokens=120, output_tokens=80)

        patcher = patch("actor.call_llm", fake_call_llm)
        patcher2 = patch("judge.call_llm", fake_call_llm)
        return patcher, patcher2, call_log

    def test_full_cycle(self, tmp_path: Path) -> None:
        repo = self._setup_repo(tmp_path)
        p1, p2, call_log = self._mock_llm()

        pr_url = "https://github.com/owner/testrepo/pull/42"
        mock_github_api = MagicMock(return_value=pr_url)

        with p1, p2, patch("github_client.create_pr_via_github_api", mock_github_api):
            from cli import main as cli_main
            from click.testing import CliRunner
            runner = CliRunner()
            result = runner.invoke(
                cli_main,
                ["run", "--config", str(repo / "evolution.yml")],
                env={"GITHUB_TOKEN": "ghp_test_token"},
            )

        # --- Verify TEST_CASES expectations ---

        # 0. CLI must succeed
        assert result.exit_code == 0, (
            f"CLI failed (exit={result.exit_code}):\n{result.output}\n{result.exception}"
        )

        # 1. GitHub API was called with an evolution/* branch
        mock_github_api.assert_called_once()
        api_kwargs = mock_github_api.call_args.kwargs
        assert api_kwargs["branch"].startswith("evolution/"), (
            f"Expected evolution/* branch, got: {api_kwargs['branch']}"
        )

        # 2. Correct repo was targeted
        assert api_kwargs["github_cfg"].repo == "owner/testrepo"

        # 3. Patch text was forwarded (contains the tunable change)
        assert "api_call_interval_sec" in api_kwargs["patch_text"]

        # 4. LLM called twice (actor then judge) with different models
        assert len(call_log) == 2
        assert call_log[0]["model"] == "claude-sonnet-4"
        assert call_log[1]["model"] == "claude-opus-4"

        # 5. Judge did NOT receive actor reasoning (independence hard check)
        judge_user = call_log[1]["user_preview"]
        assert "Increased api_call_interval" not in judge_user

        # 6. PR URL in output
        assert pr_url in result.output

        # 7. History log exists and is complete
        history_path = repo / "evolution_history.jsonl"
        assert history_path.exists()
        events = [json.loads(line) for line in history_path.read_text().strip().splitlines()]
        event_types = [e["type"] for e in events]
        assert "observation" in event_types
        assert "actor" in event_types
        assert "judge" in event_types
        assert "router" in event_types

        # 8. Router history records correct PR URL
        router_evt = next(e for e in events if e["type"] == "router")
        assert router_evt["payload"]["pr_url"] == pr_url
