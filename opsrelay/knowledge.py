"""Embeddings and similarity search, shared by runbook retrieval and incident memory.

Two embedders behind one interface:

- `bedrock`: Amazon Titan Text Embeddings v2 (256 dimensions, normalized). An Amazon model: no
  AWS Marketplace subscription needed.
- `lexical`: a deterministic hashed bag of words and word pairs. No network, no cost; used offline,
  in tests, and as a fallback if Bedrock is unreachable.

OPSRELAY_EMBEDDINGS=auto (the default) uses bedrock when the agents run on Bedrock, else lexical.
Vectors are cached by text, so a corpus is embedded once per process.
"""

import hashlib
import json
import logging
import math
import re
from functools import lru_cache

from .config import get_settings

log = logging.getLogger(__name__)
LEXICAL_DIMENSIONS = 512
TITAN = "amazon.titan-embed-text-v2:0"
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was were with".split()
)


def _tokens(text: str) -> list[str]:
    words = [w for w in _WORD.findall(text.lower()) if w not in _STOP]
    return [w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words]  # crude plural folding


def lexical_vector(text: str) -> list[float]:
    vec = [0.0] * LEXICAL_DIMENSIONS
    words = _tokens(text)
    for feature in [*words, *(f"{a}_{b}" for a, b in zip(words, words[1:], strict=False))]:
        h = int(hashlib.md5(feature.encode()).hexdigest(), 16)  # noqa: S324 - a hash for bucketing, not security
        vec[h % LEXICAL_DIMENSIONS] += 1.0 if h & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@lru_cache(maxsize=4096)
def _titan(text: str, region: str) -> tuple[float, ...]:
    import boto3

    client = boto3.client("bedrock-runtime", region_name=region)
    body = json.dumps({"inputText": text[:20000], "dimensions": 256, "normalize": True})
    resp = client.invoke_model(modelId=TITAN, body=body, contentType="application/json", accept="application/json")
    return tuple(json.loads(resp["body"].read())["embedding"])


@lru_cache(maxsize=4096)
def _lexical(text: str) -> tuple[float, ...]:
    return tuple(lexical_vector(text))


def embedder_name() -> str:
    settings = get_settings()
    if settings.embeddings == "auto":
        return "bedrock" if settings.model_provider == "bedrock" else "lexical"
    return settings.embeddings


def embed(text: str, kind: str | None = None) -> tuple[str, tuple[float, ...]]:
    """(embedder used, vector). Falls back to lexical if Bedrock fails."""
    kind = kind or embedder_name()
    if kind == "bedrock":
        try:
            return "bedrock", _titan(text, get_settings().aws_region)
        except Exception as e:  # noqa: BLE001 - retrieval degrades, it doesn't fail the incident
            log.warning("Titan embeddings unavailable (%s); using lexical", e)
    return "lexical", _lexical(text)


def cosine(a: tuple[float, ...] | list[float], b: tuple[float, ...] | list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True))  # both are normalized


def clear_cache() -> None:
    _titan.cache_clear()
    _lexical.cache_clear()
