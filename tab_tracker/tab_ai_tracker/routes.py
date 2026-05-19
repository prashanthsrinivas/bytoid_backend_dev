import json
import re
import asyncio
import uuid
import traceback
from flask import Blueprint, request, jsonify
from utils.permission_required import permission_required_body
from credits_route.route import Credits
from db.rds_db import connect_to_rds
from db.lance_db_service import LanceDBServer, QueryData
from playbook.background_worker import JobManager
from tab_tracker.helper import save_tracker_file
from utils.fireworkzz import (
    get_fireworks_response2,
    get_think_fire_response2_og,
    get_firework_embedding,
    extract_json_safe,
)
from utils.base_logger import get_logger
from websockets_custom.ws_instance import ws_service, msg_builder_main

tracker_ai_bp = Blueprint("tracker_ai", __name__)
logger = get_logger(__name__)
ws_sender = ws_service
msg_builder = msg_builder_main

INTENT_MESSAGES = {
    "add_column": "Adding new columns",
    "delete_column": "Removing columns",
    "change_column_name": "Renaming columns",
    "reduce": "Reducing content length",
    "increase": "Expanding content",
    "modify_content": "Modifying content",
    "explain": "Generating explanation",
    "normal_greeting": "Handling greeting",
}

_ERROR_LABELS = {
    "invalid_response": "AI returned an unexpected format — try rephrasing",
    "insufficient_credits": "Insufficient credits",
    "schema_validation_failed": "AI changes could not be applied safely",
    "invalid_scope": "Operation not supported for the selected scope",
    "missing_column_name": "Column name is required",
}


async def send(ws_sndr, msg, user_id):
    if not msg:
        return
    await ws_sndr.emit(
        user_id=user_id,
        message=msg.get("message"),
        scope=msg.get("scope", "global"),
        session_id=msg.get("session_id"),
        job_id=msg.get("job_id"),
        msg_type=msg.get("type"),
        stage=msg.get("stage"),
        progress=msg.get("progress"),
        feature="tracker_ai",
    )


def _build_schema_summary(tracker_data: dict) -> str:
    """Build short column/metric summary for AI prompts."""
    tracker_type = tracker_data.get("type", "")
    schema = tracker_data.get("schema", {})
    if tracker_type == "table":
        cols = schema.get("columns", [])
        return ", ".join(c.get("name", c.get("id")) for c in cols) or "(no columns yet)"
    elif tracker_type == "matrix":
        rows = schema.get("rows", [])
        cols = schema.get("columns", [])
        return f"rows: {rows}; columns: {cols}" if rows or cols else "(empty matrix)"
    elif tracker_type == "scorecard":
        metrics = [m.get("name") for m in schema.get("metrics", [])]
        return f"metrics: {', '.join(metrics)}" if metrics else "(no metrics yet)"
    return ""


async def get_intents(user_id: str, message: str, credits, tracker_data: dict) -> list:
    """
    Classify user message into one or more intents (in execution order).
    Returns a list of intent objects with parameters.
    """
    logger.info(f"get_intents: user={user_id}, message_len={len(message)}")
    tracker_type = tracker_data.get("type", "")
    schema_summary = _build_schema_summary(tracker_data)

    prompt = f"""You are an intent classifier for a data tracker assistant.

User message: "{message}"
Tracker type: {tracker_type}
Available columns: {schema_summary}

Identify ALL intents present in the message. Return them IN ORDER they should be applied.
Schema/structural changes must come BEFORE content changes.

AVAILABLE INTENTS:
- "add_column": add new column. Include: column_name (str), column_type ("text"|"number"|"boolean", default "text"), default_value (str, default "")
- "delete_column": remove existing column. Include: column_name (match to available columns)
- "change_column_name": rename a column. Include: old_column_name, new_column_name
- "reduce": shorten/condense text content values. No extra params.
- "increase": expand/elaborate text content, or add new rows. No extra params.
- "modify_content": change specific values per instruction. No extra params.
- "explain": explain/analyze data or answer a question. No extra params.
- "normal_greeting": greeting, off-topic, inappropriate, or unclear.

Return ONLY valid JSON:
{{"intents": [{{"type": "<intent>", <extra params>}}, ...]}}

RULES:
- One or more intents in execution order.
- Schema changes (add_column, delete_column, change_column_name) must come FIRST.
- If completely unclear or inappropriate: [{{"type": "normal_greeting"}}].
"""

    try:
        response = await get_fireworks_response2(
            user_id, prompt, role="system", credits=credits, temp=0.0
        )
        if not response or response == "INSUFFICIENT":
            return [{"type": "normal_greeting"}]
        parsed = extract_json_safe(response)
        if parsed and isinstance(parsed, dict):
            intents = parsed.get("intents", [])
            if isinstance(intents, list) and len(intents) > 0:
                valid_types = set(INTENT_MESSAGES.keys())
                filtered = [i for i in intents if i.get("type") in valid_types]
                if filtered:
                    return filtered
    except Exception as e:
        logger.exception("Error in get_intents: %s", e)
    return [{"type": "normal_greeting"}]


async def detect_language(user_id: str, message: str, credits) -> str:
    """Detect the natural language of the user's message."""
    logger.info(f"detect_language: user={user_id}")
    prompt = f"""Detect the language of this message: "{message}"

Return ONLY valid JSON:
{{"language": "<full language name in English, e.g. English, French, Spanish>"}}

If the language cannot be determined, return "English"."""

    try:
        response = await get_fireworks_response2(
            user_id, prompt, role="system", credits=credits, temp=0.0
        )
        if not response or response == "INSUFFICIENT":
            return "English"
        parsed = extract_json_safe(response)
        if parsed and isinstance(parsed, dict):
            lang = parsed.get("language", "English")
            return lang if lang else "English"
    except Exception as e:
        logger.exception("Error in detect_language: %s", e)
    return "English"


def _validate_tracker_schema(original: dict, modified: dict) -> tuple:
    """Validate that modified tracker preserves structure of original. Returns (is_valid, error_msg)."""
    try:
        if original.get("tracker_id") != modified.get("tracker_id"):
            return (False, "tracker_id was modified")
        if original.get("type") != modified.get("type"):
            return (False, "tracker type was modified")

        orig_schema = original.get("schema", {})
        mod_schema = modified.get("schema", {})

        tracker_type = original.get("type", "")

        if tracker_type == "table":
            orig_cols = {c["id"] for c in orig_schema.get("columns", [])}
            mod_cols = {c["id"] for c in mod_schema.get("columns", [])}
            if orig_cols != mod_cols:
                return (False, "column schema was modified")
            orig_row_ids = {r["row_id"] for r in original.get("rows", [])}
            mod_row_ids = {r["row_id"] for r in modified.get("rows", [])}
            if not orig_row_ids.issubset(mod_row_ids):
                return (False, "existing rows were removed")

        elif tracker_type == "matrix":
            orig_rows = set(orig_schema.get("rows", []))
            mod_rows = set(mod_schema.get("rows", []))
            if not orig_rows.issubset(mod_rows):
                return (False, "matrix rows were removed")
            orig_cols = set(orig_schema.get("columns", []))
            mod_cols = set(mod_schema.get("columns", []))
            if not orig_cols.issubset(mod_cols):
                return (False, "matrix columns were removed")

        elif tracker_type == "scorecard":
            orig_metrics = {m["id"] for m in orig_schema.get("metrics", [])}
            mod_metrics = {m["id"] for m in mod_schema.get("metrics", [])}
            if not orig_metrics.issubset(mod_metrics):
                return (False, "scorecard metrics were removed")

        return (True, "")
    except Exception as e:
        logger.exception("Error in _validate_tracker_schema: %s", e)
        return (False, f"validation error: {str(e)}")


def _build_scope_context_str(scope: dict, tracker_data: dict) -> str:
    """Build human-readable scope description for AI prompts."""
    scope_type = scope.get("type", "complete")
    if scope_type == "complete":
        return "Scope: Full tracker"
    elif scope_type == "selected_element":
        elem = scope.get("selected_element", {})
        return f"Selected cell: Row {elem.get('row_id')}, Column {elem.get('col_id')}, Current value: {elem.get('value')}"
    elif scope_type == "selected_row":
        row = scope.get("selected_row", {})
        return f"Selected row ID: {row.get('row_id')}\nRow data: {json.dumps(row.get('data', {}))}"
    elif scope_type == "selected_column":
        col = scope.get("selected_column", {})
        return f"Selected column: {col.get('name')} (ID: {col.get('col_id')})"
    elif scope_type == "selected_rows":
        rows = scope.get("selected_rows", [])
        return f"Selected {len(rows)} rows"
    elif scope_type == "selected_columns":
        cols = scope.get("selected_columns", [])
        return f"Selected {len(cols)} columns"
    return "Scope: Full tracker"


def _build_truncated_tracker_str(tracker_data: dict, max_items: int = 20) -> str:
    """Build truncated but structurally complete tracker JSON string."""
    try:
        tracker_type = tracker_data.get("type", "")
        truncated = {
            "tracker_id": tracker_data.get("tracker_id"),
            "type": tracker_type,
            "schema": tracker_data.get("schema", {}),
        }

        if tracker_type == "table":
            rows = tracker_data.get("rows", [])
            if len(rows) > max_items:
                truncated["rows"] = rows[:max_items]
                note = f"\n... ({len(rows) - max_items} more rows truncated)"
            else:
                truncated["rows"] = rows
                note = ""
        elif tracker_type == "matrix":
            cells = tracker_data.get("cells", [])
            if len(cells) > max_items:
                truncated["cells"] = cells[:max_items]
                note = f"\n... ({len(cells) - max_items} more cells truncated)"
            else:
                truncated["cells"] = cells
                note = ""
        elif tracker_type == "scorecard":
            records = tracker_data.get("records", [])
            if len(records) > max_items:
                truncated["records"] = records[:max_items]
                note = f"\n... ({len(records) - max_items} more records truncated)"
            else:
                truncated["records"] = records
                note = ""
        else:
            note = ""

        result = json.dumps(truncated, indent=2)
        if len(result) > 12000:
            result = result[:12000] + "\n... (truncated)"
        return result + note
    except Exception as e:
        logger.exception("Error truncating tracker: %s", e)
        return json.dumps({"error": "truncation_failed"})


