"""GitHub REST API operations — no local git binary required.

Uses PyGithub for high-level GitHub API access and unidiff for patch parsing.
All state changes happen via HTTPS API calls; nothing is written to the local
filesystem (no git clone, no git apply, no gh CLI).

Flow
----
patch_text (unified diff)
    ↓ unidiff.PatchSet
    ↓ for each file: new blob via /git/blobs
    ↓ create tree  via /git/trees   (merging with base tree)
    ↓ create commit via /git/commits
    ↓ create branch  via /git/refs
    ↓ open PR        via /pulls
    → PR URL
"""
from __future__ import annotations

import base64
import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config_loader import GitHubCfg

logger = logging.getLogger("evolution.github")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_diff(patch_text: str) -> str:
    """Rewrite @@ hunk headers so line-count fields match actual content.

    LLMs sometimes emit incorrect or placeholder hunk headers, e.g.:
      @@ -X,Y +X,Y @@          (literal placeholders)
      @@ -15,10 +15,10 @@      (counts claim 10 lines but body has 3)

    unidiff.PatchSet is strict and raises ``UnidiffParseError: Hunk is
    shorter than expected`` in both cases.  This function recounts source
    and target lines from the actual body and rewrites every header,
    making downstream parsing robust to LLM output variance.
    """
    # Matches both numeric and placeholder starts: @@ -<ss>[,<sc>] +<ts>[,<tc>] @@<rest>
    hunk_re = re.compile(r"^@@ -(\S+?)(?:,\S+?)? \+(\S+?)(?:,\S+?)? @@(.*)", re.DOTALL)

    out: list[str] = []
    lines = patch_text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        raw = lines[i]
        m = hunk_re.match(raw.rstrip("\r\n"))
        if m:
            raw_ss, raw_ts, rest = m.group(1), m.group(2), m.group(3)
            # Use numeric start if available; fall back to 1 for placeholders
            ss = raw_ss if raw_ss.isdigit() else "1"
            ts = raw_ts if raw_ts.isdigit() else "1"

            # Collect hunk body lines until the next header or file boundary
            body: list[str] = []
            i += 1
            while i < len(lines):
                nxt = lines[i].rstrip("\r\n")
                if nxt.startswith("@@") or nxt.startswith("diff "):
                    break
                body.append(lines[i])
                i += 1

            # Recount: lines not starting with '+' count for source;
            #          lines not starting with '-' count for target
            src = sum(1 for ln in body if not ln.startswith("+"))
            tgt = sum(1 for ln in body if not ln.startswith("-"))
            out.append(f"@@ -{ss},{src} +{ts},{tgt} @@{rest}\n")
            out.extend(body)
        else:
            out.append(raw)
            i += 1
    return "".join(out)


