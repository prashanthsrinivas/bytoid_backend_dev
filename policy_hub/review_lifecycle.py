"""Review-cycle and revision-history helpers for Policy Hub documents.

Pure functions (no S3 / DB I/O) so they can be unit-tested in isolation and
reused by both the policy_hub routes and the workflow publish hook.

Two concerns live here:

1. **Org-wide review cadence** — a single review frequency that every document
   in the org follows. ``REVIEW_FREQUENCIES`` maps the stable enum value the
   API accepts to an interval in months. ``compute_next_review_date`` turns a
   publish timestamp + frequency into the next due date.

2. **Review & Revision History** — when a document is approved/published the
   history section must gain a row. ``record_publication`` mutates a policy
   ``item`` dict in place: it sets the review-cycle metadata and appends a
   structured ``revision_history`` entry, and ``render_history_into_content``
   keeps the rendered HTML table (inside ``item['content']``) in sync so the
   frontend shows the row whether it reads the structured list or the HTML.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

# enum value -> interval in months. Order is the canonical display order.
REVIEW_FREQUENCIES: dict[str, int] = {
    "quarterly": 3,
    "semi_annual": 6,
    "annual": 12,
    "biennial": 24,
}
DEFAULT_REVIEW_FREQUENCY = "annual"

_FREQUENCY_LABELS = {
    "quarterly": "Quarterly",
    "semi_annual": "Semi-annual",
    "annual": "Annual",
    "biennial": "Biennial",
}


def normalize_frequency(frequency: str | None) -> str:
    """Return a valid frequency enum, falling back to the default."""
    if frequency and frequency in REVIEW_FREQUENCIES:
        return frequency
    return DEFAULT_REVIEW_FREQUENCY


def frequency_options() -> list[dict]:
    """Selectable cadence options for the settings UI (stable order)."""
    return [
        {
            "value": value,
            "label": _FREQUENCY_LABELS[value],
            "interval_months": months,
        }
        for value, months in REVIEW_FREQUENCIES.items()
    ]


def _add_months(base: date, months: int) -> date:
    """Add ``months`` to ``base`` clamping the day to the target month length."""
    total = base.month - 1 + months
    year = base.year + total // 12
    month = total % 12 + 1
    # Clamp day (e.g. Jan 31 + 1mo -> Feb 28/29).
    if month == 2:
        last_day = 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28
    elif month in (4, 6, 9, 11):
        last_day = 30
    else:
        last_day = 31
    return date(year, month, min(base.day, last_day))


def _coerce_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def compute_next_review_date(published_at, frequency: str | None) -> str:
    """Return the ISO date (YYYY-MM-DD) the document is next due for review."""
    base = _coerce_date(published_at)
    months = REVIEW_FREQUENCIES[normalize_frequency(frequency)]
    return _add_months(base, months).isoformat()


# ── Revision history ──────────────────────────────────────────────────────────

# The "Review and Revision History" section id differs per doc type but always
# ends with ``.revision_history`` (see policy_hub/templates.py).
_HISTORY_SUFFIX = ".revision_history"

_HISTORY_COLUMNS = ("Version", "Date", "Author", "Summary of Changes")


def history_section_id(doc_type: str) -> str:
    return f"{doc_type}{_HISTORY_SUFFIX}"


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_history_into_content(content: str, doc_type: str, entry: dict) -> str:
    """Append ``entry`` as a row in the history section's HTML table.

    Resilient to the section being empty or lacking a table: it will create the
    table (with a header row) on first use and strip any "No history recorded"
    placeholder. Returns the updated HTML; on any parse failure returns the
    original ``content`` unchanged so a render hiccup never blocks publishing.
    """
    if not content:
        return content
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(content, "lxml")
        target_id = history_section_id(doc_type)
        section = soup.find(attrs={"data-section-id": target_id})
        if section is None:
            # Fall back to any element whose id ends with .revision_history.
            for el in soup.find_all(attrs={"data-section-id": True}):
                if str(el.get("data-section-id", "")).endswith(_HISTORY_SUFFIX):
                    section = el
                    break
        if section is None:
            return content

        # Drop empty/placeholder prose ("No history recorded.", blank <p>) so
        # the rendered section shows only the table once it has rows.
        for p in section.find_all("p"):
            text = p.get_text(strip=True).lower()
            if not text or "no history" in text or "no revision" in text:
                p.decompose()

        table = section.find("table")
        if table is None:
            table = soup.new_tag("table")
            thead = soup.new_tag("thead")
            head_tr = soup.new_tag("tr")
            for col in _HISTORY_COLUMNS:
                th = soup.new_tag("th")
                th.string = col
                head_tr.append(th)
            thead.append(head_tr)
            table.append(thead)
            table.append(soup.new_tag("tbody"))
            section.append(table)

        tbody = table.find("tbody")
        if tbody is None:
            tbody = soup.new_tag("tbody")
            table.append(tbody)

        tr = soup.new_tag("tr")
        for value in (
            entry.get("version", ""),
            entry.get("date", ""),
            entry.get("author", ""),
            entry.get("summary", ""),
        ):
            td = soup.new_tag("td")
            td.string = str(value)
            tr.append(td)
        tbody.append(tr)

        root = soup.find("div", class_="policy-document")
        if root is not None:
            return str(root)
        if soup.body is not None:
            return soup.body.decode_contents()
        return str(soup)
    except Exception:
        return content


def record_publication(
    item: dict,
    doc_type: str,
    version: str,
    author: str,
    frequency: str | None,
    published_at=None,
    summary: str | None = None,
) -> dict:
    """Mutate ``item`` to reflect an approval/publish event.

    Sets review-cycle metadata (``review_frequency``, ``review_interval_months``,
    ``last_reviewed_at``, ``next_review_date``), appends a structured
    ``revision_history`` entry, and syncs the rendered HTML history table.
    Idempotency: callers should guard against double-publishing; this function
    appends unconditionally.
    """
    freq = normalize_frequency(frequency)
    published_at = published_at or datetime.now(timezone.utc)
    review_date = _coerce_date(published_at).isoformat()

    item["status"] = "published"
    item["review_frequency"] = freq
    item["review_interval_months"] = REVIEW_FREQUENCIES[freq]
    item["last_reviewed_at"] = review_date
    item["next_review_date"] = compute_next_review_date(published_at, freq)

    entry = {
        "version": str(version or item.get("metadata", {}).get("version", "1.0")),
        "date": review_date,
        "author": author or "",
        "summary": summary or "Approved and published via review workflow.",
        "action": "published",
    }
    history = list(item.get("revision_history") or [])
    history.append(entry)
    item["revision_history"] = history

    if item.get("content"):
        item["content"] = render_history_into_content(item["content"], doc_type, entry)
        # Keep the cached section's body_html in step with content if present.
        for sec in item.get("sections", []) or []:
            if str(sec.get("id", "")).endswith(_HISTORY_SUFFIX):
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(item["content"], "lxml")
                el = soup.find(attrs={"data-section-id": sec["id"]})
                if el is not None:
                    inner = "".join(str(c) for c in el.children
                                    if getattr(c, "name", None) != "h2")
                    sec["body_html"] = inner.strip()
                break

    return item