def _merge_scoped_changes(tracker_data: dict, scope: dict, ai_result: dict) -> dict:
    """Merge AI-generated scoped changes back into full tracker_data."""
    scope_type = scope.get("type", "complete")

    if scope_type == "complete":
        return ai_result

    rows_by_id = {row["row_id"]: row for row in tracker_data.get("rows", [])}

    if scope_type == "selected_element":
        row_id = ai_result.get("row_id")
        col_id = ai_result.get("col_id")
        new_value = ai_result.get("new_value")
        if row_id in rows_by_id:
            rows_by_id[row_id]["values"][col_id] = new_value

    elif scope_type == "selected_row":
        row_id = ai_result.get("row_id")
        new_values = ai_result.get("values", {})
        if row_id in rows_by_id:
            rows_by_id[row_id]["values"].update(new_values)

    elif scope_type == "selected_column":
        col_id = scope.get("selected_column", {}).get("col_id")
        for item in ai_result:
            row_id = item.get("row_id")
            new_value = item.get("new_value")
            if row_id in rows_by_id:
                rows_by_id[row_id]["values"][col_id] = new_value

    elif scope_type == "selected_rows":
        for item in ai_result:
            row_id = item.get("row_id")
            if row_id in rows_by_id:
                rows_by_id[row_id]["values"].update(item.get("values", {}))

    elif scope_type == "selected_columns":
        selected_cols = scope.get("selected_columns", [])
        col_ids = {col.get("col_id") for col in selected_cols}
        for item in ai_result:
            row_id = item.get("row_id")
            if row_id in rows_by_id:
                for col_id, value in item.get("values", {}).items():
                    if col_id in col_ids:
                        rows_by_id[row_id]["values"][col_id] = value

    tracker_data["rows"] = list(rows_by_id.values())
    return tracker_data


async def _handle_greeting(
    user_id, message, language, credits, job_id, session_id, emit
) -> dict:
    """Handle normal_greeting intent."""
    logger.info(
        f"_handle_greeting: user={user_id}, job_id={job_id}, language={language}"
    )
    await emit(
        msg_builder.job_progress(
            job_id, session_id, "processing", "Processing your message...", 50
        )
    )

    prompt = f"""You are a helpful assistant for a data tracker tool.

The user is working with a data tracker. They sent this message:
"{message}"

This message is either a greeting, off-topic, or not directly actionable for the tracker.

Respond warmly and briefly in {language}. Let the user know you are here to help with tracker operations like:
- Summarizing or condensing tracker content
- Expanding or adding new data rows
- Modifying specific values
- Explaining the data in the tracker

Do not include markdown, emojis, or HTML. Plain text only, 1-3 sentences maximum."""

    response = await get_fireworks_response2(
        user_id, prompt, role="system", credits=credits, temp=0.7
    )
    if not response or response == "INSUFFICIENT":
        response = "I'm here to help you manage your tracker data. You can ask me to summarize, expand, modify, or explain your tracker."

    return {
        "intent": "normal_greeting",
        "tracker_modified": False,
        "response": response,
    }


_KB_TRIGGER_PHRASES = frozenset(
    {
        "knowledge base",
        "knowledgebase",
        " kb ",
        " kb.",
        " kb,",
        " kb?",
        "uploaded document",
        "uploaded doc",
        "my document",
        "my doc",
        "in documents",
        "from documents",
        "search document",
    }
)


def _wants_kb_search(message: str) -> bool:
    """Return True if the user explicitly asks to search knowledge base / uploaded docs."""
    lower = f" {message.lower()} "
    return any(phrase in lower for phrase in _KB_TRIGGER_PHRASES)


async def _handle_explain(
    user_id, message, language, scope, credits, job_id, session_id, emit
) -> dict:
    """Handle explain intent.
    Default: answer from tracker data only.
    KB path: triggered when user mentions 'knowledge base', 'kb', 'my documents', etc.
    """
    logger.info(
        f"_handle_explain: user={user_id}, job_id={job_id}, scope_type={scope.get('type')}"
    )

    tracker_data = scope.get("tracker_data", {})
    tracker_json_str = _build_truncated_tracker_str(tracker_data)
    scope_context = _build_scope_context_str(scope, tracker_data)

    use_kb = _wants_kb_search(message)
    logger.info(f"_handle_explain: use_kb={use_kb}")

    context_section = ""
    if use_kb:
        await emit(
            msg_builder.job_progress(
                job_id, session_id, "vector_search", "Searching knowledge base...", 40
            )
        )
        try:
            dbserver = LanceDBServer()
            embedding_model = await get_firework_embedding()
            embedding = await asyncio.to_thread(embedding_model.embed_query, message)
            query = QueryData(user_id=user_id, embedding=embedding, top_k=3)
            lance_results = await dbserver.query_vector(query)
            context_texts = [r.get("text", "") for r in lance_results if r.get("text")]
            if context_texts:
                context_section = (
                    f"\nKnowledge base context:\n{''.join(context_texts)}\n"
                )
                logger.info(
                    f"_handle_explain: KB returned {len(context_texts)} results"
                )
            else:
                logger.info("_handle_explain: KB search returned no results")
        except Exception as e:
            logger.exception("LanceDB query error: %s", e)

    await emit(
        msg_builder.job_progress(
            job_id, session_id, "processing", "Generating explanation...", 50
        )
    )

    prompt = f"""You are a data analyst assistant for a tracker tool.

User question: "{message}"
Language for response: {language}

{scope_context}

Tracker data (current state):
{tracker_json_str}
{context_section}
INSTRUCTIONS:
- Answer the user's question clearly and concisely in {language}.
- Base your answer on the tracker data above.{" Use the knowledge base context only if directly relevant." if use_kb else ""}
- Do NOT modify or suggest changes to the tracker.
- Do NOT include markdown fences.
- Maximum 300 words.
- Plain text only."""

    response = await get_fireworks_response2(
        user_id, prompt, role="system", credits=credits, temp=0.7
    )
    if not response or response == "INSUFFICIENT":
        response = "I could not generate an explanation at this time."

    await emit(
        msg_builder.job_progress(
            job_id, session_id, "ai_complete", "Explanation ready.", 80
        )
    )

    return {
        "intent": "explain",
        "tracker_modified": False,
        "response": response,
    }


async def _handle_reduce(
    user_id, tracker_id, message, language, scope, credits, job_id, session_id, emit
) -> dict:
    """Handle reduce intent."""
    logger.info(
        f"_handle_reduce: user={user_id}, tracker_id={tracker_id}, job_id={job_id}, scope_type={scope.get('type')}"
    )
    await emit(
        msg_builder.job_progress(
            job_id, session_id, "processing", "Applying AI changes...", 50
        )
    )

    tracker_data = scope.get("tracker_data", {})
    scope_type = scope.get("type", "complete")

    if scope_type == "complete":
        tracker_json_str = _build_truncated_tracker_str(tracker_data)
        prompt = f"""You are a data content editor for a tracker tool.

User request: "{message}"
Language: {language}

Current tracker JSON:
{tracker_json_str}

TASK: Apply the user's request precisely.
- If the user specifies a particular row ID, item name, column, or any specific element, ONLY shorten/condense that element.
- If no specific target is mentioned, shorten ALL text content values.
- Leave every element not explicitly targeted by the user's request completely unchanged.

CRITICAL FORMAT RULES:
1. You MUST return ONLY valid JSON.
2. The output JSON must have EXACTLY the same structure as the input tracker JSON.
3. Do NOT add, remove, or rename any keys, column IDs, row IDs, or schema fields.
4. Do NOT change tracker_id, type, runbook_id, source_blocks, schema.columns[*].id, schema.columns[*].name, or any row_id values.
5. Only change the "values" dict content within each row (for table), cell "value" fields (for matrix), or record "value" fields (for scorecard).
6. Preserve numeric values unchanged.
7. Return the complete modified tracker JSON, not a diff."""

        response = await get_fireworks_response2(
            user_id, prompt, role="system", credits=credits, temp=0.7
        )
        if not response or response == "INSUFFICIENT":
            return {"error": "insufficient_credits"}
        parsed = extract_json_safe(response)
        if not parsed or not isinstance(parsed, dict):
            return {"error": "invalid_response"}
        ai_result = parsed
    else:
        elem = scope.get("selected_element", {})
        sel_row = scope.get("selected_row", {})
        sel_col = scope.get("selected_column", {})
        sel_rows = scope.get("selected_rows", [])
        sel_cols = scope.get("selected_columns", [])

        if scope_type == "selected_element" and elem:
            row_id = elem.get("row_id")
            col_id = elem.get("col_id")
            value = elem.get("value", "")
            prompt = f"""You are a data content editor for a tracker tool.

User request: "{message}"
Language: {language}

Selected cell:
Row ID: {row_id}, Column ID: {col_id}
Current value: "{value}"

TASK: Shorten/condense this specific cell value per the user's request.

Return ONLY valid JSON:
{{"row_id": "{row_id}", "col_id": "{col_id}", "new_value": "<shortened value>"}}"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if not parsed or not isinstance(parsed, dict):
                return {"error": "invalid_response"}
            ai_result = parsed

        elif scope_type == "selected_row" and sel_row:
            row_id = sel_row.get("row_id")
            col_schema = tracker_data.get("schema", {}).get("columns", [])
            col_map = ", ".join(
                f'{c["id"]} = "{c.get("name", c["id"])}"' for c in col_schema
            )
            row_values_json = json.dumps(sel_row.get("values", {}))
            prompt = f"""You are a data content editor for a tracker tool.

User request: "{message}"
Language: {language}

Column ID mapping: {col_map}
Row ID: {row_id}
Current values: {row_values_json}

TASK: Shorten/condense the text values in this row per the user's request.
Preserve numeric values unchanged.

