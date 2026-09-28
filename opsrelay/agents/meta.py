"""Which agent made a decision, and with what: recorded on every agent event in the audit log,
so "why did the system do this?" can be answered after the fact (and replayed)."""

from .. import __version__
from ..config import get_settings
from ..store.base import sha256
from .prompts import PROMPTS


def agent_meta(role: str) -> dict[str, str]:
    settings = get_settings()
    model = f"offline:{role}" if settings.model_provider == "offline" else settings.bedrock_model_id
    return {
        "agent": role,
        "agent_version": __version__,
        "model": model,
        "prompt_version": sha256(PROMPTS.get(role, ""))[:10],
    }
