"""Assessment chat — AI translation with a per-(message, language) cache.

Reuses the Bedrock wrapper ``get_fireworks_response2`` (same call shape as
``policy_hub.template_chat_endpoint``: a fresh event loop driven synchronously
from the Flask request thread). Translations are cached in
``chat_message_translation`` so a message is translated at most once per
language.

Every translated payload carries the original text and an ``ai_translated``
flag so the frontend can show the source alongside the translation with the
"Translated by AI — AI can make mistakes" disclaimer.
"""

import asyncio

import pymysql.cursors

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger
from utils.fireworkzz import NORMAL_MODEL, get_fireworks_response2

from assessment_chat.schema import DEFAULT_LANG, SUPPORTED_LANGS

logger = get_logger(__name__)

AI_DISCLAIMER = "Translated by AI — AI can make mistakes."

# Human-readable names used in the translation prompt.
LANG_NAMES = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "ja": "Japanese",
    "zh": "Chinese (Mandarin, Simplified)",
}


def normalize_lang(lang: str | None) -> str:
    """Coerce an arbitrary lang code to a supported one, defaulting to English."""
    code = (lang or "").strip().lower()
    # Accept common aliases (e.g. 'zh-cn', 'ja-jp').
    code = code.split("-")[0].split("_")[0]
    return code if code in SUPPORTED_LANGS else DEFAULT_LANG


def _get_cached(message_id: str, lang: str) -> str | None:
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT translated_text FROM chat_message_translation "
                "WHERE message_id=%s AND lang=%s",
                (message_id, lang),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return row["translated_text"] if row else None


def _store_cached(message_id: str, lang: str, text: str, model: str) -> None:
    conn = connect_to_rds()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO chat_message_translation
                   (message_id, lang, translated_text, model)
                   VALUES (%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE translated_text=VALUES(translated_text),
                                           model=VALUES(model)""",
                (message_id, lang, text, model),
            )
        conn.commit()
    finally:
        conn.close()


def _translate_text(text: str, source_lang: str, target_lang: str, user_id: str) -> str | None:
    """Call the LLM to translate ``text`` into ``target_lang``.

    Returns the translation, or None if the call failed / returned no usable
    output (caller falls back to the original text + ai_translated=False).
    """
    src_name = LANG_NAMES.get(source_lang, "the source language")
    tgt_name = LANG_NAMES.get(target_lang, target_lang)
    prompt = (
        f"Translate the following message from {src_name} into {tgt_name}.\n"
        "Rules:\n"
        "- Output ONLY the translated text, with no preamble, notes, or quotes.\n"
        "- Preserve meaning, tone, names, numbers, and any formatting.\n"
        "- Do not answer or react to the content; translate it verbatim.\n\n"
        f"Message:\n{text}"
    )

    loop = asyncio.new_event_loop()
    try:
        raw = loop.run_until_complete(
            get_fireworks_response2(
                user_id=user_id,
                user_message=prompt,
                role="user",
                credits=None,
                temp=0.1,
            )
        )
    except Exception as exc:
        logger.error("chat translation LLM call failed: %s", exc)
        return None
    finally:
        loop.close()

    if not raw or raw == "INSUFFICIENT":
        return None
    return raw.strip()


def translate_message(
    message_id: str,
    original_text: str,
    original_lang: str,
    target_lang: str,
    requester_user_id: str,
) -> dict:
    """Return a translation payload for one message in the requester's language.

    Shape::

        {
          "text": <text shown to the reader>,
          "original_text": <always the untranslated source>,
          "original_lang": "en",
          "target_lang": "fr",
          "ai_translated": True/False,
          "disclaimer": "Translated by AI — AI can make mistakes." | None,
        }

    When ``target_lang`` equals the original, or translation is unavailable,
    ``ai_translated`` is False and ``text`` is the original.
    """
    source = normalize_lang(original_lang)
    target = normalize_lang(target_lang)

    base = {
        "original_text": original_text,
        "original_lang": source,
        "target_lang": target,
        "ai_translated": False,
        "disclaimer": None,
    }

    if target == source or not (original_text or "").strip():
        base["text"] = original_text
        return base

    cached = _get_cached(message_id, target)
    if cached is not None:
        base["text"] = cached
        base["ai_translated"] = True
        base["disclaimer"] = AI_DISCLAIMER
        return base

    translated = _translate_text(original_text, source, target, requester_user_id)
    if translated is None:
        # Graceful fallback: show the original, flag as not translated so the UI
        # doesn't falsely claim an AI translation happened.
        base["text"] = original_text
        return base

    _store_cached(message_id, target, translated, NORMAL_MODEL)
    base["text"] = translated
    base["ai_translated"] = True
    base["disclaimer"] = AI_DISCLAIMER
    return base
