"""Unit tests for utils.fireworkzz.extract_json_safe.

Regression coverage for the tracker-AI "AI returned an unexpected format"
failure: the parser only matched JSON *objects* (``\\{.*\\}``), so any prompt
asking for a top-level JSON array (selected column/row tracker edits) failed
to parse even when the model complied exactly.

The fireworkzz module pulls in boto3/fireworks/langchain at import time, and
other unit tests stub the whole module in ``sys.modules`` — so the function
under test is compiled straight from the source file instead of importing
the module.
"""

import ast
import json
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "utils" / "fireworkzz.py"


def _load_extract_json_safe():
    tree = ast.parse(_SRC.read_text())
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "extract_json_safe"
    )
    ns = {"re": re, "json": json}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(_SRC), "exec"), ns)  # noqa: S102
    return ns["extract_json_safe"]


extract_json_safe = _load_extract_json_safe()


class TestObjects:
    def test_bare_object(self):
        assert extract_json_safe('{"a": 1}') == {"a": 1}

    def test_object_in_prose(self):
        assert extract_json_safe('Sure! Here it is: {"a": 1} Hope that helps.') == {
            "a": 1
        }

    def test_markdown_fenced_object(self):
        assert extract_json_safe('```json\n{"a": 1}\n```') == {"a": 1}

    def test_object_takes_precedence_when_first(self):
        assert extract_json_safe('{"a": 1} and also [2, 3]') == {"a": 1}


class TestArrays:
    def test_bare_array_of_objects(self):
        # The exact shape the selected-column prompt requests.
        text = '[{"row_id": "r1", "new_value": "Confirmed"}, {"row_id": "r2", "new_value": "Confirmed"}]'
        assert extract_json_safe(text) == [
            {"row_id": "r1", "new_value": "Confirmed"},
            {"row_id": "r2", "new_value": "Confirmed"},
        ]

    def test_markdown_fenced_array(self):
        text = '```json\n[{"row_id": "r1", "new_value": "x"}]\n```'
        assert extract_json_safe(text) == [{"row_id": "r1", "new_value": "x"}]

    def test_array_in_prose(self):
        text = 'Here are the updated rows:\n[{"row_id": "r1", "new_value": "x"}]\nDone.'
        assert extract_json_safe(text) == [{"row_id": "r1", "new_value": "x"}]

    def test_truncated_array_salvages_complete_objects(self):
        # Simulates a max_tokens cutoff mid-array: the complete leading
        # objects are kept, the partial trailing one is dropped.
        text = '[{"row_id": "r1", "new_value": "x"}, {"row_id": "r2", "new_value": "y"}, {"row_id": "r3", "new_va'
        assert extract_json_safe(text) == [
            {"row_id": "r1", "new_value": "x"},
            {"row_id": "r2", "new_value": "y"},
        ]


class TestRejects:
    def test_empty_and_none(self):
        assert extract_json_safe("") is None
        assert extract_json_safe(None) is None

    def test_plain_prose(self):
        assert extract_json_safe("I could not generate the changes.") is None

    def test_unsalvageable_garbage(self):
        assert extract_json_safe("[not json at all") is None
