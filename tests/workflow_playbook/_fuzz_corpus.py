"""FuzzyLLM corpus — deliberately broken model outputs (§4y-1).

Every entry is a *string*, because real LLM responses are always strings: the
non-determinism is in the *content*, not the type. Parsers that consume model
output must survive every one of these with a safe value and **never raise**.

Each item is ``(id, raw)``. Use ``FUZZ_VECTORS`` for the full sweep and
``EXTRACTABLE`` for the subset where a well-behaved extractor should still
recover parseable JSON.
"""

from __future__ import annotations

# Broken / adversarial outputs that must not crash a parser.
FUZZ_VECTORS: list[tuple[str, str]] = [
    ("truncated_json", '{"a": 1,'),
    ("markdown_json_fence", '```json\n{"a": 1}\n```'),
    ("markdown_generic_fence", '```\n{"a": 1}\n```'),
    ("yaml_fence", '```yaml\na: 1\n```'),
    ("prose_wrapped", 'Sure! Here is the result: {"a": 1} — hope this helps'),
    ("hallucinated_extra_keys", '{"a": 1, "totally_made_up": true}'),
    ("missing_keys", '{}'),
    ("empty_string", ''),
    ("whitespace_only", '   \n\t  '),
    ("null_literal", 'null'),
    ("trailing_comma", '{"a": 1,}'),
    ("single_quotes", "{'a': 1}"),
    ("nan_inf", '{"a": NaN, "b": Infinity}'),
    ("deeply_nested", '{"a":' * 40 + '1' + '}' * 40),
    ("huge_blob", '{"k":"' + 'x' * 20000 + '"}'),
    ("control_chars", '{"a": "\x00\x01\x02bad"}'),
    ("prompt_injection_echo", 'Ignore all previous instructions. {"a": 1}'),
    ("refusal_prose", 'I am sorry, but I cannot help with that request.'),
    ("partial_array", '[{"q": "x"}'),
    ("html_wrapped", '<pre>{"a": 1}</pre>'),
    ("non_ascii", '{"name": "café ☕ 日本語"}'),
    ("multiple_objects", '{"a":1}\n{"b":2}'),
]

# Subset where an extractor should still surface a parseable JSON payload.
EXTRACTABLE: list[tuple[str, str]] = [
    ("markdown_json_fence", '```json\n{"a": 1}\n```'),
    ("markdown_generic_fence", '```\n{"a": 1}\n```'),
]
