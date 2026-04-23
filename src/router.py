from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from config_loader import GitHubCfg


@dataclass
class RouterResult:
    action: str
    pr_url: Optional[str] = None
    details: Optional[str] = None


def create_pr_from_patch(
    *,
    patch_text: str,
    title: str,
    body: str,
    branch: str,
    github_cfg: "GitHubCfg",
) -> RouterResult:
    """Apply *patch_text* and open a PR via GitHub REST API.

    No local git binary or gh CLI required — all operations use HTTPS.
    """
    from github_client import create_pr_via_github_api

    url = create_pr_via_github_api(
        patch_text=patch_text,
        title=title,
        body=body,
        branch=branch,
        github_cfg=github_cfg,
    )
    return RouterResult(action="PR_CREATED", pr_url=url)
