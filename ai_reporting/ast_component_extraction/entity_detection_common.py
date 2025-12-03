from typing import Optional
import re
from difflib import get_close_matches
from .entity_map_common import ENTITY_CANONICAL_MAP

_plural_regex = re.compile(r"(s|es)$", re.IGNORECASE)


def normalize_entity(raw: str) -> str:
    """
    Normalize user-provided tokens:
      - lowercase
      - strip punctuation
      - normalize whitespace
      - basic plural stripping (s/es)
    """
    if not raw:
        return ""

    token = raw.lower().strip()
    token = re.sub(r"[^\w\s]", " ", token)
    token = re.sub(r"\s+", " ", token).strip()

    # strip plural form (simple rule)
    if len(token) > 3 and _plural_regex.search(token):
        token = re.sub(r"(s|es)$", "", token)

    return token


# Allowed canonical entities = all canonical values from mapping
ALLOWED_CANONICAL_ENTITIES = set(ENTITY_CANONICAL_MAP.values())


def map_to_canonical_entity(
    raw: str,
    canonical_map: dict = ENTITY_CANONICAL_MAP,
    allowed_entities: set = ALLOWED_CANONICAL_ENTITIES,
    fuzzy: bool = True
) -> Optional[str]:
    """
    Map a raw token (e.g: 'channels', 'msgs', 'tickets') to a canonical entity
    using:
      - normalization
      - direct lookup
      - fuzzy matching on synonyms
      - fallback fuzzy match on final canonical entities
    """
    token = normalize_entity(raw)

    # --- 1) Direct deterministic lookup ---
    if token in canonical_map:
        return canonical_map[token]

    # --- 2) Try normalized-key match (handles spacing/punctuation variants) ---
    for k, v in canonical_map.items():
        if token == normalize_entity(k):
            return v

    # --- 3) Fuzzy match on synonym keys ---
    if fuzzy:
        keys = list(canonical_map.keys())
        matches = get_close_matches(token, keys, n=3, cutoff=0.8)
        if matches:
            return canonical_map[matches[0]]

    # --- 4) Fallback fuzzy match on canonical entity names ---
    if fuzzy:
        matches = get_close_matches(token, list(allowed_entities), n=1, cutoff=0.8)
        if matches:
            return matches[0]

    return None
