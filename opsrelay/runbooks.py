"""Runbook retrieval (RAG): find the procedures that fit an incident.

Runbooks are Markdown files with YAML front matter, in opsrelay/runbooks/ and, optionally, your own
directory (OPSRELAY_RUNBOOK_DIR, which overrides built-ins with the same id):

    ---
    id: RB-001
    title: Error spike after a deployment
    categories: [bad-deploy]          # diagnosis categories it covers
    services: []                      # empty: any service
    actions: [rollback_deployment]    # the remediations it recommends ([] = diagnose, then escalate)
    ---
    Steps...

`search` ranks runbooks by semantic similarity (opsrelay.knowledge) plus a boost when the category or
service matches. Remediation proposals cite a runbook id, and the policy engine checks the proposed
action against that runbook's `actions`.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

import yaml

from . import knowledge
from .config import get_settings

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass(frozen=True)
class Runbook:
    id: str
    title: str
    categories: tuple[str, ...]
    services: tuple[str, ...]
    actions: tuple[str, ...]
    body: str

    @property
    def text(self) -> str:
        return f"{self.title}\n{' '.join(self.categories)}\n{self.body}"

    def public(self, score: float | None = None) -> dict:
        out = {
            "id": self.id,
            "title": self.title,
            "categories": list(self.categories),
            "services": list(self.services) or ["any"],
            "recommended_actions": list(self.actions) or ["none: diagnose, then escalate"],
            "steps": self.body.strip(),
        }
        if score is not None:
            out["score"] = round(score, 3)
        return out


def parse(text: str) -> Runbook:
    match = _FRONT.match(text.lstrip("﻿"))
    if not match:
        raise ValueError("a runbook needs YAML front matter between --- lines")
    meta = yaml.safe_load(match.group(1)) or {}
    if not meta.get("id") or not meta.get("title"):
        raise ValueError("a runbook needs an id and a title")
    return Runbook(
        id=str(meta["id"]),
        title=str(meta["title"]),
        categories=tuple(meta.get("categories") or ()),
        services=tuple(meta.get("services") or ()),
        actions=tuple(meta.get("actions") or ()),
        body=match.group(2),
    )


@lru_cache
def _load(extra_dir: str) -> dict[str, Runbook]:
    books: dict[str, Runbook] = {}
    for entry in sorted(files("opsrelay").joinpath("runbooks").iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".md"):
            rb = parse(entry.read_text(encoding="utf-8"))
            books[rb.id] = rb
    if extra_dir:
        for path in sorted(Path(extra_dir).glob("*.md")):
            rb = parse(path.read_text(encoding="utf-8"))
            books[rb.id] = rb  # yours override the built-ins
    return books


def all_runbooks() -> dict[str, Runbook]:
    return _load(get_settings().runbook_dir)


def get(runbook_id: str) -> Runbook | None:
    return all_runbooks().get(runbook_id)


def search(
    query: str, k: int = 3, category: str | None = None, service: str | None = None
) -> list[tuple[Runbook, float]]:
    _, q = knowledge.embed(query)
    scored = []
    for rb in all_runbooks().values():
        _, v = knowledge.embed(rb.text)
        score = 0.7 * knowledge.cosine(q, v)
        if category and category in rb.categories:
            score += 0.2
        if service and service in rb.services:
            score += 0.1
        scored.append((rb, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:k]


def clear_cache() -> None:
    _load.cache_clear()
