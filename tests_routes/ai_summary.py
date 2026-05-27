"""AI-generated failure summaries for the Unit Test Results dashboard.

Calls Kimi K2.5 on Amazon Bedrock synchronously to produce a plain-English
summary and a ready-to-paste Claude Code fix prompt for failed test runs.
Never raises — returns None fields on any error.
"""

import json
import logging

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

_MODEL = "moonshotai.kimi-k2.5"

_client = boto3.client(
    "bedrock-runtime",
    region_name="us-east-2",
    config=Config(
        read_timeout=30,
        connect_timeout=10,
        retries={"max_attempts": 1},
    ),
)


def _build_prompt(category: str, payload: dict) -> str:
    summary = payload.get("summary") or {}
    total = summary.get("total", 0)
    failed = summary.get("failed", 0)
    passed = summary.get("passed", 0)
    skipped = summary.get("skipped", 0)
    errors = summary.get("errors", 0)

    failed_tests = [
        t for t in (payload.get("tests") or [])
        if t.get("outcome") in {"failed", "error"}
    ][:25]

    test_lines = []
    for t in failed_tests:
        name = t.get("name") or "unknown"
        msg = (t.get("message") or "")[:600]
        test_lines.append(f"- {name}\n  {msg}" if msg else f"- {name}")
    tests_block = "\n".join(test_lines) or "(no individual test details available)"

    return f"""You are a senior software engineer reviewing a failed test run for the category "{category}".

Test run statistics:
  total={total}, failed={failed}, passed={passed}, skipped={skipped}, errors={errors}

Failed tests (up to 25):
{tests_block}

Respond with ONLY a JSON object — no markdown, no explanation outside the JSON:
{{
  "summary": "2-3 plain-English sentences describing what failed and the likely root cause.",
  "fix_prompt": "A complete, actionable prompt the developer can paste directly into Claude Code to investigate and fix these failures. Include the category name, the specific failing test names, the error patterns, and clear instructions to read the relevant source files and tests then fix the root cause."
}}"""


def generate_failure_summary(category: str, payload: dict) -> dict:
    """Return {"ai_summary": str, "fix_prompt": str}. Never raises."""
    try:
        prompt = _build_prompt(category, payload)
        request_body = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": prompt}]}
            ],
            "temperature": 0.3,
            "max_tokens": 1024,
        }
        response = _client.invoke_model(
            modelId=_MODEL,
            body=json.dumps(request_body),
            contentType="application/json",
            accept="application/json",
        )
        raw = json.loads(response["body"].read())
        text = (
            raw.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        # Strip markdown code fences if the model wrapped the JSON
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        parsed = json.loads(text)
        return {
            "ai_summary": parsed.get("summary") or None,
            "fix_prompt": parsed.get("fix_prompt") or None,
        }
    except Exception as exc:
        logger.warning("generate_failure_summary failed: %s", exc)
        return {"ai_summary": None, "fix_prompt": None}