Return ONLY valid JSON:
{{"row_id": "{row_id}", "values": {{"col_id": "shortened value", ...}}}}"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if not parsed or not isinstance(parsed, dict):
                return {"error": "invalid_response"}
            ai_result = parsed

        elif scope_type == "selected_column" and sel_col:
            col_id = sel_col.get("col_id")
            col_name = sel_col.get("name", col_id)
            rows_with_col = json.dumps(
                [
                    {
                        "row_id": r["row_id"],
                        "value": r.get("values", {}).get(col_id, ""),
                    }
                    for r in tracker_data.get("rows", [])
                ]
            )
            prompt = f"""You are a data content editor for a tracker tool.

User request: "{message}"
Language: {language}

Column to reduce: {col_name} (ID: {col_id})
Current values across all rows:
{rows_with_col}

TASK: Shorten/condense the value in column "{col_name}" for every row listed.

Return ONLY valid JSON array:
[{{"row_id": "...", "new_value": "<shortened value>"}}, ...]"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if isinstance(parsed, dict):
                parsed = parsed.get("rows") or parsed.get("data")
            if not parsed or not isinstance(parsed, list):
                return {"error": "invalid_response"}
            ai_result = parsed

        elif scope_type == "selected_rows" and sel_rows:
            col_schema = tracker_data.get("schema", {}).get("columns", [])
            col_map = ", ".join(
                f'{c["id"]} = "{c.get("name", c["id"])}"' for c in col_schema
            )
            rows_json = json.dumps(sel_rows, indent=2)
            prompt = f"""You are a data content editor for a tracker tool.

User request: "{message}"
Language: {language}

Column ID mapping: {col_map}

Selected rows to condense:
{rows_json}

TASK: Apply the user's request to the text values in the selected rows.
Preserve numeric values and row_id unchanged.

Return ONLY a valid JSON array — no wrapper object, no markdown:
[{{"row_id": "...", "values": {{"col_id": "shortened value", ...}}}}, ...]"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if isinstance(parsed, dict):
                parsed = (
                    parsed.get("rows") or parsed.get("data") or parsed.get("result")
                )
            if not parsed or not isinstance(parsed, list):
                logger.warning(
                    "invalid_response in selected_rows reduce, trying AI fallback"
                )
                fallback = await _ai_fallback(
                    user_id,
                    message,
                    tracker_data,
                    language,
                    credits,
                    hint="reduce selected rows",
                )
                if fallback:
                    is_valid, _ = _validate_tracker_schema(
                        scope.get("tracker_data", {}), fallback
                    )
                    if is_valid:
                        return {
                            "intent": "reduce",
                            "tracker_modified": True,
                            "pending_confirmation": True,
                            "tracker_data": fallback,
                            "tracker_id": tracker_id,
                        }
                return {"error": "invalid_response"}
            ai_result = parsed

        elif scope_type == "selected_columns" and sel_cols:
            col_ids_to_reduce = [c.get("col_id") for c in sel_cols if c.get("col_id")]
            col_names = {c.get("col_id"): c.get("name") for c in sel_cols}
            rows_view = json.dumps(
                [
                    {
                        "row_id": r["row_id"],
                        "values": {
                            cid: r.get("values", {}).get(cid, "")
                            for cid in col_ids_to_reduce
                        },
                    }
                    for r in tracker_data.get("rows", [])
                ]
            )
            prompt = f"""You are a data content editor for a tracker tool.

User request: "{message}"
Language: {language}

Columns to reduce: {json.dumps(col_names)}
Current values in selected columns (all rows):
{rows_view}

TASK: Shorten/condense values in the selected columns only.
Leave all other columns untouched.

Return ONLY valid JSON array:
[{{"row_id": "...", "values": {{"col_id": "shortened value", ...}}}}, ...]"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if isinstance(parsed, dict):
                parsed = parsed.get("rows") or parsed.get("data")
            if not parsed or not isinstance(parsed, list):
                return {"error": "invalid_response"}
            ai_result = parsed

        else:
            return {"error": "invalid_scope"}

    await emit(
        msg_builder.job_progress(
            job_id, session_id, "ai_complete", "AI changes ready.", 80
        )
    )

    if scope_type != "complete":
        tracker_data = _merge_scoped_changes(tracker_data, scope, ai_result)
    else:
        tracker_data = ai_result

    is_valid, error_msg = _validate_tracker_schema(
        scope.get("tracker_data", {}), tracker_data
    )
    if not is_valid:
        logger.error(f"Schema validation FAILED in reduce: {error_msg}")
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "ai_complete",
                f"Could not apply safely: {error_msg}",
                80,
            )
        )
        return {
            "error": "schema_validation_failed",
            "message": f"The AI changes could not be safely applied. {error_msg}",
        }

    return {
        "intent": "reduce",
        "tracker_modified": True,
        "pending_confirmation": True,
        "tracker_data": tracker_data,
        "tracker_id": tracker_id,
    }


async def _handle_increase(
    user_id, tracker_id, message, language, scope, credits, job_id, session_id, emit
) -> dict:
    """Handle increase intent."""
    logger.info(
        f"_handle_increase: user={user_id}, tracker_id={tracker_id}, job_id={job_id}, scope_type={scope.get('type')}"
    )
    await emit(
        msg_builder.job_progress(
            job_id, session_id, "processing", "Applying AI changes...", 50
        )
    )

    tracker_data = scope.get("tracker_data", {})
    scope_type = scope.get("type", "complete")
    tracker_type = tracker_data.get("type", "")

    add_keywords = {"add", "new", "insert", "append", "sections", "rows"}
    should_add = any(kw in message.lower() for kw in add_keywords)

    if scope_type == "complete":
        tracker_json_str = _build_truncated_tracker_str(tracker_data)

        if should_add and tracker_type == "table":
            schema = tracker_data.get("schema", {})
            columns = schema.get("columns", [])
            col_ids = [c["id"] for c in columns]
            col_ids_str = ", ".join([f'"{id}"' for id in col_ids])

            first_rows = tracker_data.get("rows", [])[:5]
            first_rows_json = json.dumps(first_rows, indent=2)

            prompt = f"""You are a data entry assistant for a tracker tool.

User request: "{message}"
Language: {language}

Current tracker JSON (table type):
{tracker_json_str}

Column schema: {col_ids_str}

Existing rows (context):
{first_rows_json}

TASK: Generate new rows for this tracker following the user's request.
Infer appropriate content from the existing rows as context.

CRITICAL FORMAT RULES:
1. Return ONLY valid JSON array of new row objects.
2. Each row must use EXACTLY these column IDs: {col_ids_str}
3. Generate meaningful content values in {language}.
4. Do NOT include row_id in your output - it will be assigned by the system.

Return JSON array: [{{"values": {{"{col_ids[0]}": "...", "{col_ids[-1] if len(col_ids) > 1 else col_ids[0]}": "..."}}}}]"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if not parsed or not isinstance(parsed, list):
                return {"error": "invalid_response"}

            for new_row_values in parsed:
                if isinstance(new_row_values, dict) and "values" in new_row_values:
                    row_id = f"trk_r_{uuid.uuid4().hex[:8]}"
                    tracker_data["rows"].append(
                        {"row_id": row_id, "values": new_row_values["values"]}
                    )
            ai_result = tracker_data
        else:
            prompt = f"""You are a data content editor for a tracker tool.

User request: "{message}"
Language: {language}

Current tracker JSON:
{tracker_json_str}

TASK: Apply the user's request precisely.
- If the user specifies a particular row ID, item name, column, or any specific element, ONLY expand/elaborate that element.
- If no specific target is mentioned, expand ALL text content values.
- Leave every element not explicitly targeted by the user's request completely unchanged.

CRITICAL FORMAT RULES:
1. You MUST return ONLY valid JSON.
2. The output JSON must have EXACTLY the same structure as the input tracker JSON.
3. Do NOT add, remove, or rename any keys, column IDs, row IDs, or schema fields.
4. Do NOT change tracker_id, type, runbook_id, source_blocks, schema.columns[*].id, schema.columns[*].name, or any row_id values.
5. Only modify string "values" content as instructed. Preserve numeric values unchanged.
6. Return the complete modified tracker JSON, not a diff."""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if not parsed or not isinstance(parsed, dict):
                return {"error": "invalid_response"}
            ai_result = parsed
    else:
        elem = scope.get("selected_element", {})
        sel_row = scope.get("selected_row", {})
        sel_col = scope.get("selected_column", {})
        sel_rows = scope.get("selected_rows", [])
        sel_cols = scope.get("selected_columns", [])

        if scope_type == "selected_element" and elem:
            row_id = elem.get("row_id")
            col_id = elem.get("col_id")
            value = elem.get("value", "")
            col_name = next(
                (
                    c["name"]
                    for c in tracker_data.get("schema", {}).get("columns", [])
                    if c["id"] == col_id
                ),
                col_id,
            )
            prompt = f"""You are a data content editor for a tracker tool.

User request: "{message}"
Language: {language}

Selected cell:
Row ID: {row_id}
Column: {col_name} (ID: {col_id})
Current value: "{value}"

TASK: Expand and elaborate this cell value per the user's request.

Return ONLY valid JSON:
{{"row_id": "{row_id}", "col_id": "{col_id}", "new_value": "<expanded value>"}}"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if not parsed or not isinstance(parsed, dict):
                return {"error": "invalid_response"}
            ai_result = parsed

        elif scope_type == "selected_row" and sel_row:
            row_id = sel_row.get("row_id")
            row_data = sel_row.get("data", {})
            col_schema = json.dumps(tracker_data.get("schema", {}).get("columns", []))
            row_values_json = json.dumps(row_data)

            prompt = f"""You are a data content editor for a tracker tool.

User request: "{message}"
Language: {language}

Selected row:
Row ID: {row_id}
Current values: {row_values_json}
Column schema: {col_schema}

TASK: Expand and elaborate the text values in this row per the user's request.
Preserve numeric values unchanged.

Return ONLY valid JSON:
{{"row_id": "{row_id}", "values": {{<col_id>: <expanded_value>, ...}}}}"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if not parsed or not isinstance(parsed, dict):
                return {"error": "invalid_response"}
            ai_result = parsed

        elif scope_type == "selected_column" and sel_col:
            col_id = sel_col.get("col_id")
            col_name = sel_col.get("name", col_id)
            rows_with_col = json.dumps(
                [
                    {
                        "row_id": r["row_id"],
                        "value": r.get("values", {}).get(col_id, ""),
                    }
                    for r in tracker_data.get("rows", [])
                ]
            )

            prompt = f"""You are a data content editor for a tracker tool.

User request: "{message}"
Language: {language}

Column to expand: {col_name} (ID: {col_id})
Current values across all rows:
{rows_with_col}

TASK: Expand and elaborate the value in column "{col_name}" for every row listed.

Return ONLY valid JSON array:
[{{"row_id": "...", "new_value": "<expanded value>"}}, ...]"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if not parsed or not isinstance(parsed, list):
                return {"error": "invalid_response"}
            ai_result = parsed

        elif scope_type == "selected_rows" and sel_rows:
            col_schema = tracker_data.get("schema", {}).get("columns", [])
            col_map = ", ".join(
                f'{c["id"]} = "{c.get("name", c["id"])}"' for c in col_schema
            )
            rows_json = json.dumps(sel_rows, indent=2)

            prompt = f"""You are a data content editor for a tracker tool.

