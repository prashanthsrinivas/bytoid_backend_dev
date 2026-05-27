"""Unit tests for policy_hub.doc_ref.derive_prefix (pure, no DB)."""

import sys
from unittest.mock import MagicMock

import pytest

# doc_ref imports db.rds_db at module load (AWS Secrets Manager). Stub the DB
# layer before import so these pure-logic tests run without credentials.
for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

from policy_hub.doc_ref import (  # noqa: E402
    _PREFIX_OVERRIDES,
    derive_prefix,
)


@pytest.mark.unit
class TestOverrideMap:
    @pytest.mark.parametrize("title,expected_prefix", [
        ("Access Control Policy", "ACC"),
        ("Acceptable Use Policy", "AUP"),
        ("Asset Management Standard", "ASM"),
        ("Business Continuity Plan", "BCM"),
        ("Change Management Procedure", "CHG"),
        ("Data Classification Policy", "DCL"),
        ("Data Protection Policy", "DPR"),
        ("Data Privacy Standard", "DPR"),
        ("Encryption Policy", "ENC"),
        ("Cryptography Standard", "ENC"),
        ("Incident Management Policy", "IRM"),
        ("Incident Response Procedure", "IRM"),
        ("Information Security Policy", "ISP"),
        ("Physical Security Policy", "PHS"),
        ("Risk Management Framework", "RSK"),
        ("Third Party Risk Policy", "TPM"),
        ("Vendor Management Standard", "TPM"),
        ("Vulnerability Management Procedure", "VLN"),
    ])
    def test_override_hits(self, title, expected_prefix):
        prefix, _seed = derive_prefix(title)
        assert prefix == expected_prefix

    def test_every_override_row_is_reachable(self):
        # Each override substring, used verbatim as a title, must resolve to
        # its prefix — guards against an entry being shadowed by an earlier one.
        for needle, prefix in _PREFIX_OVERRIDES.items():
            got, _ = derive_prefix(needle)
            # A later, more-general needle can legitimately shadow only if it
            # maps to the same prefix; assert prefix identity holds.
            assert got == _PREFIX_OVERRIDES[needle] == prefix or got == prefix

    def test_match_is_case_insensitive(self):
        assert derive_prefix("ACCESS CONTROL POLICY")[0] == "ACC"
        assert derive_prefix("access control policy")[0] == "ACC"

    def test_override_seed_is_the_substring(self):
        _prefix, seed = derive_prefix("Corporate Access Control Policy")
        assert seed == "access control"


@pytest.mark.unit
class TestStopwordFallback:
    def test_strips_doc_type_and_stopwords(self):
        # "policy" is a stopword; first significant word is "accounting"
        prefix, seed = derive_prefix("The Accounting Policy")
        assert prefix == "ACC"
        assert seed == "accounting"

    def test_first_significant_word_drives_prefix(self):
        prefix, _ = derive_prefix("Backup and Recovery Standard")
        assert prefix == "BAC"

    def test_short_first_word_pads_from_next_word(self):
        # "IT" → only 2 chars, borrows from "governance"
        prefix, _ = derive_prefix("IT Governance Policy")
        assert len(prefix) == 3
        assert prefix.startswith("IT")

    def test_single_short_word_padded_with_x(self):
        prefix, _ = derive_prefix("Hr")
        assert prefix == "HRX"
        assert len(prefix) == 3


@pytest.mark.unit
class TestEdgeCases:
    def test_empty_title(self):
        assert derive_prefix("") == ("DOC", "")

    def test_whitespace_only(self):
        assert derive_prefix("   \t  ") == ("DOC", "")

    def test_none_title(self):
        assert derive_prefix(None) == ("DOC", "")

    def test_only_stopwords(self):
        # No significant words -> cleaned-title fallback over the raw alpha
        prefix, _ = derive_prefix("The Policy")
        assert len(prefix) == 3

    def test_numbers_and_symbols_only(self):
        prefix, seed = derive_prefix("123 !!! ###")
        assert prefix == "DOC"
        assert seed == ""

    def test_unicode_title_does_not_crash(self):
        prefix, _ = derive_prefix("Política de Acceso")
        assert isinstance(prefix, str)
        assert len(prefix) >= 3

    def test_prefix_always_three_chars_minimum(self):
        for title in ["A", "AB", "X Y Z", "an of the", "Q"]:
            prefix, _ = derive_prefix(title)
            assert len(prefix) >= 3, f"{title!r} -> {prefix!r}"
