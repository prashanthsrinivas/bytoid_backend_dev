"""Microsoft Presidio analyzer singleton.

Used by the guardrail enforcer for entities that can't be reliably detected
with regex (PERSON names, LOCATION, ORGANIZATION, …).  The spaCy model is
heavy to load (~1-2 s for ``en_core_web_sm``), so the analyzer is built once
and cached.

Import is deferred so the module is safe to import in environments that
don't have ``presidio_analyzer`` / ``spacy`` installed — calls to
``analyze`` will return ``[]`` and ``is_available()`` will return ``False``
in that case.  The guardrail UI surfaces the availability flag so users
know NER-backed entities are dormant.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_analyzer: Any | None = None
_unavailable_reason: str | None = None  # caches the import/init failure


# Default spaCy model. ``en_core_web_lg`` gives better recall on PERSON /
# ORGANIZATION but is ~580 MB; the small model is the standard Presidio
# starting point and is what we declare in requirements.txt.
SPACY_MODEL = "en_core_web_sm"


def _build_analyzer() -> Any:
    """Construct and return a Presidio ``AnalyzerEngine``.

    Raises on any import/initialisation failure; callers wrap in try/except.
    """
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    nlp_config = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": SPACY_MODEL}],
    }
    provider = NlpEngineProvider(nlp_configuration=nlp_config)
    nlp_engine = provider.create_engine()
    return AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])


def get_analyzer() -> Any | None:
    """Return the shared analyzer, or ``None`` if Presidio is unavailable.

    First call pays the spaCy model load cost; subsequent calls are O(1).
    The reason for unavailability is logged once and cached so a missing
    dependency does not spam the logs on every guardrail evaluation.
    """
    global _analyzer, _unavailable_reason
    if _analyzer is not None:
        return _analyzer
    if _unavailable_reason is not None:
        return None
    with _lock:
        if _analyzer is not None:
            return _analyzer
        if _unavailable_reason is not None:
            return None
        try:
            _analyzer = _build_analyzer()
            logger.info("presidio: analyzer initialised (model=%s)", SPACY_MODEL)
            return _analyzer
        except Exception as exc:
            _unavailable_reason = f"{exc.__class__.__name__}: {exc}"
            logger.warning(
                "presidio: analyzer unavailable — NER-backed entities will not "
                "be detected. Cause: %s",
                _unavailable_reason,
            )
            return None


def is_available() -> bool:
    """Cheap probe — does not initialise the analyzer.

    Returns ``True`` only after a successful build.  Used by the metadata
    endpoint to flag NER entities as live vs. dormant.
    """
    if _analyzer is not None:
        return True
    if _unavailable_reason is not None:
        return False
    # We haven't tried yet — do a cheap module-presence check rather than
    # eagerly loading the spaCy model on every metadata fetch.
    import importlib.util

    for mod in ("presidio_analyzer", "spacy"):
        if importlib.util.find_spec(mod) is None:
            return False
    return True


def unavailable_reason() -> str | None:
    """Human-readable reason the analyzer is dormant, or ``None`` if it is
    available (or hasn't been probed yet)."""
    return _unavailable_reason


def analyze(text: str, entities: list[str]) -> list[dict]:
    """Run Presidio over ``text``, restricted to the given Presidio entity
    types (e.g. ``["PERSON", "LOCATION"]``).

    Returns a list of match dicts ``{entity, excerpt, span, score}``.  An
    empty list is returned when the analyzer is unavailable or no matches
    are found — the enforcer treats this the same way as a regex miss.
    """
    if not text or not entities:
        return []
    analyzer = get_analyzer()
    if analyzer is None:
        return []
    try:
        results = analyzer.analyze(text=text, entities=entities, language="en")
    except Exception as exc:
        logger.warning("presidio: analyze raised: %s", exc)
        return []
    out: list[dict] = []
    for r in results:
        out.append(
            {
                "entity": r.entity_type,
                "excerpt": text[r.start : r.end],
                "span": (r.start, r.end),
                "score": float(r.score),
            }
        )
    return out