User request: "{message}"
Language: {language}

Column ID mapping: {col_map}

Selected rows (expand text values as requested):
{rows_json}

TASK: Apply the user's request to the text values in the selected rows.
Preserve numeric values and row_id unchanged.

Return ONLY a valid JSON array — no wrapper object, no markdown:
[{{"row_id": "...", "values": {{"col_id": "expanded value", ...}}}}, ...]"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            # Tolerate {"rows": [...]} wrapper from AI
            if isinstance(parsed, dict):
                parsed = (
                    parsed.get("rows") or parsed.get("data") or parsed.get("result")
                )
            if not parsed or not isinstance(parsed, list):
                logger.warning(
                    "invalid_response in selected_rows increase, trying AI fallback"
                )
                fallback = await _ai_fallback(
                    user_id,
                    message,
                    tracker_data,
                    language,
                    credits,
                    hint="expand selected rows",
                )
                if fallback:
                    is_valid, _ = _validate_tracker_schema(
                        scope.get("tracker_data", {}), fallback
                    )
                    if is_valid:
                        return {
                            "intent": "increase",
                            "tracker_modified": True,
                            "pending_confirmation": True,
                            "tracker_data": fallback,
                            "tracker_id": tracker_id,
                        }
                return {"error": "invalid_response"}
            ai_result = parsed

        elif scope_type == "selected_columns" and sel_cols:
            col_ids_to_expand = [c.get("col_id") for c in sel_cols if c.get("col_id")]
            col_names = {c.get("col_id"): c.get("name") for c in sel_cols}
            rows_view = json.dumps(
                [
                    {
                        "row_id": r["row_id"],
                        "values": {
                            cid: r.get("values", {}).get(cid, "")
                            for cid in col_ids_to_expand
                        },
                    }
                    for r in tracker_data.get("rows", [])
                ]
            )

            prompt = f"""You are a data content editor for a tracker tool.

User request: "{message}"
Language: {language}

Columns to expand: {json.dumps(col_names)}
Current values in selected columns (all rows):
{rows_view}

TASK: Expand and elaborate values in the selected columns only.
Leave all other columns untouched.

Return ONLY valid JSON array:
[{{"row_id": "...", "values": {{<col_id>: <expanded_value>, ...}}}}, ...]"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if not parsed or not isinstance(parsed, list):
                logger.warning(
                    "invalid_response in selected_columns increase, trying AI fallback"
                )
                fallback = await _ai_fallback(
                    user_id,
                    message,
                    tracker_data,
                    language,
                    credits,
                    hint="expand selected columns",
                )
                if fallback:
                    is_valid, _ = _validate_tracker_schema(
                        scope.get("tracker_data", {}), fallback
                    )
                    if is_valid:
                        return {
                            "intent": "increase",
                            "tracker_modified": True,
                            "pending_confirmation": True,
                            "tracker_data": fallback,
                            "tracker_id": tracker_id,
                        }
                return {"error": "invalid_response"}
            ai_result = parsed

        else:
            return {"error": "invalid_scope"}

    await emit(
        msg_builder.job_progress(
            job_id, session_id, "ai_complete", "AI changes ready.", 80
        )
    )

    if scope_type != "complete":
        tracker_data = _merge_scoped_changes(tracker_data, scope, ai_result)
    else:
        tracker_data = ai_result

    is_valid, error_msg = _validate_tracker_schema(
        scope.get("tracker_data", {}), tracker_data
    )
    if not is_valid:
        logger.error(f"Schema validation FAILED in increase: {error_msg}")
        return {
            "error": "schema_validation_failed",
            "message": f"The AI changes could not be safely applied. {error_msg}",
        }

    return {
        "intent": "increase",
        "tracker_modified": True,
        "pending_confirmation": True,
        "tracker_data": tracker_data,
        "tracker_id": tracker_id,
    }


async def _handle_modify_content(
    user_id, tracker_id, message, language, scope, credits, job_id, session_id, emit
) -> dict:
    """Handle modify_content intent."""
    logger.info(
        f"_handle_modify_content: user={user_id}, tracker_id={tracker_id}, job_id={job_id}, scope_type={scope.get('type')}"
    )
    await emit(
        msg_builder.job_progress(
            job_id, session_id, "processing", "Applying AI changes...", 50
        )
    )

    tracker_data = scope.get("tracker_data", {})
    scope_type = scope.get("type", "complete")

    if scope_type == "complete":
        tracker_json_str = _build_truncated_tracker_str(tracker_data)
        prompt = f"""You are a data editor for a tracker tool.

User instruction: "{message}"
Language: {language}

Current tracker JSON:
{tracker_json_str}

TASK: Apply the user's modification instruction to the relevant content values.
Only change what the user explicitly requested.

CRITICAL FORMAT RULES:
1. You MUST return ONLY valid JSON - complete tracker JSON.
2. The output JSON must have EXACTLY the same structure as the input JSON.
3. Do NOT add, remove, or rename any keys, column IDs, row IDs, or schema fields.
4. Do NOT change tracker_id, type, runbook_id, source_blocks, schema.columns[*].id, schema.columns[*].name, or any row_id values.
5. Only change "values" content as instructed.
6. Leave unchanged rows/cells/records exactly as they are.
7. Return the complete modified tracker JSON."""

        response = await get_fireworks_response2(
            user_id, prompt, role="system", credits=credits, temp=0.7
        )
        if not response or response == "INSUFFICIENT":
            return {"error": "insufficient_credits"}
        parsed = extract_json_safe(response)
        if not parsed or not isinstance(parsed, dict):
            return {"error": "invalid_response"}
        ai_result = parsed
    else:
        elem = scope.get("selected_element", {})
        sel_row = scope.get("selected_row", {})
        sel_col = scope.get("selected_column", {})
        sel_rows = scope.get("selected_rows", [])
        sel_cols = scope.get("selected_columns", [])

        if scope_type == "selected_element" and elem:
            row_id = elem.get("row_id")
            col_id = elem.get("col_id")
            current_value = elem.get("value", "")
            prompt = f"""You are a data editor for a tracker tool.

User instruction: "{message}"
Language: {language}

Cell to modify:
Row ID: {row_id}, Column ID: {col_id}
Current value: "{current_value}"

Apply the user's instruction to this specific cell value only.

Return ONLY valid JSON:
{{"row_id": "{row_id}", "col_id": "{col_id}", "new_value": "<modified value>"}}"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if not parsed or not isinstance(parsed, dict):
                return {"error": "invalid_response"}
            ai_result = parsed

        elif scope_type == "selected_row" and sel_row:
            row_id = sel_row.get("row_id")
            col_schema = tracker_data.get("schema", {}).get("columns", [])
            col_map = ", ".join(
                f'{c["id"]} = "{c.get("name", c["id"])}"' for c in col_schema
            )
            row_values_json = json.dumps(sel_row.get("values", {}))

            prompt = f"""You are a data editor for a tracker tool.

User instruction: "{message}"
Language: {language}

Column ID mapping: {col_map}
Row ID: {row_id}
Current values: {row_values_json}

Apply the user's instruction to the relevant values in this row.

Return ONLY valid JSON:
{{"row_id": "{row_id}", "values": {{"col_id": "modified value", ...}}}}"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if not parsed or not isinstance(parsed, dict):
                return {"error": "invalid_response"}
            ai_result = parsed

        elif scope_type == "selected_column" and sel_col:
            col_id = sel_col.get("col_id")
            col_name = sel_col.get("name", col_id)
            rows_with_col = json.dumps(
                [
                    {
                        "row_id": r["row_id"],
                        "value": r.get("values", {}).get(col_id, ""),
                    }
                    for r in tracker_data.get("rows", [])
                ]
            )

            prompt = f"""You are a data editor for a tracker tool.

User instruction: "{message}"
Language: {language}

Column to modify: {col_name} (ID: {col_id})
Current values across all rows:
{rows_with_col}

Apply the user's instruction to the value in column "{col_name}" for each row.

Return ONLY valid JSON array:
[{{"row_id": "...", "new_value": "<modified value>"}}, ...]"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if isinstance(parsed, dict):
                parsed = parsed.get("rows") or parsed.get("data")
            if not parsed or not isinstance(parsed, list):
                return {"error": "invalid_response"}
            ai_result = parsed

        elif scope_type == "selected_rows" and sel_rows:
            col_schema = tracker_data.get("schema", {}).get("columns", [])
            col_map = ", ".join(
                f'{c["id"]} = "{c.get("name", c["id"])}"' for c in col_schema
            )
            rows_json = json.dumps(sel_rows, indent=2)

            prompt = f"""You are a data editor for a tracker tool.

User instruction: "{message}"
Language: {language}

Column ID mapping: {col_map}

Selected rows to modify:
{rows_json}

Apply the user's instruction to the relevant values in the selected rows.

Return ONLY a valid JSON array — no wrapper object, no markdown:
[{{"row_id": "...", "values": {{"col_id": "modified value", ...}}}}, ...]"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if isinstance(parsed, dict):
                parsed = (
                    parsed.get("rows") or parsed.get("data") or parsed.get("result")
                )
            if not parsed or not isinstance(parsed, list):
                logger.warning(
                    "invalid_response in selected_rows modify_content, trying AI fallback"
                )
                fallback = await _ai_fallback(
                    user_id,
                    message,
                    tracker_data,
                    language,
                    credits,
                    hint="modify selected rows",
                )
                if fallback:
                    is_valid, _ = _validate_tracker_schema(
                        scope.get("tracker_data", {}), fallback
                    )
                    if is_valid:
                        return {
                            "intent": "modify_content",
                            "tracker_modified": True,
                            "pending_confirmation": True,
                            "tracker_data": fallback,
                            "tracker_id": tracker_id,
                        }
                return {"error": "invalid_response"}
            ai_result = parsed

        elif scope_type == "selected_columns" and sel_cols:
            col_ids_to_modify = [c.get("col_id") for c in sel_cols if c.get("col_id")]
            col_names = {c.get("col_id"): c.get("name") for c in sel_cols}
            rows_view = json.dumps(
                [
                    {
                        "row_id": r["row_id"],
                        "values": {
                            cid: r.get("values", {}).get(cid, "")
                            for cid in col_ids_to_modify
                        },
                    }
                    for r in tracker_data.get("rows", [])
                ]
            )

            prompt = f"""You are a data editor for a tracker tool.

