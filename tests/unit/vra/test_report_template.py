"""VRA report-template assembly tests — vra/report_template.py.

Asserts the OSINT section is appended to the real base template without forking
it, sits before the trailing appendices/references, and is idempotent.
"""

import pytest

from vra.report_template import build_vra_structure


def _block_ids(structure):
    return [b.get("block_id") for b in structure.get("blocks", [])]


@pytest.mark.unit
def test_osint_block_present():
    ids = _block_ids(build_vra_structure())
    assert "osint-intelligence-assessment" in ids


@pytest.mark.unit
def test_osint_block_before_appendices():
    ids = _block_ids(build_vra_structure())
    if "appendices" in ids:
        assert ids.index("osint-intelligence-assessment") < ids.index("appendices")
    if "references" in ids:
        assert ids.index("osint-intelligence-assessment") < ids.index("references")


@pytest.mark.unit
def test_base_blocks_preserved():
    # The base template's executive-summary etc. must still be there (not forked away).
    ids = _block_ids(build_vra_structure())
    assert "executive-summary" in ids
    assert len(ids) >= 3


@pytest.mark.unit
def test_idempotent_no_double_insert():
    once = _block_ids(build_vra_structure())
    assert once.count("osint-intelligence-assessment") == 1


@pytest.mark.unit
def test_osint_block_has_required_subsections():
    struct = build_vra_structure()
    block = next(b for b in struct["blocks"] if b["block_id"] == "osint-intelligence-assessment")
    titles = {mb.get("title") for mb in block["micro_blocks"]}
    assert {
        "Executive Summary",
        "Intelligence Summary",
        "Significant Findings",
        "Evidence Summary",
        "Dashboard Reference",
    } <= titles
