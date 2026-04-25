from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


class Principle(BaseModel):
    priority: int
    rule: str


class ResourceItem(BaseModel):
    path: Optional[str] = None
    health_check: Optional[str] = None
    description: Optional[str] = None

    @field_validator("path")
    @classmethod
    def _validate_env_path(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if v.startswith("env:"):
            env_name = v[len("env:") :].strip()
            if not env_name:
                raise ValueError("env: path must include a variable name")
        return v


def resolve_env_path(raw: str) -> str:
    """Expand ``env:VAR_NAME`` at runtime (not at config-load time).

    V1 note: Observer uses ``evidence_sources`` (not ``resources``) for evidence gathering,
    so this function is not yet called by the core loop. It is provided for V2 integration
    when health checks (resources.accounts.health_check, resources.proxies.health_check)
    are wired into the Observer.
    """
    if raw.startswith("env:"):
        env_name = raw[len("env:"):].strip()
        resolved = os.getenv(env_name)
        if not resolved:
            raise RuntimeError(f"env var not set: {env_name}")
        return resolved
    return raw


class Budget(BaseModel):
    daily_usd: float
    hard_cap_usd: float

    @model_validator(mode="after")
    def _check_budget(self) -> "Budget":
        if self.daily_usd <= 0:
            raise ValueError("budget.daily_usd must be > 0")
        if self.hard_cap_usd <= 0:
            raise ValueError("budget.hard_cap_usd must be > 0")
        return self


class Resources(BaseModel):
    # V1: budget is enforced; accounts/proxies/target_repos/personas are validated but
    # their health_check scripts are not yet wired into the Observer loop (V2 scope).
    accounts: Optional[ResourceItem] = None
    proxies: Optional[ResourceItem] = None
    target_repos: Optional[ResourceItem] = None
    personas: Optional[ResourceItem] = None
    budget: Budget


class HardStopsCfg(BaseModel):
    budget_hard_cap_usd: float
    max_consecutive_failures: int
    max_iterations_per_day: int
    on_trigger: str = "halt_and_notify"

    @model_validator(mode="after")
    def _check(self) -> "HardStopsCfg":
        if self.budget_hard_cap_usd <= 0:
            raise ValueError("hard_stops.budget_hard_cap_usd must be > 0")
        if self.max_consecutive_failures <= 0:
            raise ValueError("hard_stops.max_consecutive_failures must be > 0")
        if self.max_iterations_per_day <= 0:
            raise ValueError("hard_stops.max_iterations_per_day must be > 0")
        return self


class ModelSpec(BaseModel):
    """Single LLM endpoint config.

    Short-hand (string) form is auto-normalised by ModelsCfg:
        actor: "claude-sonnet-4"   →  ModelSpec(name="claude-sonnet-4")

    Full form (custom / third-party OpenAI-compatible API):
        actor:
          name: "gpt-4o"
          api_base_url: "https://api.openai.com/v1"
          api_key_env: "OPENAI_API_KEY"

    ``env:`` prefix — keep your YAML secret-free by deferring to env vars:
        actor:
          name: "env:ACTOR_MODEL"           # resolved at call time
          api_base_url: "env:ACTOR_API_BASE_URL"
          api_key_env: "ACTOR_API_KEY"      # already an env-var name, unchanged
    """
    name: str
    api_base_url: Optional[str] = None   # plain URL or "env:VAR_NAME"; None → Anthropic native SDK
    api_key_env: str = "ANTHROPIC_API_KEY"


class ModelsCfg(BaseModel):
    actor: ModelSpec
    judge: ModelSpec

    @model_validator(mode="before")
    @classmethod
    def _normalize_strings(cls, data: Any) -> Any:
        """Allow plain strings as shorthand for ModelSpec."""
        if isinstance(data, dict):
            for field in ("actor", "judge"):
                v = data.get(field)
                if isinstance(v, str):
                    data[field] = {"name": v}
        return data


class GitHubCfg(BaseModel):
    """GitHub repository target for PR creation.

    ``repo`` may be a plain ``owner/repo`` string or ``env:VAR_NAME``:
        github:
          repo: "env:GITHUB_REPO"
          token_env: "GITHUB_TOKEN"
          base_branch: "main"
    """
    repo: str                       # "owner/repo" or "env:VAR_NAME"
    token_env: str = "GITHUB_TOKEN"  # env var that holds the Personal Access Token
    base_branch: str = "main"


class EvolutionConfig(BaseModel):
    mission: str = Field(min_length=1)
    principles: list[Principle] = Field(min_length=1)
    resources: Resources
    evidence_sources: list[str] = Field(min_length=1)
    hard_stops: HardStopsCfg
    models: ModelsCfg
    github: Optional[GitHubCfg] = None   # required when Router needs to create PRs
    # V1 only supports human_in_the_loop; validated but auto-merge is not yet implemented.
    safety_mode: Literal["human_in_the_loop"] = "human_in_the_loop"

    @model_validator(mode="after")
    def _warn_same_model(self) -> "EvolutionConfig":
        # Warning is handled by caller; keep validation pass.
        return self


@dataclass(frozen=True)
class LoadedConfig:
    config: EvolutionConfig
    raw: dict[str, Any]
    path: Path

    @property
    def actor_and_judge_same(self) -> bool:
        return self.config.models.actor.name == self.config.models.judge.name


def _friendly_validation_error(e: ValidationError) -> str:
    """Convert Pydantic errors to human-readable messages matching TEST_CASES wording."""
    parts: list[str] = []
    for err in e.errors():
        loc = ".".join(str(l) for l in err["loc"])
        typ = err.get("type", "")
        msg = err.get("msg", "")
        if typ == "too_short" and loc:
            parts.append(f"{loc} must have at least 1 item")
        elif typ == "value_error":
            parts.append(msg.removeprefix("Value error, "))
        else:
            parts.append(f"{loc}: {msg}")
    return "; ".join(parts) if parts else str(e)


def load_config(path: Path) -> LoadedConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(f"Config file not found: {path}")
    except Exception as e:
        raise RuntimeError(f"Failed to read config: {e}")

    if not isinstance(raw, dict):
        raise RuntimeError("Config must be a YAML mapping/object at top-level")

    # Produce friendlier missing-field errors aligned with TEST_CASES.md.
    required = ["mission", "principles", "resources", "evidence_sources", "hard_stops", "models"]
    for key in required:
        if key not in raw:
            raise RuntimeError(f"Missing required field: {key}")

    try:
        cfg = EvolutionConfig.model_validate(raw)
    except ValidationError as e:
        raise RuntimeError(_friendly_validation_error(e))

    return LoadedConfig(config=cfg, raw=raw, path=path)