User instruction: "{message}"
Language: {language}

Columns to modify: {json.dumps(col_names)}
Current values in selected columns (all rows):
{rows_view}

Apply the user's instruction to the selected columns only.

Return ONLY valid JSON array:
[{{"row_id": "...", "values": {{"col_id": "modified value", ...}}}}, ...]"""

            response = await get_fireworks_response2(
                user_id, prompt, role="system", credits=credits, temp=0.7
            )
            if not response or response == "INSUFFICIENT":
                return {"error": "insufficient_credits"}
            parsed = extract_json_safe(response)
            if isinstance(parsed, dict):
                parsed = parsed.get("rows") or parsed.get("data")
            if not parsed or not isinstance(parsed, list):
                return {"error": "invalid_response"}
            ai_result = parsed

        else:
            return {"error": "invalid_scope"}

    await emit(
        msg_builder.job_progress(
            job_id, session_id, "ai_complete", "AI changes ready.", 80
        )
    )

    if scope_type != "complete":
        tracker_data = _merge_scoped_changes(tracker_data, scope, ai_result)
    else:
        tracker_data = ai_result

    is_valid, error_msg = _validate_tracker_schema(
        scope.get("tracker_data", {}), tracker_data
    )
    if not is_valid:
        logger.error(f"Schema validation FAILED in modify_content: {error_msg}")
        return {
            "error": "schema_validation_failed",
            "message": f"The AI changes could not be safely applied. {error_msg}",
        }

    return {
        "intent": "modify_content",
        "tracker_modified": True,
        "pending_confirmation": True,
        "tracker_data": tracker_data,
        "tracker_id": tracker_id,
    }


async def _handle_complex_think(
    user_id, tracker_id, message, language, scope, credits, job_id, session_id, emit
) -> dict:
    """Handle complex multi-intent requests on selected rows/columns using the think model."""
    logger.info(
        f"_handle_complex_think: user={user_id}, tracker_id={tracker_id}, scope={scope.get('type')}"
    )
    await emit(
        msg_builder.job_progress(
            job_id, session_id, "processing", "Applying all changes simultaneously...", 50
        )
    )

    tracker_data = scope.get("tracker_data", {})
    scope_type = scope.get("type", "")
    sel_rows = scope.get("selected_rows", [])
    sel_cols = scope.get("selected_columns", [])

    col_schema = tracker_data.get("schema", {}).get("columns", [])
    col_map = ", ".join(f'{c["id"]} = "{c.get("name", c["id"])}"' for c in col_schema)

    if scope_type == "selected_rows" and sel_rows:
        rows_json = json.dumps(sel_rows, indent=2)
        prompt = f"""You are a precise data editor for a tracker tool.

User request: "{message}"
Language: {language}

Column ID mapping: {col_map}

The user has selected {len(sel_rows)} rows. Apply ALL aspects of the request simultaneously.
Do not split the task — treat every part of the request as one unified operation.

Selected rows (full data):
{rows_json}

Return ONLY a valid JSON array — no wrapper object, no markdown fences:
[{{"row_id": "...", "values": {{"col_id": "new value", ...}}}}, ...]

Rules:
1. Every selected row_id must appear in the output.
2. Preserve numeric values unchanged.
3. Do NOT add, remove, or rename column IDs.
4. Apply every aspect of the user request (resize, sync, rewrite, etc.) simultaneously."""

    elif scope_type == "selected_columns" and sel_cols:
        col_ids = [c.get("col_id") for c in sel_cols if c.get("col_id")]
        col_names = {c.get("col_id"): c.get("name") for c in sel_cols}
        rows_view = json.dumps(
            [
                {
                    "row_id": r["row_id"],
                    "values": {cid: r.get("values", {}).get(cid, "") for cid in col_ids},
                }
                for r in tracker_data.get("rows", [])
            ],
            indent=2,
        )
        prompt = f"""You are a precise data editor for a tracker tool.

User request: "{message}"
Language: {language}

Selected columns: {json.dumps(col_names)}

Apply ALL aspects of the request simultaneously to the selected columns across all rows.
Do not split the task — treat every part of the request as one unified operation.

Current values in selected columns:
{rows_view}

Return ONLY a valid JSON array — no wrapper object, no markdown fences:
[{{"row_id": "...", "values": {{"col_id": "new value", ...}}}}, ...]

Rules:
1. Every row_id must appear in the output.
2. Preserve numeric values unchanged.
3. Do NOT add, remove, or rename column IDs.
4. Apply every aspect of the user request simultaneously."""

    else:
        return {"error": "invalid_scope"}

    response = await get_think_fire_response2_og(
        user_message=prompt, user_id=user_id, credits=credits, total_input_chars=len(prompt)
    )
    if not response or response == "INSUFFICIENT":
        return {"error": "insufficient_credits"}

    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned).rstrip("`").strip()

    ai_result = None
    try:
        candidate = json.loads(cleaned)
        if isinstance(candidate, list):
            ai_result = candidate
    except (json.JSONDecodeError, ValueError):
        pass

    if not ai_result:
        array_match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if array_match:
            try:
                candidate = json.loads(array_match.group(0))
                if isinstance(candidate, list):
                    ai_result = candidate
            except (json.JSONDecodeError, ValueError):
                pass

    if not ai_result:
        obj = extract_json_safe(cleaned)
        if isinstance(obj, dict):
            ai_result = obj.get("rows") or obj.get("data") or obj.get("result")

    if not ai_result or not isinstance(ai_result, list):
        logger.error("_handle_complex_think: invalid_response from think model")
        return {"error": "invalid_response"}

    await emit(
        msg_builder.job_progress(job_id, session_id, "ai_complete", "AI changes ready.", 80)
    )

    tracker_data = _merge_scoped_changes(tracker_data, scope, ai_result)
    is_valid, error_msg = _validate_tracker_schema(scope.get("tracker_data", {}), tracker_data)
    if not is_valid:
        logger.error(f"Schema validation FAILED in _handle_complex_think: {error_msg}")
        return {
            "error": "schema_validation_failed",
            "message": f"The AI changes could not be safely applied. {error_msg}",
        }

    return {
        "intent": "complex_think",
        "tracker_modified": True,
        "pending_confirmation": True,
        "tracker_data": tracker_data,
        "tracker_id": tracker_id,
    }


async def _handle_add_column(
    user_id, tracker_id, intent_obj, tracker_data, language, job_id, session_id, emit
) -> dict:
    """Add a new column to the tracker."""
    column_name = intent_obj.get("column_name", "").strip()
    column_type = intent_obj.get("column_type", "text")
    default_value = intent_obj.get("default_value", "")

    logger.info(f"_handle_add_column: column_name={column_name}, type={column_type}")

    if not column_name:
        return {"error": "missing_column_name", "message": "Column name is required."}

    tracker_type = tracker_data.get("type", "")
    if tracker_type != "table":
        return {
            "intent": "add_column",
            "tracker_modified": False,
            "response": f"Adding columns is only supported for table trackers, not '{tracker_type}'.",
        }

    schema = tracker_data.get("schema", {})
    schema_cols = schema.get("columns", [])

    # Duplicate check
    existing_names = {c.get("source_column", "").lower() for c in schema_cols}
    if column_name.lower() in existing_names:
        return {
            "intent": "add_column",
            "tracker_modified": False,
            "response": f"A column named '{column_name}' already exists.",
        }

    # Generate new column ID
    new_col_id = f"col_{len(schema_cols) + 1}"
    schema_cols.append(
        {
            "id": new_col_id,
            "name": column_name,
            "source_column": column_name,
            "type": column_type,
            "enum": [],
        }
    )

    # Backfill existing rows if default_value provided
    if default_value is not None:
        for row in tracker_data.get("rows", []):
            row["values"].setdefault(new_col_id, default_value)

    return {
        "intent": "add_column",
        "tracker_modified": True,
        "tracker_data": tracker_data,
        "tracker_id": tracker_id,
    }


def _find_column(schema_cols: list, name: str):
    """Find a column by exact then partial name match (case-insensitive).
    Handles:
    - "category" matching "Category / Type" (partial substring)
    - "col_3: Category / Type" (AI returning col_id prefix from old schema summary format)
    """
    lname = name.lower().strip()
    # Strip col_id prefix if AI returned "col_3: Some Name" format
    if ": " in lname:
        lname = lname.split(": ", 1)[1].strip()
    # Exact match first
    target = next(
        (
            c
            for c in schema_cols
            if c.get("source_column", "").lower() == lname
            or c.get("name", "").lower() == lname
        ),
        None,
    )
    if target:
        return target
    # Partial: user's input is a substring of the column name
    return next(
        (
            c
            for c in schema_cols
            if lname in c.get("source_column", "").lower()
            or lname in c.get("name", "").lower()
        ),
        None,
    )


async def _ai_fallback(
    user_id: str,
    message: str,
    tracker_data: dict,
    language: str,
    credits,
    hint: str = "",
) -> dict | None:
    """Universal AI fallback: pass full tracker + request to AI and let it apply the change.
    Used when structured handlers fail (invalid format, column not found, etc.).
    """
    tracker_json_str = _build_truncated_tracker_str(tracker_data)
    hint_line = f"Attempted operation: {hint}\n" if hint else ""

    prompt = f"""You are a data tracker assistant.

User request: "{message}"
Language: {language}
{hint_line}
Current tracker data:
{tracker_json_str}

Apply the user's request to the tracker data. Use the tracker schema and content to understand what the user is referring to, even if the wording is approximate.

