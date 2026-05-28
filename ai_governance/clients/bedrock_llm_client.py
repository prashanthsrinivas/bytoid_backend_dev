"""Custom Giskard LLM client backed by Bedrock kimi-k2.5.

Giskard / RAGET default to OpenAI.  This routes giskard's question generation
and answer grading through the SAME Bedrock model the live RAG pipeline uses
(``utils.fireworkzz.bedrock_runtime``), so evaluation and serving share a model
and no OpenAI key is needed.

These are eval/probe calls: they hit Bedrock directly and intentionally bypass
the per-user credit system (the ``utils.fireworkzz`` wrappers are what meter
real user traffic; only the RAGET ``answer_fn`` — the actual RAG pipeline —
consumes credits).

VERSION NOTE — the giskard LLM-client API has shifted across the 2.x line:
  * ``ChatMessage`` lives in ``giskard.llm.client`` (or ``.client.base``).
  * registration is ``giskard.llm.set_default_client`` (or
    ``giskard.llm.client.set_default_client``).
  * ``complete()`` has gained kwargs (``caller_id``, ``seed``, ``format`` …).
This wrapper imports defensively and accepts ``**kwargs`` so a minor giskard
bump won't break it.  If the contract has moved beyond what we can satisfy,
``set_default_bedrock_client`` raises ``GiskardUnavailable`` rather than letting
giskard silently fall back to OpenAI.
"""

from __future__ import annotations

import importlib
import json
import logging

from ai_governance.clients.giskard_client import GiskardUnavailable, require_giskard

logger = logging.getLogger(__name__)

# Mirror utils.fireworkzz.NORMAL_MODEL — the model the RAG pipeline serves with.
EVAL_MODEL = "moonshotai.kimi-k2.5"

_registered = False


def _chat_message_cls():
    """Locate giskard's ``ChatMessage`` across known 2.x import paths."""
    for path in ("giskard.llm.client", "giskard.llm.client.base"):
        try:
            mod = importlib.import_module(path)
        except Exception:
            logger.debug("bedrock_llm_client: import %s failed", path, exc_info=True)
            continue
        cls = getattr(mod, "ChatMessage", None)
        if cls is not None:
            return cls
    raise GiskardUnavailable("giskard ChatMessage class not found")


def _llm_client_base():
    """Return giskard's ``LLMClient`` base if importable, else ``object``."""
    try:
        from giskard.llm.client import LLMClient

        return LLMClient
    except Exception:
        return object


def _bedrock_generate(messages, temperature, max_tokens) -> str:
    """Call Bedrock with giskard ChatMessages; return the text (``""`` on error)."""
    from utils.fireworkzz import bedrock_runtime, extract_bedrock_text

    payload = {
        "messages": [
            {
                "role": getattr(m, "role", None) or "user",
                "content": [{"type": "text", "text": getattr(m, "content", "") or ""}],
            }
            for m in messages
        ],
        "temperature": float(temperature if temperature is not None else 0.0),
        "max_tokens": int(max_tokens or 2048),
    }
    try:
        resp = bedrock_runtime.invoke_model(
            modelId=EVAL_MODEL,
            body=json.dumps(payload),
            contentType="application/json",
            accept="application/json",
        )
        body = json.loads(resp["body"].read())
        return extract_bedrock_text(body)
    except Exception as exc:
        # One throttle/timeout must not abort a whole testset — degrade to empty.
        logger.warning("bedrock_llm_client: eval completion failed: %s", exc)
        return ""


def _build_client():
    base = _llm_client_base()
    chat_message = _chat_message_cls()

    class BedrockGiskardClient(base):
        """giskard ``LLMClient`` whose ``complete`` is served by Bedrock."""

        def __init__(self, model: str = EVAL_MODEL):
            self.model = model

        def complete(
            self,
            messages,
            temperature: float = 0.0,
            max_tokens: int | None = 2048,
            caller_id=None,
            seed=None,
            format=None,
            **kwargs,
        ):
            text = _bedrock_generate(messages, temperature, max_tokens)
            return chat_message(role="assistant", content=text)

    return BedrockGiskardClient()


def _try_set_embeddings() -> None:
    """Best-effort: route giskard's embeddings through Fireworks (4096-dim, the
    same space the LanceDB store uses).  Optional — if anything is missing,
    RAGET falls back to giskard's default embedding."""
    try:
        from giskard.llm.embeddings import set_default_embedding
        from giskard.llm.embeddings.base import BaseEmbedding
    except Exception:
        return  # giskard version lacks pluggable embeddings — leave default

    try:
        import numpy as np

        from utils.async_check import run_async
        from utils.fireworkzz import get_firework_embedding

        fw_emb = run_async(get_firework_embedding())
    except Exception as exc:
        logger.info("bedrock_llm_client: Fireworks embedding unavailable: %s", exc)
        return

    class FireworksGiskardEmbedding(BaseEmbedding):
        def embed(self, texts):
            if isinstance(texts, str):
                texts = [texts]
            return np.array(fw_emb.embed_documents(list(texts)))

    try:
        set_default_embedding(FireworksGiskardEmbedding())
    except Exception as exc:
        logger.info("bedrock_llm_client: could not set giskard embedding: %s", exc)


def set_default_bedrock_client(force: bool = False) -> None:
    """Register the Bedrock client as giskard's default LLM (idempotent).

    Call once at the top of every scan task body that uses giskard's LLM
    features (RAGET, LLM scan) BEFORE ``generate_testset`` / ``scan``.
    """
    global _registered
    if _registered and not force:
        return

    require_giskard()  # raises GiskardUnavailable if giskard is absent
    client = _build_client()

    set_fn = None
    try:
        from giskard.llm import set_default_client as set_fn
    except Exception:
        try:
            from giskard.llm.client import set_default_client as set_fn
        except Exception:
            set_fn = None
    if set_fn is None:
        raise GiskardUnavailable("giskard set_default_client entry point not found")

    set_fn(client)
    _try_set_embeddings()
    _registered = True