def _apply_hunks(original: str, patched_file) -> str:  # type: ignore[no-untyped-def]
    """Apply unidiff hunks to *original* file content; return new content."""
    lines = original.splitlines(keepends=True)
    result: list[str] = []
    orig_idx = 0
    for hunk in patched_file:
        # unidiff source_start is 1-indexed
        hunk_start = hunk.source_start - 1
        while orig_idx < hunk_start and orig_idx < len(lines):
            result.append(lines[orig_idx])
            orig_idx += 1
        for line in hunk:
            if line.is_context:
                if orig_idx < len(lines):
                    result.append(lines[orig_idx])
                orig_idx += 1
            elif line.is_added:
                result.append(line.value)
            elif line.is_removed:
                orig_idx += 1
    result.extend(lines[orig_idx:])
    return "".join(result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_pr_via_github_api(
    *,
    patch_text: str,
    title: str,
    body: str,
    branch: str,
    github_cfg: "GitHubCfg",
) -> str:
    """Apply *patch_text* to the target repo and open a PR via GitHub API.

    Returns the HTML URL of the newly created PR.
    Raises ``RuntimeError`` on any failure (bad token, repo not found, etc.).
    """
    try:
        from github import Github, GithubException, InputGitTreeElement
        from unidiff import PatchSet
    except ImportError as exc:
        raise ImportError(
            f"Missing dependency: {exc}. Run: pip install PyGithub unidiff"
        ) from exc

    from config_loader import resolve_env_path

    repo_name = resolve_env_path(github_cfg.repo)
    token = os.getenv(github_cfg.token_env)
    if not token:
        raise RuntimeError(
            f"{github_cfg.token_env} is not set — set it or run `gh auth login`"
        )
    base_branch = github_cfg.base_branch

    logger.info(
        "GitHub PR: repo=%s branch=%s base=%s",
        repo_name, branch, base_branch,
    )

    g = Github(token)
    try:
        repo = g.get_repo(repo_name)
    except GithubException as exc:
        msg = exc.data.get("message", str(exc)) if isinstance(exc.data, dict) else str(exc)
        raise RuntimeError(f"Cannot access repo '{repo_name}': {msg}") from exc

    # Normalize hunk headers before parsing (handles LLM placeholder/wrong counts)
    patch_text = _normalize_diff(patch_text)
    patch_set = PatchSet(patch_text)
    if not patch_set:
        raise RuntimeError("Patch produced no file changes after parsing")

    # Get base commit + tree
    try:
        base_ref = repo.get_branch(base_branch)
    except GithubException as exc:
        msg = exc.data.get("message", str(exc)) if isinstance(exc.data, dict) else str(exc)
        raise RuntimeError(f"Cannot get branch '{base_branch}': {msg}") from exc

    base_sha = base_ref.commit.sha
    base_commit = repo.get_git_commit(base_sha)
    base_tree = repo.get_git_tree(base_commit.tree.sha, recursive=True)

    # path → sha map for existing files (used for modified-file lookups)
    existing_paths = {item.path: item.sha for item in base_tree.tree if item.type == "blob"}

    # Build new tree entries
    elements: list[InputGitTreeElement] = []
    for pf in patch_set:
        path = pf.path  # unidiff strips the a/ b/ prefix

        if pf.is_added_file:
            content = "".join(
                line.value for hunk in pf for line in hunk if line.is_added
            )
            blob = repo.create_git_blob(
                content=base64.b64encode(content.encode()).decode(),
                encoding="base64",
            )
            logger.info("GitHub blob (new)      %s → %s", path, blob.sha[:8])
            elements.append(InputGitTreeElement(path, "100644", "blob", sha=blob.sha))

        elif pf.is_removed_file:
            logger.info("GitHub blob (delete)   %s", path)
            # sha=None signals deletion to the GitHub API
            elements.append(InputGitTreeElement(path, "100644", "blob", sha=None))

        else:
            # Modified file — fetch original, apply hunks, create new blob
            if path in existing_paths:
                file_obj = repo.get_contents(path, ref=base_branch)
                # get_contents may return list for directories; handle gracefully
                if isinstance(file_obj, list):
                    file_obj = file_obj[0]
                original = base64.b64decode(file_obj.content).decode("utf-8", errors="replace")  # type: ignore[union-attr]
                content = _apply_hunks(original, pf)
            else:
                logger.warning("Modified file '%s' not in base tree; treating as new", path)
                content = "".join(
                    line.value for hunk in pf for line in hunk if line.is_added
                )
            blob = repo.create_git_blob(
                content=base64.b64encode(content.encode()).decode(),
                encoding="base64",
            )
            logger.info("GitHub blob (modified) %s → %s", path, blob.sha[:8])
            elements.append(InputGitTreeElement(path, "100644", "blob", sha=blob.sha))

    if not elements:
        raise RuntimeError("Patch produced no actionable file entries")

    # Create tree → commit → branch ref → PR
    new_tree = repo.create_git_tree(elements, base_tree=base_tree)
    new_commit = repo.create_git_commit(
        message=title,
        tree=new_tree,
        parents=[base_commit],
    )
    repo.create_git_ref(ref=f"refs/heads/{branch}", sha=new_commit.sha)
    logger.info("GitHub branch created: %s @ %s", branch, new_commit.sha[:8])

    pr = repo.create_pull(title=title, body=body, head=branch, base=base_branch)
    logger.info("GitHub PR opened: %s", pr.html_url)
    return pr.html_url