CRITICAL FORMAT RULES:
1. Return ONLY valid JSON — the complete modified tracker.
2. Preserve EXACTLY the same structure as the input.
3. Do NOT change tracker_id, type, schema column IDs, or row_ids.
4. Only change what the user requested.
5. No markdown fences, no wrapper objects."""

    try:
        response = await get_fireworks_response2(
            user_id, prompt, role="system", credits=credits, temp=0.3
        )
        if not response or response == "INSUFFICIENT":
            return None
        parsed = extract_json_safe(response)
        if parsed and isinstance(parsed, dict) and parsed.get("schema"):
            logger.info("_ai_fallback: AI returned valid tracker structure")
            return parsed
    except Exception as e:
        logger.exception("_ai_fallback error: %s", e)
    return None


async def _handle_delete_column(
    user_id,
    tracker_id,
    intent_obj,
    tracker_data,
    language,
    message,
    credits,
    job_id,
    session_id,
    emit,
) -> dict:
    """Delete a column from the tracker."""
    column_name = intent_obj.get("column_name", "").strip()
    logger.info(f"_handle_delete_column: column_name={column_name}")

    schema_cols = tracker_data.get("schema", {}).get("columns", [])
    target = _find_column(schema_cols, column_name)
    if not target:
        logger.info(f"_handle_delete_column: falling back to AI")
        fallback = await _ai_fallback(
            user_id, message, tracker_data, language, credits, hint="delete column"
        )
        if fallback:
            return {
                "intent": "delete_column",
                "tracker_modified": True,
                "tracker_data": fallback,
                "tracker_id": tracker_id,
            }
        available = ", ".join(c.get("name", c.get("id")) for c in schema_cols)
        return {
            "intent": "delete_column",
            "tracker_modified": False,
            "response": f"I couldn't find a column matching '{column_name}'. Available columns are: {available}.",
        }

    col_id = target["id"]
    schema_cols.remove(target)
    for row in tracker_data.get("rows", []):
        row["values"].pop(col_id, None)

    return {
        "intent": "delete_column",
        "tracker_modified": True,
        "tracker_data": tracker_data,
        "tracker_id": tracker_id,
    }


async def _handle_change_column_name(
    user_id,
    tracker_id,
    intent_obj,
    tracker_data,
    language,
    message,
    credits,
    job_id,
    session_id,
    emit,
) -> dict:
    """Rename a column in the tracker."""
    old_name = intent_obj.get("old_column_name", "").strip()
    new_name = intent_obj.get("new_column_name", "").strip()
    logger.info(f"_handle_change_column_name: {old_name} → {new_name}")

    schema_cols = tracker_data.get("schema", {}).get("columns", [])
    target = _find_column(schema_cols, old_name)
    if not target:
        logger.info(f"_handle_change_column_name: falling back to AI")
        fallback = await _ai_fallback(
            user_id, message, tracker_data, language, credits, hint="rename column"
        )
        if fallback:
            return {
                "intent": "change_column_name",
                "tracker_modified": True,
                "tracker_data": fallback,
                "tracker_id": tracker_id,
            }
        available = ", ".join(c.get("name", c.get("id")) for c in schema_cols)
        return {
            "intent": "change_column_name",
            "tracker_modified": False,
            "response": f"I couldn't find a column matching '{old_name}'. Available columns are: {available}.",
        }

    target["name"] = new_name
    target["source_column"] = new_name

    return {
        "intent": "change_column_name",
        "tracker_modified": True,
        "tracker_data": tracker_data,
        "tracker_id": tracker_id,
    }


async def _dispatch_single_intent(
    intent_type,
    intent_obj,
    user_id,
    tracker_id,
    message,
    language,
    scope,
    credits,
    job_id,
    session_id,
    emit,
) -> dict:
    """Route a single intent to its handler."""
    if intent_type == "add_column":
        return await _handle_add_column(
            user_id,
            tracker_id,
            intent_obj,
            scope["tracker_data"],
            language,
            job_id,
            session_id,
            emit,
        )
    elif intent_type == "delete_column":
        return await _handle_delete_column(
            user_id,
            tracker_id,
            intent_obj,
            scope["tracker_data"],
            language,
            message,
            credits,
            job_id,
            session_id,
            emit,
        )
    elif intent_type == "change_column_name":
        return await _handle_change_column_name(
            user_id,
            tracker_id,
            intent_obj,
            scope["tracker_data"],
            language,
            message,
            credits,
            job_id,
            session_id,
            emit,
        )
    elif intent_type == "normal_greeting":
        return await _handle_greeting(
            user_id, message, language, credits, job_id, session_id, emit
        )
    elif intent_type == "explain":
        return await _handle_explain(
            user_id, message, language, scope, credits, job_id, session_id, emit
        )
    elif intent_type == "reduce":
        return await _handle_reduce(
            user_id,
            tracker_id,
            message,
            language,
            scope,
            credits,
            job_id,
            session_id,
            emit,
        )
    elif intent_type == "increase":
        return await _handle_increase(
            user_id,
            tracker_id,
            message,
            language,
            scope,
            credits,
            job_id,
            session_id,
            emit,
        )
    elif intent_type == "modify_content":
        return await _handle_modify_content(
            user_id,
            tracker_id,
            message,
            language,
            scope,
            credits,
            job_id,
            session_id,
            emit,
        )
    else:
        return await _handle_greeting(
            user_id, message, language, credits, job_id, session_id, emit
        )


async def _dispatch_intents(
    intents,
    user_id,
    tracker_id,
    message,
    language,
    scope,
    credits,
    job_id,
    session_id,
    emit,
) -> dict:
    """
    Dispatch multiple intents sequentially with tracker_data threading.
    Each handler receives and returns the updated tracker_data.
    """
    current_tracker = scope.get("tracker_data", {})
    total = len(intents)
    all_modified = False
    last_response = None
    intent_errors = []

    _COMPLEX_CONTENT_INTENTS = {"reduce", "increase", "modify_content"}
    _COMPLEX_TRIGGER_SCOPES = {"selected_rows", "selected_columns"}
    scope_type = scope.get("type", "complete")
    intent_types = {obj.get("type") for obj in intents}
    content_intents_present = intent_types & _COMPLEX_CONTENT_INTENTS

    if scope_type in _COMPLEX_TRIGGER_SCOPES and len(content_intents_present) >= 2:
        logger.info(
            f"_dispatch_intents: routing to _handle_complex_think "
            f"(scope={scope_type}, content_intents={content_intents_present})"
        )
        await emit(
            msg_builder.job_progress(
                job_id, session_id, "step_complex", "Applying changes simultaneously...", 50
            )
        )
        try:
            result = await _handle_complex_think(
                user_id, tracker_id, message, language, scope, credits, job_id, session_id, emit
            )
        except Exception as e:
            logger.exception(f"Exception in _handle_complex_think: {e}")
            result = {"error": str(e)}

        if "error" in result:
            err_code = result.get("error")
            detail = _ERROR_LABELS.get(str(err_code), str(err_code))
            error_msg = f"Combined operation: {detail}"
            logger.error(f"_handle_complex_think failed for job {job_id}: {error_msg}")
            return {
                "intents": [i.get("type") for i in intents],
                "tracker_modified": False,
                "pending_confirmation": False,
                "error": "processing_failed",
                "message": error_msg,
            }

        return {
            "intents": [i.get("type") for i in intents],
            "tracker_modified": result.get("tracker_modified", True),
            "pending_confirmation": result.get("pending_confirmation", True),
            "tracker_data": result.get("tracker_data"),
            "response": result.get("response"),
            "intent_errors": None,
        }

    for idx, intent_obj in enumerate(intents):
        intent_type = intent_obj.get("type", "normal_greeting")
        step_label = (
            f"Step {idx + 1}/{total}: {INTENT_MESSAGES.get(intent_type, intent_type)}"
        )
        progress = int(15 + (idx / total) * 65) if total > 0 else 50

        await emit(
            msg_builder.job_progress(
                job_id, session_id, f"step_{idx+1}", step_label, progress
            )
        )
        logger.info(f"Dispatching intent {idx+1}/{total}: {intent_type}")

        step_scope = {**scope, "tracker_data": current_tracker}

        try:
            result = await _dispatch_single_intent(
                intent_type,
                intent_obj,
                user_id,
                tracker_id,
                message,
                language,
                step_scope,
                credits,
                job_id,
                session_id,
                emit,
            )

            if "tracker_data" in result:
                current_tracker = result["tracker_data"]
            if result.get("tracker_modified"):
                all_modified = True
            if result.get("response"):
                last_response = result["response"]

            if "error" in result:
                logger.error(f"Intent {intent_type} failed: {result}")
                intent_errors.append({intent_type: result.get("error")})
        except Exception as e:
            logger.exception(f"Exception in intent {intent_type}: {e}")
            intent_errors.append({intent_type: str(e)})

    if intent_errors:
        parts = []
        for err_dict in intent_errors:
            for intent_type, err_code in err_dict.items():
                label = INTENT_MESSAGES.get(intent_type, intent_type)
                detail = _ERROR_LABELS.get(str(err_code), str(err_code))
                parts.append(f"{label}: {detail}")
        error_msg = "; ".join(parts)
        logger.error(f"Intents failed for job {job_id}: {error_msg}")
        return {
            "intents": [i.get("type") for i in intents],
            "tracker_modified": False,
            "pending_confirmation": False,
            "response": None,
            "intent_errors": intent_errors,
            "error": "processing_failed",
            "message": error_msg,
        }

    return {
        "intents": [i.get("type") for i in intents],
        "tracker_modified": all_modified,
        "pending_confirmation": all_modified,
        "tracker_data": current_tracker,
        "response": last_response,
        "intent_errors": intent_errors if intent_errors else None,
    }


async def _complete_tracker_worker(data, job_id=None, session_id=None):
    """Background worker for complete_tracker_change."""
    user_id = data.get("user_id")
    tracker_id = data.get("tracker_id")
    message = data.get("message")
    tracker_data = data.get("tracker_data")

    logger.info(
        f"_complete_tracker_worker: job_id={job_id}, user={user_id}, tracker={tracker_id}"
    )

    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            await send(ws_sender, msg, user_id)

    conn = None
    try:
        conn = connect_to_rds()
        credits = Credits(conn)

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "init", "Starting tracker AI...", 5
            )
        )

        if not all([user_id, tracker_id, message, tracker_data]):
            await emit(
                msg_builder.job_error(job_id, session_id, "Missing required fields")
            )
            return {"error": "missing_fields"}

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "detecting_intent", "Analyzing your request...", 15
            )
        )
        intents = await get_intents(user_id, message, credits, tracker_data)
        logger.info(f"Intents detected: {[i.get('type') for i in intents]}")
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "intent_detected",
                ", ".join(
                    INTENT_MESSAGES.get(i.get("type", ""), i.get("type", ""))
                    for i in intents
                ),
                25,
            )
        )

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "language_detection", "Detecting language...", 30
            )
        )
        language = await detect_language(user_id, message, credits)

        scope = {"type": "complete", "tracker_data": tracker_data}
        result = await _dispatch_intents(
            intents=intents,
            user_id=user_id,
            tracker_id=tracker_id,
            message=message,
            language=language,
            scope=scope,
            credits=credits,
            job_id=job_id,
            session_id=session_id,
            emit=emit,
        )

        if "error" not in result:
            intent = result.get("intent", "unknown")
            logger.info(
                f"Job {job_id} completed: intent={intent}, pending_confirmation={result.get('pending_confirmation')}"
            )
            msg_text = (
                "Changes ready. Please confirm to save."
                if result.get("pending_confirmation")
                else result.get("response", "Done.")
            )
            await emit(msg_builder.job_success(job_id, session_id, msg_text))
        else:
            error = result.get("error", "unknown")
            logger.error(f"Job {job_id} FAILED: {error} - {result.get('message', '')}")
            await emit(
                msg_builder.job_error(
                    job_id, session_id, result.get("message", "An error occurred")
                )
            )

        return result

    except Exception as e:
        logger.exception("complete_tracker_worker error: %s", e)
        await emit(
            msg_builder.job_error(job_id, session_id, f"An error occurred: {str(e)}")
        )
        raise
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


async def _selected_element_worker(data, job_id=None, session_id=None):
    """Background worker for selected_tracker_change."""
    user_id = data.get("user_id")
    tracker_id = data.get("tracker_id")
    message = data.get("message")
    tracker_data = data.get("tracker_data")
    selected_element = data.get("selected_element", {})

    logger.info(
        f"_selected_element_worker: job_id={job_id}, user={user_id}, tracker={tracker_id}, row_id={selected_element.get('row_id')}"
    )

    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            await send(ws_sender, msg, user_id)

    conn = None
    try:
        conn = connect_to_rds()
        credits = Credits(conn)

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "init", "Starting tracker AI...", 5
            )
        )

        if not all([user_id, tracker_id, message, tracker_data]):
            await emit(
                msg_builder.job_error(job_id, session_id, "Missing required fields")
            )
            return {"error": "missing_fields"}

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "detecting_intent", "Analyzing your request...", 15
            )
        )
        intents = await get_intents(user_id, message, credits, tracker_data)
        logger.info(f"Intents detected: {[i.get('type') for i in intents]}")
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "intent_detected",
                ", ".join(
                    INTENT_MESSAGES.get(i.get("type", ""), i.get("type", ""))
                    for i in intents
                ),
                25,
            )
        )

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "language_detection", "Detecting language...", 30
            )
        )
        language = await detect_language(user_id, message, credits)

        # Normalize column_id → col_id (clients may send either field name)
        if "column_id" in selected_element and "col_id" not in selected_element:
            selected_element = {
                **selected_element,
                "col_id": selected_element["column_id"],
            }

        scope = {
            "type": "selected_element",
            "tracker_data": tracker_data,
            "selected_element": selected_element,
        }
        result = await _dispatch_intents(
            intents=intents,
            user_id=user_id,
            tracker_id=tracker_id,
            message=message,
            language=language,
            scope=scope,
            credits=credits,
            job_id=job_id,
            session_id=session_id,
            emit=emit,
        )

        if "error" not in result:
            intent = result.get("intent", "unknown")
            logger.info(
                f"Job {job_id} completed: intent={intent}, pending_confirmation={result.get('pending_confirmation')}"
            )
            msg_text = (
                "Changes ready. Please confirm to save."
                if result.get("pending_confirmation")
                else result.get("response", "Done.")
            )
            await emit(msg_builder.job_success(job_id, session_id, msg_text))
        else:
            error = result.get("error", "unknown")
            logger.error(f"Job {job_id} FAILED: {error} - {result.get('message', '')}")
            await emit(
                msg_builder.job_error(
                    job_id, session_id, result.get("message", "An error occurred")
                )
            )

        return result

    except Exception as e:
        logger.exception("selected_element_worker error: %s", e)
        await emit(
            msg_builder.job_error(job_id, session_id, f"An error occurred: {str(e)}")
        )
        raise
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


async def _selected_row_worker(data, job_id=None, session_id=None):
    """Background worker for selected_row_tracker_change."""
    user_id = data.get("user_id")
    tracker_id = data.get("tracker_id")
    message = data.get("message")
    tracker_data = data.get("tracker_data")
    selected_row = data.get("selected_row", {})

    logger.info(
        f"_selected_row_worker: job_id={job_id}, user={user_id}, tracker={tracker_id}, row_id={selected_row.get('row_id')}"
    )

    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            await send(ws_sender, msg, user_id)

    conn = None
    try:
        conn = connect_to_rds()
        credits = Credits(conn)

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "init", "Starting tracker AI...", 5
            )
        )

        if not all([user_id, tracker_id, message, tracker_data]):
            await emit(
                msg_builder.job_error(job_id, session_id, "Missing required fields")
            )
            return {"error": "missing_fields"}

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "detecting_intent", "Analyzing your request...", 15
            )
        )
        intents = await get_intents(user_id, message, credits, tracker_data)
        logger.info(f"Intents detected: {[i.get('type') for i in intents]}")
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "intent_detected",
                ", ".join(
                    INTENT_MESSAGES.get(i.get("type", ""), i.get("type", ""))
                    for i in intents
                ),
                25,
            )
        )

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "language_detection", "Detecting language...", 30
            )
        )
        language = await detect_language(user_id, message, credits)

        scope = {
            "type": "selected_row",
            "tracker_data": tracker_data,
            "selected_row": selected_row,
        }
        result = await _dispatch_intents(
            intents=intents,
            user_id=user_id,
            tracker_id=tracker_id,
            message=message,
            language=language,
            scope=scope,
            credits=credits,
            job_id=job_id,
            session_id=session_id,
            emit=emit,
        )

        if "error" not in result:
            intent = result.get("intent", "unknown")
            logger.info(
                f"Job {job_id} completed: intent={intent}, pending_confirmation={result.get('pending_confirmation')}"
            )
            msg_text = (
                "Changes ready. Please confirm to save."
                if result.get("pending_confirmation")
                else result.get("response", "Done.")
            )
            await emit(msg_builder.job_success(job_id, session_id, msg_text))
        else:
            error = result.get("error", "unknown")
            logger.error(f"Job {job_id} FAILED: {error} - {result.get('message', '')}")
            await emit(
                msg_builder.job_error(
                    job_id, session_id, result.get("message", "An error occurred")
                )
            )

        return result

    except Exception as e:
        logger.exception("selected_row_worker error: %s", e)
        await emit(
            msg_builder.job_error(job_id, session_id, f"An error occurred: {str(e)}")
        )
        raise
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


async def _selected_column_worker(data, job_id=None, session_id=None):
    """Background worker for selected_column_tracker_change."""
    user_id = data.get("user_id")
    tracker_id = data.get("tracker_id")
    message = data.get("message")
    tracker_data = data.get("tracker_data")
    selected_column = data.get("selected_column", {})

    logger.info(
        f"_selected_column_worker: job_id={job_id}, user={user_id}, tracker={tracker_id}, col_id={selected_column.get('col_id')}"
    )

    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            await send(ws_sender, msg, user_id)

    conn = None
    try:
        conn = connect_to_rds()
        credits = Credits(conn)

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "init", "Starting tracker AI...", 5
            )
        )

        if not all([user_id, tracker_id, message, tracker_data]):
            await emit(
                msg_builder.job_error(job_id, session_id, "Missing required fields")
            )
            return {"error": "missing_fields"}

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "detecting_intent", "Analyzing your request...", 15
            )
        )
        intents = await get_intents(user_id, message, credits, tracker_data)
        logger.info(f"Intents detected: {[i.get('type') for i in intents]}")
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "intent_detected",
                ", ".join(
                    INTENT_MESSAGES.get(i.get("type", ""), i.get("type", ""))
                    for i in intents
                ),
                25,
            )
        )

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "language_detection", "Detecting language...", 30
            )
        )
        language = await detect_language(user_id, message, credits)

        scope = {
            "type": "selected_column",
            "tracker_data": tracker_data,
            "selected_column": selected_column,
        }
        result = await _dispatch_intents(
            intents=intents,
            user_id=user_id,
            tracker_id=tracker_id,
            message=message,
            language=language,
            scope=scope,
            credits=credits,
            job_id=job_id,
            session_id=session_id,
            emit=emit,
        )

        if "error" not in result:
            intent = result.get("intent", "unknown")
            logger.info(
                f"Job {job_id} completed: intent={intent}, pending_confirmation={result.get('pending_confirmation')}"
            )
            msg_text = (
                "Changes ready. Please confirm to save."
                if result.get("pending_confirmation")
                else result.get("response", "Done.")
            )
            await emit(msg_builder.job_success(job_id, session_id, msg_text))
        else:
            error = result.get("error", "unknown")
            logger.error(f"Job {job_id} FAILED: {error} - {result.get('message', '')}")
            await emit(
                msg_builder.job_error(
                    job_id, session_id, result.get("message", "An error occurred")
                )
            )

        return result

    except Exception as e:
        logger.exception("selected_column_worker error: %s", e)
        await emit(
            msg_builder.job_error(job_id, session_id, f"An error occurred: {str(e)}")
        )
        raise
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


async def _selected_rows_worker(data, job_id=None, session_id=None):
    """Background worker for selected_rows_tracker_change."""
    user_id = data.get("user_id")
    tracker_id = data.get("tracker_id")
    message = data.get("message")
    tracker_data = data.get("tracker_data")
    selected_rows = data.get("selected_rows", [])

    logger.info(
        f"_selected_rows_worker: job_id={job_id}, user={user_id}, tracker={tracker_id}, num_rows={len(selected_rows)}"
    )

    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            await send(ws_sender, msg, user_id)

    conn = None
    try:
        conn = connect_to_rds()
        credits = Credits(conn)

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "init", "Starting tracker AI...", 5
            )
        )

        if not all([user_id, tracker_id, message, tracker_data]):
            await emit(
                msg_builder.job_error(job_id, session_id, "Missing required fields")
            )
            return {"error": "missing_fields"}

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "detecting_intent", "Analyzing your request...", 15
            )
        )
        intents = await get_intents(user_id, message, credits, tracker_data)
        logger.info(f"Intents detected: {[i.get('type') for i in intents]}")
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "intent_detected",
                ", ".join(
                    INTENT_MESSAGES.get(i.get("type", ""), i.get("type", ""))
                    for i in intents
                ),
                25,
            )
        )

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "language_detection", "Detecting language...", 30
            )
        )
        language = await detect_language(user_id, message, credits)

        scope = {
            "type": "selected_rows",
            "tracker_data": tracker_data,
            "selected_rows": selected_rows,
        }
        result = await _dispatch_intents(
            intents=intents,
            user_id=user_id,
            tracker_id=tracker_id,
            message=message,
            language=language,
            scope=scope,
            credits=credits,
            job_id=job_id,
            session_id=session_id,
            emit=emit,
        )

        if "error" not in result:
            intent = result.get("intent", "unknown")
            logger.info(
                f"Job {job_id} completed: intent={intent}, pending_confirmation={result.get('pending_confirmation')}"
            )
            msg_text = (
                "Changes ready. Please confirm to save."
                if result.get("pending_confirmation")
                else result.get("response", "Done.")
            )
            await emit(msg_builder.job_success(job_id, session_id, msg_text))
        else:
            error = result.get("error", "unknown")
            logger.error(f"Job {job_id} FAILED: {error} - {result.get('message', '')}")
            await emit(
                msg_builder.job_error(
                    job_id, session_id, result.get("message", "An error occurred")
                )
            )

        return result

    except Exception as e:
        logger.exception("selected_rows_worker error: %s", e)
        await emit(
            msg_builder.job_error(job_id, session_id, f"An error occurred: {str(e)}")
        )
        raise
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


async def _selected_columns_worker(data, job_id=None, session_id=None):
    """Background worker for selected_columns_tracker_change."""
    user_id = data.get("user_id")
    tracker_id = data.get("tracker_id")
    message = data.get("message")
    tracker_data = data.get("tracker_data")
    selected_columns = data.get("selected_columns", [])

    logger.info(
        f"_selected_columns_worker: job_id={job_id}, user={user_id}, tracker={tracker_id}, num_cols={len(selected_columns)}"
    )

    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            await send(ws_sender, msg, user_id)

    conn = None
    try:
        conn = connect_to_rds()
        credits = Credits(conn)

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "init", "Starting tracker AI...", 5
            )
        )

        if not all([user_id, tracker_id, message, tracker_data]):
            await emit(
                msg_builder.job_error(job_id, session_id, "Missing required fields")
            )
            return {"error": "missing_fields"}

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "detecting_intent", "Analyzing your request...", 15
            )
        )
        intents = await get_intents(user_id, message, credits, tracker_data)
        logger.info(f"Intents detected: {[i.get('type') for i in intents]}")
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "intent_detected",
                ", ".join(
                    INTENT_MESSAGES.get(i.get("type", ""), i.get("type", ""))
                    for i in intents
                ),
                25,
            )
        )

        await emit(
            msg_builder.job_progress(
                job_id, session_id, "language_detection", "Detecting language...", 30
            )
        )
        language = await detect_language(user_id, message, credits)

        scope = {
            "type": "selected_columns",
            "tracker_data": tracker_data,
            "selected_columns": selected_columns,
        }
        result = await _dispatch_intents(
            intents=intents,
            user_id=user_id,
            tracker_id=tracker_id,
            message=message,
            language=language,
            scope=scope,
            credits=credits,
            job_id=job_id,
            session_id=session_id,
            emit=emit,
        )

        if "error" not in result:
            intent = result.get("intent", "unknown")
            logger.info(
                f"Job {job_id} completed: intent={intent}, pending_confirmation={result.get('pending_confirmation')}"
            )
            msg_text = (
                "Changes ready. Please confirm to save."
                if result.get("pending_confirmation")
                else result.get("response", "Done.")
            )
            await emit(msg_builder.job_success(job_id, session_id, msg_text))
        else:
            error = result.get("error", "unknown")
            logger.error(f"Job {job_id} FAILED: {error} - {result.get('message', '')}")
            await emit(
                msg_builder.job_error(
                    job_id, session_id, result.get("message", "An error occurred")
                )
            )

        return result

    except Exception as e:
        logger.exception("selected_columns_worker error: %s", e)
        await emit(
            msg_builder.job_error(job_id, session_id, f"An error occurred: {str(e)}")
        )
        raise
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


@tracker_ai_bp.route("/tracker/ai/complete_tracker_change", methods=["POST"])
@permission_required_body("trackers.table.chat")
async def complete_tracker_change():
    try:
        data = request.json
        user_id = data.get("user_id")
        tracker_id = data.get("tracker_id")
        logger.info(
            f"POST /tracker/ai/complete_tracker_change: user={user_id}, tracker={tracker_id}"
        )
        if not user_id:
            return jsonify({"error": "user_id is required"}), 400
        session_id = data.get("session_id") or None
        job_id = await JobManager.submit_job(
            _complete_tracker_worker, data, session_id=session_id
        )
        return jsonify({"success": True, "job_id": job_id, "status": "queued"})
    except Exception as e:
        logger.exception("Error in complete_tracker_change: %s", e)
        return jsonify({"error": str(e)}), 500


@tracker_ai_bp.route("/tracker/ai/selected_tracker_change", methods=["POST"])
@permission_required_body("trackers.table.chat")
async def selected_tracker_change():
    try:
        data = request.json
        user_id = data.get("user_id")
        tracker_id = data.get("tracker_id")
        logger.info(
            f"POST /tracker/ai/selected_tracker_change: user={user_id}, tracker={tracker_id}"
        )
        if not user_id:
            return jsonify({"error": "user_id is required"}), 400
        session_id = data.get("session_id") or None
        job_id = await JobManager.submit_job(
            _selected_element_worker, data, session_id=session_id
        )
        return jsonify({"success": True, "job_id": job_id, "status": "queued"})
    except Exception as e:
        logger.exception("Error in selected_tracker_change: %s", e)
        return jsonify({"error": str(e)}), 500


@tracker_ai_bp.route("/tracker/ai/selected_row_tracker_change", methods=["POST"])
@permission_required_body("trackers.table.chat")
async def selected_row_tracker_change():
    try:
        data = request.json
        user_id = data.get("user_id")
        logger.info(f"POST /tracker/ai/selected_row_tracker_change: user={user_id}")
        if not user_id:
            return jsonify({"error": "user_id is required"}), 400
        session_id = data.get("session_id") or None
        job_id = await JobManager.submit_job(
            _selected_row_worker, data, session_id=session_id
        )
        return jsonify({"success": True, "job_id": job_id, "status": "queued"})
    except Exception as e:
        logger.exception("Error in selected_row_tracker_change: %s", e)
        return jsonify({"error": str(e)}), 500


@tracker_ai_bp.route("/tracker/ai/selected_column_tracker_change", methods=["POST"])
@permission_required_body("trackers.table.chat")
async def selected_column_tracker_change():
    try:
        data = request.json
        user_id = data.get("user_id")
        logger.info(f"POST /tracker/ai/selected_column_tracker_change: user={user_id}")
        if not user_id:
            return jsonify({"error": "user_id is required"}), 400
        session_id = data.get("session_id") or None
        job_id = await JobManager.submit_job(
            _selected_column_worker, data, session_id=session_id
        )
        return jsonify({"success": True, "job_id": job_id, "status": "queued"})
    except Exception as e:
        logger.exception("Error in selected_column_tracker_change: %s", e)
        return jsonify({"error": str(e)}), 500


@tracker_ai_bp.route("/tracker/ai/selected_rows_tracker_change", methods=["POST"])
@permission_required_body("trackers.table.chat")
async def selected_rows_tracker_change():
    try:
        data = request.json
        user_id = data.get("user_id")
        logger.info(f"POST /tracker/ai/selected_rows_tracker_change: user={user_id}")
        if not user_id:
            return jsonify({"error": "user_id is required"}), 400
        session_id = data.get("session_id") or None
        job_id = await JobManager.submit_job(
            _selected_rows_worker, data, session_id=session_id
        )
        return jsonify({"success": True, "job_id": job_id, "status": "queued"})
    except Exception as e:
        logger.exception("Error in selected_rows_tracker_change: %s", e)
        return jsonify({"error": str(e)}), 500


@tracker_ai_bp.route("/tracker/ai/selected_columns_tracker_change", methods=["POST"])
@permission_required_body("trackers.table.chat")
async def selected_columns_tracker_change():
    try:
        data = request.json
        user_id = data.get("user_id")
        logger.info(f"POST /tracker/ai/selected_columns_tracker_change: user={user_id}")
        if not user_id:
            return jsonify({"error": "user_id is required"}), 400
        session_id = data.get("session_id") or None
        job_id = await JobManager.submit_job(
            _selected_columns_worker, data, session_id=session_id
        )
        return jsonify({"success": True, "job_id": job_id, "status": "queued"})
    except Exception as e:
        logger.exception("Error in selected_columns_tracker_change: %s", e)
        return jsonify({"error": str(e)}), 500


@tracker_ai_bp.route("/tracker/ai/save_tracker_change", methods=["POST"])
@permission_required_body("trackers.table.chat")
async def save_tracker_change():
    """
    Persist confirmed AI-modified tracker data to S3.

    Call this after the user confirms the proposed changes from any
    *_tracker_change endpoint. Accepts the tracker_data returned by
    the AI job and writes it to S3.

    Body:
      user_id     string (required)
      tracker_id  string (required)
      tracker_data object (required) — the confirmed modified tracker JSON

    Returns:
      { "success": true, "tracker_id": "..." }
    """
    try:
        data = request.json
        user_id = data.get("user_id")
        tracker_id = data.get("tracker_id")
        tracker_data = data.get("tracker_data")

        logger.info(
            f"POST /tracker/ai/save_tracker_change: user={user_id}, tracker={tracker_id}"
        )

        if not user_id:
            return jsonify({"error": "user_id is required"}), 400
        if not tracker_id:
            return jsonify({"error": "tracker_id is required"}), 400
        if not tracker_data or not isinstance(tracker_data, dict):
            return (
                jsonify({"error": "tracker_data is required and must be an object"}),
                400,
            )

        await asyncio.to_thread(save_tracker_file, user_id, tracker_id, tracker_data)
        logger.info(f"Tracker saved successfully: user={user_id}, tracker={tracker_id}")

        return jsonify({"success": True, "tracker_id": tracker_id})
    except Exception as e:
        logger.exception("Error in save_tracker_change: %s", e)
        return jsonify({"error": str(e)}), 500
