"""
Structured document helpers for Policy Hub V2.

Responsibilities:
  parse_document_html  — parse a V2 HTML document into sections + statements
  render_document_html — render sections back to canonical HTML
  reconcile_statement_ids — preserve stable statement IDs across LLM edits
  sync_statements_to_lance — upsert statement embeddings into LanceDB
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from bs4 import BeautifulSoup, Tag

from policy_hub.templates import SectionDef, get_template
from utils.base_logger import get_logger

logger = get_logger(__name__)


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class Statement:
    id: str
    text: str
    seq: int
    section_id: str = ""
    status: str = "active"  # 'active' | 'superseded'


@dataclass
class Section:
    id: str
    title: str
    kind: str
    body_html: str = ""
    statements: list[Statement] = field(default_factory=list)


@dataclass
class ParsedDocument:
    sections: list[Section]
    metadata: dict  # document_id, version, effective_date, classification


# ── HTML parsing ─────────────────────────────────────────────────────────────


def parse_document_html(
    html: str, doc_type: str
) -> ParsedDocument:
    """Parse a V2 HTML document into structured sections and statements.

    Handles both well-formed V2 documents (with data-section-id / data-statement-id
    attributes) and legacy HTML (best-effort mapping by heading text).
    """
    template = get_template(doc_type)
    template_index: dict[str, SectionDef] = {s.id: s for s in template}

    soup = BeautifulSoup(html, "lxml")
    sections: list[Section] = []
    metadata: dict = {}

    # --- Collect sections by data-section-id (V2 path) ---
    tagged_sections = soup.find_all(attrs={"data-section-id": True})

    if tagged_sections:
        for tag in tagged_sections:
            sec_id = tag.get("data-section-id", "")
            sec_def = template_index.get(sec_id)
            if sec_def is None:
                continue

            sec = Section(
                id=sec_id,
                title=sec_def.title,
                kind=sec_def.kind,
                body_html=str(tag),
            )

            if sec_def.kind in ("statements", "steps"):
                sec.statements = _extract_statements(tag, sec_id)

            if sec_def.kind == "header_table":
                metadata.update(_extract_metadata_from_table(tag))

            sections.append(sec)
    else:
        # Legacy path — map by <h2> text
        sections = _parse_legacy_html(soup, template)

    # Ensure every required section is present (as empty if missing)
    present_ids = {s.id for s in sections}
    for sec_def in template:
        if sec_def.id not in present_ids:
            sections.append(
                Section(id=sec_def.id, title=sec_def.title, kind=sec_def.kind)
            )

    # Preserve template order
    order = {s.id: i for i, s in enumerate(template)}
    sections.sort(key=lambda s: order.get(s.id, 999))

    return ParsedDocument(sections=sections, metadata=metadata)


def _extract_statements(container: Tag, section_id: str) -> list[Statement]:
    statements: list[Statement] = []
    for seq, li in enumerate(container.find_all("li"), start=1):
        stmt_id = li.get("data-statement-id") or str(uuid.uuid4())
        text = li.get_text(separator=" ", strip=True)
        if text:
            statements.append(
                Statement(id=stmt_id, text=text, seq=seq, section_id=section_id)
            )
    return statements


def _extract_metadata_from_table(tag: Tag) -> dict:
    meta: dict = {}
    key_map = {
        "document id": "document_id",
        "version": "version",
        "effective date": "effective_date",
        "classification": "classification",
        "policy name": "title",
        "procedure name": "title",
        "standard name": "title",
    }
    for row in tag.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            k = cells[0].get_text(strip=True).lower()
            v = cells[1].get_text(strip=True)
            mapped = key_map.get(k)
            if mapped:
                meta[mapped] = v
    return meta


def _parse_legacy_html(soup: BeautifulSoup, template: list[SectionDef]) -> list[Section]:
    """Best-effort section mapping for pre-V2 HTML documents."""
    # Build a loose title → section_id map
    title_map: dict[str, SectionDef] = {}
    for sec in template:
        title_map[sec.title.lower()] = sec
        # Also allow partial matches (first word)
        first_word = sec.title.split()[0].lower()
        title_map.setdefault(first_word, sec)

    sections: list[Section] = []
    headings = soup.find_all(["h2"])

    for h2 in headings:
        heading_text = h2.get_text(strip=True)
        sec_def = _fuzzy_match_section(heading_text, title_map)
        if sec_def is None:
            continue

        # Collect content between this h2 and the next h2
        body_parts: list[str] = []
        for sibling in h2.next_siblings:
            if sibling.name == "h2":
                break
            body_parts.append(str(sibling))

        body_html = "".join(body_parts).strip()
        sec = Section(
            id=sec_def.id,
            title=sec_def.title,
            kind=sec_def.kind,
            body_html=body_html,
        )

        if sec_def.kind in ("statements", "steps"):
            # Parse any <li> items found; mint new ids since legacy has none
            tmp = BeautifulSoup(body_html, "lxml")
            for seq, li in enumerate(tmp.find_all("li"), start=1):
                text = li.get_text(separator=" ", strip=True)
                if text:
                    sec.statements.append(
                        Statement(
                            id=str(uuid.uuid4()),
                            text=text,
                            seq=seq,
                            section_id=sec_def.id,
                        )
                    )

        sections.append(sec)

    return sections


def _fuzzy_match_section(heading: str, title_map: dict[str, SectionDef]) -> Optional[SectionDef]:
    h = heading.lower().strip()
    # Exact match first
    if h in title_map:
        return title_map[h]
    # Substring match
    for key, sec_def in title_map.items():
        if key in h or h in key:
            return sec_def
    return None


# ── HTML rendering ────────────────────────────────────────────────────────────


def render_document_html(parsed: ParsedDocument, doc_type: str) -> str:
    """Render a ParsedDocument back to canonical V2 HTML.

    The output uses data-section-id on wrapper divs and data-statement-id on
    <li> elements so the parser can round-trip cleanly.
    """
    template = get_template(doc_type)
    section_map = {s.id: s for s in parsed.sections}

    parts: list[str] = ['<div class="policy-document">']

    for sec_def in template:
        sec = section_map.get(sec_def.id)
        if sec is None:
            sec = Section(id=sec_def.id, title=sec_def.title, kind=sec_def.kind)

        parts.append(
            f'<div data-section-id="{sec.id}">'
        )
        parts.append(f'<h2>{sec.title}</h2>')

        if sec.kind in ("statements", "steps") and sec.statements:
            ol_tag = "ol" if sec.kind == "steps" else "ul"
            parts.append(f"<{ol_tag}>")
            for stmt in sorted(sec.statements, key=lambda s: s.seq):
                parts.append(
                    f'<li data-statement-id="{stmt.id}">{_esc(stmt.text)}</li>'
                )
            parts.append(f"</{ol_tag}>")
        elif sec.body_html:
            parts.append(sec.body_html)
        else:
            parts.append("<p></p>")

        parts.append("</div>")

    parts.append("</div>")
    return "\n".join(parts)


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ── Statement ID reconciliation ───────────────────────────────────────────────


def reconcile_statement_ids(
    old_statements: list[Statement],
    new_html_section: str,
    section_id: str,
    similarity_threshold: float = 0.85,
) -> tuple[list[Statement], list[Statement]]:
    """Reconcile statement IDs after an LLM has rewritten a section.

    The LLM may:
      - Preserve the data-statement-id attribute (best case — kept as-is).
      - Drop the attribute but keep the text semantically (recovered via
        embedding similarity if score ≥ similarity_threshold).
      - Write genuinely new content (new UUID minted).
      - Delete a statement (old ID marked superseded).
      - Split one into two (first keeps old ID, second gets new UUID).
      - Merge two into one (one ID kept, the other superseded).

    Returns:
      (active_statements, superseded_statements)
    """
    soup = BeautifulSoup(new_html_section, "lxml")
    li_tags = soup.find_all("li")

    old_by_id: dict[str, Statement] = {s.id: s for s in old_statements}
    matched_old_ids: set[str] = set()

    active: list[Statement] = []

    for seq, li in enumerate(li_tags, start=1):
        text = li.get_text(separator=" ", strip=True)
        if not text:
            continue

        explicit_id = li.get("data-statement-id")

        if explicit_id and explicit_id in old_by_id:
            # LLM preserved the attribute — keep the id
            matched_old_ids.add(explicit_id)
            active.append(
                Statement(
                    id=explicit_id,
                    text=text,
                    seq=seq,
                    section_id=section_id,
                )
            )
        else:
            # Try similarity recovery for dropped attributes
            recovered_id = _recover_id_by_similarity(
                text,
                old_statements,
                matched_old_ids,
                threshold=similarity_threshold,
            )
            if recovered_id:
                matched_old_ids.add(recovered_id)
                stmt_id = recovered_id
                logger.debug(
                    "Recovered statement id=%s via similarity (threshold=%.2f)",
                    stmt_id,
                    similarity_threshold,
                )
            else:
                stmt_id = str(uuid.uuid4())

            active.append(
                Statement(id=stmt_id, text=text, seq=seq, section_id=section_id)
            )

    # Statements not present in the new HTML are superseded
    superseded = [
        Statement(
            id=s.id,
            text=s.text,
            seq=s.seq,
            section_id=s.section_id,
            status="superseded",
        )
        for s in old_statements
        if s.id not in matched_old_ids
    ]

    return active, superseded


def _recover_id_by_similarity(
    new_text: str,
    old_statements: list[Statement],
    already_matched: set[str],
    threshold: float,
) -> Optional[str]:
    """Find the best-matching old statement for *new_text* by cosine similarity.

    Uses pre-computed in-memory embeddings when available (populated by
    sync_statements_to_lance). Falls back to simple word-overlap Jaccard
    similarity so the function works without a live Fireworks connection
    (useful in tests and when the embedding service is unavailable).
    """
    candidates = [s for s in old_statements if s.id not in already_matched]
    if not candidates:
        return None

    # Jaccard similarity (word-overlap) as a cheap approximation
    new_words = set(new_text.lower().split())

    best_id: Optional[str] = None
    best_score: float = 0.0

    for stmt in candidates:
        old_words = set(stmt.text.lower().split())
        if not old_words or not new_words:
            continue
        intersection = len(new_words & old_words)
        union = len(new_words | old_words)
        score = intersection / union if union else 0.0
        if score > best_score:
            best_score = score
            best_id = stmt.id

    return best_id if best_score >= threshold else None


# ── LanceDB sync ──────────────────────────────────────────────────────────────


async def sync_statements_to_lance(
    policy_id: str,
    doc_type: str,
    version: str,
    statements: list[Statement],
    superseded: Optional[list[Statement]] = None,
    user_id: str = "system",
) -> None:
    """Upsert active statements into the index_policy_statements LanceDB table
    and mark superseded statements accordingly.

    Uses the Fireworks embedding API (get_firework_embedding) — same approach
    as _async_index_framework in policy_hub/routes.py.
    """
    from db.lance_db_service import LanceDBServer
    from utils.fireworkzz import get_firework_embedding

    if not statements and not superseded:
        return

    lance = LanceDBServer()

    try:
        # Delete existing rows for this (policy_id, version) before inserting
        await lance.delete_policy_statements(policy_id=policy_id, version=version)

        if statements:
            texts = [s.text for s in statements]
            embeddings_model = await get_firework_embedding()
            vecs = await asyncio.to_thread(embeddings_model.embed_documents, texts)

            rows = [
                {
                    "statement_id": stmt.id,
                    "policy_id": policy_id,
                    "doc_type": doc_type,
                    "section_id": stmt.section_id,
                    "seq": stmt.seq,
                    "text": stmt.text,
                    "version": version,
                    "status": "active",
                    "embedding": np.array(vec, dtype=np.float32),
                }
                for stmt, vec in zip(statements, vecs)
            ]

            await lance.upsert_policy_statements(rows, user_id=user_id)
            logger.info(
                "Synced %d statements for policy=%s version=%s",
                len(rows),
                policy_id,
                version,
            )

        if superseded:
            await lance.mark_policy_statements_superseded(
                policy_id=policy_id,
                statement_ids=[s.id for s in superseded],
            )
            logger.info(
                "Marked %d statements superseded for policy=%s",
                len(superseded),
                policy_id,
            )

    except Exception as exc:
        logger.error(
            "sync_statements_to_lance failed for policy=%s: %s", policy_id, exc
        )
        raise
