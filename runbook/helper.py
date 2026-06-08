import asyncio
import json
import os
import time
import traceback
from urllib.parse import parse_qs, unquote, urlparse
import uuid
from agent_route.doc_clarity import QueryData
import boto3
import hashlib
from config_evidences.evidence_helpers import _get_user_evidence
from credits_route.route import Credits
from db.lance_db_service import LanceDBServer
from db.rds_db import connect_to_rds

# from services.scheduler_service import SchedulerService
from utils.img_tokens import image_credit_cost
from utils.normal import load_yaml_file
from utils.s3_utils import read_json_from_s3, upload_any_file, s3bucket, S3_BUCKET, load_yaml_from_s3
from tab_tracker.helper import (
    check_config_exist,
    append_to_tracker,
    save_tracker_file,
    update_tracker_config,
)
from utils.fireworkzz import (
    get_firework_embedding,
    get_fireworks_response2,
    get_think_bedrok_response,
    get_think_fire_response2_og,
    get_think_fire_response2_og2,
    get_extract_response,
    get_think_bedrock_vision_image,
    analyze_tracker_framework_rows,
    quality_review_framework_assignments,
)
from utils.normal import load_yaml_file
from radar.radar_helpers import (
    process_file_payloads,
    extract_files_content,
    IMAGE_EXTENSIONS,
)

from cust_helpers import pathconfig
from utils.base_logger import get_logger
from utils.app_configs import IS_DEV, FRAMEWORK_OWNER
from .utils import *
from .utils import _safe_json_parse
from .utils import _safe_json_parse_full
from .risk_engine import (
    get_risk_config,
    compute_risk,
    apply_risk_overrides,
    prior_risks_for_prompt,
    risk_analysis_disabled,
)
from utils.scheduler import scheduler
from apscheduler.triggers.cron import CronTrigger

dbserver = LanceDBServer()
conn = connect_to_rds()
credits = Credits(conn)


RUNBOOK_TEMPLATE = load_yaml_file(path=pathconfig.runbook_prompts)
RADAR_TEMPLATE = load_yaml_file(path=pathconfig.radar_prompts)
logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")


def schedule_runbook_log(runbook):

    cron_expr = runbook.get("schedule")

    if not cron_expr:
        return
    if cron_expr == "1m":
        cron_expr = "*/1 * * * *"
    elif cron_expr == "2m":
        cron_expr = "*/2 * * * *"
    elif cron_expr == "5m":
        cron_expr = "*/5 * * * *"
    elif cron_expr == "10m":
        cron_expr = "*/10 * * * *"
    elif cron_expr == "15m":
        cron_expr = "*/15 * * * *"
    elif cron_expr == "1h":
        cron_expr = "0 * * * *"
    elif cron_expr == "daily":
        cron_expr = "0 0 * * *"

    trigger = CronTrigger.from_crontab(cron_expr)

    # ✅ FIX: wrap async properly
    scheduler.add_job(
        run_runbook_job_wrapper,
        trigger=trigger,
        id=runbook["runbook_id"],
        args=[runbook],
        replace_existing=True,
    )

    logger.info("Scheduled runbook: %s", runbook["runbook_id"])


async def run_runbook_job(runbook):
    #  print(f"🔥 JOB TRIGGERED: {runbook['runbook_id']}")

    try:
        logger.info("Running runbook: %s", runbook["runbook_id"])
        dbserver = LanceDBServer()
        await run_runbook_execution_engine(
            dbserver=dbserver,
            user_id=runbook["user_id"],
            runbook=runbook,
        )
        logger.info("Runbook completed: %s", runbook["runbook_id"])
    except Exception as e:
        logger.debug("Full traceback: %s", traceback.format_exc())
        logger.error("Runbook failed: %s", e, exc_info=IS_DEV)


def run_runbook_job_wrapper(runbook):
    # print("🚀 WRAPPER TRIGGERED")
    asyncio.run(run_runbook_job(runbook))


ASSESSORS = """
For Risk Assessors (Control Effectiveness Across Security, Privacy, AI)
- Evidence maps directly to the control objective (security/privacy/AI governance)
- Evidence demonstrates actual control operation, not just documented intent
- Evidence aligns with security baselines, privacy obligations, and AI governance policies
- Evidence reflects data classification and sensitivity (PII, PHI, confidential, AI inputs/outputs)
- Evidence shows how personal data is collected, processed, stored, and shared
- Evidence demonstrates lawful basis and purpose limitation for data processing
- Evidence supports data minimization principles (only required data used)
- Evidence reflects data retention and deletion controls
- Evidence shows user consent capture and management (where applicable)
- Evidence demonstrates data subject rights handling (access, deletion, correction)
- Evidence reflects cross-border data transfer controls and restrictions
- Evidence demonstrates third-party/vendor risk handling (including AI providers)
- Evidence shows integration points between systems and data flows
- Evidence reflects AI model usage context and decision boundaries
- Evidence demonstrates human-in-the-loop or oversight mechanisms for AI decisions
- Evidence supports bias, fairness, and ethical AI considerations
- Evidence reflects risk mitigation effectiveness across security, privacy, and AI domains
- Evidence shows control ownership and accountability
- Evidence demonstrates frequency and consistency of control execution
- Evidence includes exceptions, deviations, and risk acceptance decisions
- Evidence supports residual risk determination
- Evidence aligns with framework mappings (ISO 27001, NIST, SOC 2, GDPR, AI frameworks)
"""

AUDITORS = """
For Auditors (Assurance, Compliance, Defensibility)

- Evidence is relevant to security, privacy, and AI control objectives
- Evidence is sufficient to support an audit conclusion across domains
- Evidence is reliable (system-generated, authoritative sources preferred)
- Evidence is accurate and internally consistent across artifacts
- Evidence is complete with no material omissions in scope or population
- Evidence is time-bound and within the audit/review period
- Evidence is authentic and free from tampering or manipulation
- Evidence is traceable to systems, users, and transactions
- Evidence supports re-performance or independent validation
- Evidence aligns with policies, procedures, and regulatory requirements
- Evidence demonstrates actual execution of controls (not theoretical compliance)
- Evidence is consistent across multiple samples, systems, and timeframes
- Evidence does not contradict other security, privacy, or AI artifacts
- Evidence includes identifiers (logs, IDs, model versions, dataset references)
- Evidence reflects independence (not solely self-attested by control owner)
- Evidence demonstrates review and approval workflows (including AI outputs where applicable)
- Evidence supports segregation of duties and access restrictions
- Evidence includes exception handling, incident logs, and remediation tracking
- Evidence supports data protection obligations (GDPR, HIPAA, etc.)
- Evidence supports AI governance requirements (transparency, explainability where required)
- Evidence is clear, interpretable, and defensible to external stakeholders/regulators
- Evidence is retained, versioned, and auditable over time
- Evidence supports a defensible audit opinion across security, privacy, and AI domains
"""

REVIEWER = """
For Security / Privacy / AI Reviewers (Technical & Governance Validation)

- Evidence originates from authoritative sources (logs, configs, AI system outputs, data lineage tools)
- Evidence reflects actual system configuration and runtime behavior
- Evidence includes raw logs, configuration outputs, or system-generated reports where possible
- Evidence validates access controls, authentication, and authorization mechanisms
- Evidence demonstrates least privilege and role-based access enforcement
- Evidence confirms encryption at rest and in transit for sensitive data
- Evidence validates secure key management practices (e.g., KMS, CMK)
- Evidence reflects data masking, anonymization, or pseudonymization where required
- Evidence shows PII/PHI is not unnecessarily exposed in logs, prompts, or outputs
- Evidence demonstrates secure data handling in AI pipelines (input → processing → output)
- Evidence validates prompt handling and protection against prompt injection or leakage
- Evidence confirms AI outputs are controlled, filtered, or moderated where required
- Evidence demonstrates model versioning, traceability, and reproducibility
- Evidence reflects training data governance (source, quality, bias considerations)
- Evidence validates monitoring of AI behavior (drift, anomalies, misuse)
- Evidence demonstrates alerting and incident response for security/privacy/AI events
- Evidence reflects logging completeness and integrity (no gaps or tampering)
- Evidence confirms segregation of environments (dev/test/prod, training vs inference)
- Evidence validates secure API integrations and data exchange points
- Evidence demonstrates vulnerability management and remediation for systems and AI components
- Evidence reflects resilience controls (backup, recovery, failover)
- Evidence confirms third-party AI/vendor controls and data handling assurances
- Evidence supports compliance with security benchmarks and privacy regulations
- Evidence is sufficient to technically validate effectiveness of controls across all domains
"""


EVIDENCE_ANALYSIS_PROMPT = """
You are a senior compliance and security evidence analyst.

Your task is to perform a deep, audit-level evaluation of the provided document.

--------------------------------------------------
INPUTS
--------------------------------------------------
EVIDENCE TYPES (select EXACTLY ONE best match):
{{evidence_types}}

FILE NAME:
{{filename}}

FILE CONTENT:
{{file_data}}

REPORT VIEWER TYPE:
{{viewer_type}}

REVIEW CRITERIA FOR {{viewer_type}}:
{{viewer_criteria}}

--------------------------------------------------
CORE INSTRUCTIONS
--------------------------------------------------
1. You MUST select exactly ONE evidence_type from the provided list.
2. Your decision MUST be strictly based on the document content.
3. Do NOT assume missing data — explicitly state absence.
4. Evaluate ALL viewer criteria.
5. Every statement must be justified with evidence or clearly marked as missing.

--------------------------------------------------
SUMMARY REQUIREMENTS
--------------------------------------------------
- "summary" MUST be a SINGLE HTML string.
- Must be long, detailed, and dynamically structured.
- Use multiple sections with dynamic headings.

--------------------------------------------------
ANALYSIS REQUIREMENTS (CRITICAL)
--------------------------------------------------
- "analysis" MUST be a STRUCTURED OBJECT.
- Each field inside analysis MUST contain HTML content.

ALLOWED HTML TAGS:
<h3>, <p>, <ul>, <ol>, <li>, <b>, <strong>, <i>, <br>

STRICT RULES:
- NO markdown
- NO inline styles/scripts
- Proper HTML formatting

--------------------------------------------------
OUTPUT FORMAT (STRICT JSON ONLY)
--------------------------------------------------
{
  "evidence_type": "<exact match from list>",

  "summary": "<FULL HTML STRING WITH MULTIPLE SECTIONS>",

  "analysis": {
    "criteria_evaluation": [
      {
        "criterion": "<criterion from viewer_criteria>",
        "status": "<present | partially_present | not_present>",
        "is_valid": "<true | false>",
        "details_html": "<HTML explaining if it is present, why valid/invalid, and supporting evidence or absence>"
      }
    ],

    "detailed_assessment_html": "<HTML with deep explanation of what the document demonstrates>",

    "gaps_and_risks_html": "<HTML describing missing elements and associated risks>",

    "evidence_type_justification_html": "<HTML explaining why this evidence type was selected>",

    "rejection_reasoning_html": "<HTML explaining why other types were not selected>",

    "audit_readiness_html": "<HTML explaining whether the document is audit-ready and why>"
  }
}
"""


async def run_evidence_analysis(data_checked, report_viewer, user_id, credits):
    from collections import defaultdict

    viewer_map = {
        "Assessor": ASSESSORS,
        "Auditor": AUDITORS,
        "Reviewer": REVIEWER,
    }
    viewer_data = viewer_map.get(report_viewer, ASSESSORS)

    files_map = defaultdict(list)
    for item in data_checked:
        source = (
            item.get("source")
            or item.get("endpoint_id")
            or item.get("note_id")
            or item.get("url")
            or "unknown"
        )
        files_map[source].append(str(item.get("data", "")))

    results = []
    EVIDENCES_TYPES, _ = _get_user_evidence(user_id)
    if isinstance(EVIDENCES_TYPES, list):
        EVIDENCES_TYPES = "\n".join(
            f"- {e.get('artifact', '')}: {e.get('expectations', '')}"
            for e in EVIDENCES_TYPES
        )
    elif not isinstance(EVIDENCES_TYPES, str):
        EVIDENCES_TYPES = json.dumps(EVIDENCES_TYPES)

    for filename, chunks in files_map.items():
        display_name = os.path.basename(filename)
        combined_data = "\n\n".join(chunks)[:20000]
        prompt = (
            EVIDENCE_ANALYSIS_PROMPT.replace("{{evidence_types}}", EVIDENCES_TYPES)
            .replace("{{filename}}", display_name)
            .replace("{{file_data}}", combined_data)
            .replace("{{viewer_type}}", report_viewer or "Assessor")
            .replace("{{viewer_criteria}}", viewer_data)
        )
        raw = await get_think_fire_response2_og2(
            user_message=prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=len(prompt),
        )
        parsed = _safe_json_parse(raw)
        if parsed:
            parsed["filename"] = display_name
            results.append(parsed)

    return results


async def reduce_data_for_report(
    data_items, structure_file_payload, user_id, credits, label="evidence"
):
    if not data_items:
        return json.dumps(data_items)

    structure_str = json.dumps(structure_file_payload)
    prompt_template = (
        f"You are a document intelligence extractor.\n\n"
        f"Given raw {label} data and a report structure, extract ONLY the information relevant to the report blocks.\n"
        f"Preserve ALL specific facts, values, metrics, configurations, dates, names, and technical details.\n"
        f"Eliminate repeated or duplicate content. Be thorough — losing key details degrades report quality.\n\n"
        f"REPORT STRUCTURE (what the report covers):\n{structure_str}\n\n"
        f"{label.upper()} DATA:\n{{{{data}}}}\n\n"
        f'OUTPUT (strict JSON only, no markdown):\n{{"extracted_content": "<dense extraction preserving all specific facts relevant to the report structure>"}}'
    )

    extracted = await get_extract_response(
        prompt_template=prompt_template,
        data=json.dumps(data_items),
        user_id=user_id,
        credits=credits,
    )
    return extracted if extracted else json.dumps(data_items)


async def _reduce_by_admissible_evidence(
    data_checked, admissible_evidence, user_id, credits
):
    """
    For playbook-based executions: extract from data_checked only the information
    that satisfies each admissible artifact's expectations (from the user evidence config).
    Unmatched items are dropped — only admissible content reaches the report.
    """
    if not data_checked or not admissible_evidence:
        return json.dumps(data_checked)

    # artifact → [expectation, ...] from the user evidence config
    user_ev_list, _ = _get_user_evidence(user_id)
    artifact_expectations = {
        entry.get("artifact", ""): [
            e.strip() for e in entry.get("expectations", "").split(";") if e.strip()
        ]
        for entry in (user_ev_list if isinstance(user_ev_list, list) else [])
    }

    # filename → artifact from the admissible_evidence list
    filename_to_artifact = {}
    for ev in admissible_evidence:
        artifact = ev.get("artifact", "")
        for fname in ev.get("files", []):
            filename_to_artifact[os.path.basename(fname)] = artifact
            filename_to_artifact[fname] = artifact

    # Group data_checked items by their admissible artifact
    artifact_groups = {}
    for item in data_checked:
        source = item.get("source", "")
        artifact = filename_to_artifact.get(
            os.path.basename(source)
        ) or filename_to_artifact.get(source)
        if artifact:
            artifact_groups.setdefault(artifact, []).append(str(item.get("data", "")))

    extracted_parts = []
    for artifact, chunks in artifact_groups.items():
        expectations = artifact_expectations.get(artifact, [])
        combined_data = "\n\n".join(chunks)[:15000]
        expectations_str = (
            "; ".join(expectations) if expectations else "general compliance criteria"
        )

        prompt = (
            f"ARTIFACT: {artifact}\n"
            f"EXPECTATIONS: {expectations_str}\n\n"
            f"DATA:\n{combined_data}\n\n"
            "Extract ONLY the information from the data that is relevant to satisfying "
            "the above expectations. Include specific facts, values, dates, and references. "
            "Discard anything not related to these expectations.\n"
            f'Return ONLY JSON: {{"artifact": "{artifact}", "extracted": "..."}}'
        )
        try:
            raw = await get_fireworks_response2(
                user_message=prompt,
                role="user",
                temp=0.0,
                user_id=user_id,
                credits=credits,
            )
            parsed = json.loads(raw) if raw else {}
            content = parsed.get("extracted") if isinstance(parsed, dict) else None
            extracted_parts.append(
                {
                    "artifact": artifact,
                    "extracted": content or combined_data[:3000],
                }
            )
        except Exception:
            extracted_parts.append(
                {
                    "artifact": artifact,
                    "extracted": combined_data[:3000],
                }
            )

    return json.dumps(extracted_parts)


async def _push_blocks_to_trackers(user_id, runbook, merged_result, new_result_id):
    try:
        tracker_cfg = runbook.get("tracker_configuration")
        if not tracker_cfg:
            return
        if isinstance(tracker_cfg, str):
            try:
                tracker_cfg = json.loads(tracker_cfg)
            except Exception:
                logger.warning(
                    "tracker_configuration is not valid JSON, skipping tracker push"
                )
                return
        if not isinstance(tracker_cfg, dict) or not tracker_cfg:
            return

        blocks = merged_result.get("blocks", [])
        block_map = {b["block_id"]: b for b in blocks if "block_id" in b}

        config_path, config_data = check_config_exist(user_id)
        if not config_data:
            logger.warning(
                "No tracker config found for user %s, skipping tracker push", user_id
            )
            return

        for block_id, tracker_id in tracker_cfg.items():
            try:
                block = block_map.get(block_id)
                if not block:
                    logger.warning(
                        "Block %s not in merged_result, skipping tracker %s",
                        block_id,
                        tracker_id,
                    )
                    continue

                tracker_meta = next(
                    (
                        t
                        for t in config_data.get("trackers", [])
                        if t["tracker_id"] == tracker_id
                    ),
                    None,
                )
                if not tracker_meta:
                    logger.warning(
                        "Tracker %s not in config, skipping block %s",
                        tracker_id,
                        block_id,
                    )
                    continue

                file_path = tracker_meta.get(
                    "file_path", f"{user_id}/tracker/{tracker_id}/tracker.json"
                )
                tracker_data = read_json_from_s3(file_path)
                if not tracker_data:
                    logger.warning(
                        "Tracker file missing at %s, skipping tracker %s",
                        file_path,
                        tracker_id,
                    )
                    continue

                before_row_count = len(tracker_data.get("rows", []))
                append_to_tracker(tracker_data, block, new_result_id)

                linked_frameworks = tracker_data.get("frameworks", [])
                if linked_frameworks:
                    schema_cols = tracker_data.get("schema", {}).get("columns", [])
                    new_rows = tracker_data.get("rows", [])[before_row_count:]
                    if new_rows:
                        fw_credits = Credits()
                        for fw_entry in linked_frameworks:
                            # Isolate each framework so an LLM failure (timeout,
                            # malformed JSON, etc.) doesn't bubble up to the
                            # outer except, which would skip save_tracker_file
                            # below and silently drop every row we just appended.
                            # The fresh row's framework column stays at its
                            # setdefault [] when AI mapping fails — preserving
                            # the row, just unmapped against this framework.
                            fw_name = fw_entry.get("name")
                            try:
                                fw_id = fw_entry.get("id")
                                fw_col = next(
                                    (
                                        col for col in schema_cols
                                        if col.get("source_column") == "frameworks"
                                        and col.get("name") == fw_name
                                    ),
                                    None,
                                )
                                if not fw_col:
                                    continue
                                fw_col_id = fw_col["id"]
                                for row in new_rows:
                                    row["values"].setdefault(fw_col_id, [])
                                fw_s3_key = f"{FRAMEWORK_OWNER}/frameworks/{fw_id}.yaml"
                                fw_data = load_yaml_from_s3(fw_s3_key)
                                if not fw_data:
                                    continue
                                fw_rows_data = fw_data.get("rows", [])
                                fw_cols = fw_data.get("columns", [])
                                req_col = fw_cols[0] if fw_cols else "REQUIREMENT/TASK"
                                sec_col = fw_cols[1] if len(fw_cols) > 1 else "SECTION/CATEGORY"
                                if not fw_rows_data:
                                    continue
                                rows_analysis_input = [
                                    {
                                        "row_id": row.get("row_id"),
                                        "col_values": {
                                            col.get("name"): row["values"].get(col.get("id"), "")
                                            for col in schema_cols
                                            if col.get("source_column") != "frameworks"
                                        },
                                    }
                                    for row in new_rows
                                ]
                                ai_result = await analyze_tracker_framework_rows(
                                    rows=rows_analysis_input,
                                    fw_rows=fw_rows_data,
                                    framework_id=fw_id,
                                    framework_name=fw_name,
                                    user_id=user_id,
                                    credits=fw_credits,
                                )
                                reviewed_assignments = await quality_review_framework_assignments(
                                    rows=rows_analysis_input,
                                    fw_rows=fw_rows_data,
                                    assignments=(ai_result or {}).get("assignments", []),
                                    framework_name=fw_name,
                                    user_id=user_id,
                                    credits=fw_credits,
                                )
                                for assignment in reviewed_assignments or []:
                                    row_id = assignment.get("row_id")
                                    fw_indices = assignment.get("fw_row_indices", [])
                                    if isinstance(fw_indices, int):
                                        fw_indices = [fw_indices] if fw_indices >= 0 else []
                                    matched_row = next(
                                        (r for r in new_rows if r.get("row_id") == row_id), None
                                    )
                                    if matched_row:
                                        matched_row["values"][fw_col_id] = [
                                            {
                                                "requirement": fw_rows_data[idx].get(req_col, ""),
                                                "section": fw_rows_data[idx].get(sec_col, ""),
                                            }
                                            for idx in fw_indices
                                            if 0 <= idx < len(fw_rows_data)
                                        ]
                            except Exception as fw_exc:
                                logger.warning(
                                    "Framework mapping failed for tracker=%s framework=%s — "
                                    "rows will be saved with this framework column empty: %s",
                                    tracker_id, fw_name, fw_exc, exc_info=IS_DEV,
                                )
                                continue

                try:
                    from tab_tracker.helper import propagate_assessment_status_to_policy_cells
                    propagate_assessment_status_to_policy_cells(tracker_data, new_result_id)
                except Exception as _pex:
                    logger.warning("propagate_assessment_status_to_policy_cells failed: %s", _pex)

                save_tracker_file(user_id, tracker_id, tracker_data)
                update_tracker_config(
                    config_path=config_path,
                    user_id=user_id,
                    tracker_id=tracker_id,
                    updates={"last_result_id": new_result_id},
                )
                logger.info(
                    "Pushed block %s → tracker %s (result=%s)",
                    block_id,
                    tracker_id,
                    new_result_id,
                )

            except Exception as e:
                logger.error(
                    "Failed to push block %s to tracker %s: %s",
                    block_id,
                    tracker_id,
                    e,
                    exc_info=IS_DEV,
                )

    except Exception as e:
        logger.error("_push_blocks_to_trackers error: %s", e, exc_info=IS_DEV)


def _auto_submit_runbook_workflow(runbook_id: str, owner_user_id: str, result_id: str | None = None) -> None:
    """Programmatically submit a freshly-generated runbook result for review.

    Mirrors POST /workflow/submit for the runbook doc_type, but only when
    the org is configured for role-based assignment. Per-document orgs
    (which require explicit per-runbook reviewer selection) are left in
    draft so the existing "Send for review" UI flow still works.

    The workflow is registered against ``result_id`` (each generated report
    is reviewed independently). ``runbook_id`` is retained only for log
    correlation and the early-exit when no result was produced.

    Idempotent: if a workflow row already exists in a non-draft state,
    skips entirely; if it exists in draft, transitions it forward.
    Any failure here is logged and swallowed — runbook generation has
    already succeeded and must not be rolled back.
    """
    if not result_id:
        # Older callers passed only the runbook_id. Without a result_id the
        # workflow would key against the wrong identifier (the runbook
        # definition, not the specific report) and the UI's by-doc lookup
        # would never find it. Skip rather than create a phantom workflow.
        logger.warning(
            "auto_submit: skipping runbook=%s — no result_id provided", runbook_id,
        )
        return

    try:
        from workflow_route.state_machine import (
            RoleResolutionError,
            create_workflow,
            get_user_org_id,
            get_workflow_config,
            get_workflow_for_doc,
            pick_user_for_role,
            transition,
        )
    except Exception as exc:
        logger.warning("auto_submit: import failed for runbook=%s: %s", runbook_id, exc)
        return

    try:
        org_id = get_user_org_id(owner_user_id)
        if not org_id:
            logger.info(
                "auto_submit: skipping runbook=%s result=%s — owner=%s has no resolvable org",
                runbook_id, result_id, owner_user_id,
            )
            return

        config = get_workflow_config(org_id, "runbook")
        if config.get("assignment_mode") != "role_based":
            logger.debug(
                "auto_submit: skipping runbook=%s result=%s — org=%s is per_document mode",
                runbook_id, result_id, org_id,
            )
            return
        if not config.get("reviewer_role_id") and not config.get("approver_role_id"):
            logger.debug(
                "auto_submit: skipping runbook=%s result=%s — org=%s has no role IDs configured",
                runbook_id, result_id, org_id,
            )
            return

        # Resolve each role slot independently; leave a slot empty on
        # RoleResolutionError so the owner can act as fallback per
        # workflow_route.routes.review_document.
        def _resolve(role_id):
            if not role_id:
                return None
            try:
                uid, _ = pick_user_for_role(role_id, owner_user_id)
                return uid
            except RoleResolutionError as rre:
                logger.warning(
                    "auto_submit: role %s has no eligible user (runbook=%s result=%s): %s",
                    role_id, runbook_id, result_id, rre,
                )
                return None

        quality_reviewer_user_id = _resolve(config.get("reviewer_role_id"))
        # workflow_config currently only carries reviewer_role_id and
        # approver_role_id; governance reviewer reuses the quality slot
        # until a dedicated column is added.
        governance_reviewer_user_id = quality_reviewer_user_id
        approver_user_id = _resolve(config.get("approver_role_id"))

        doc_version = "1.0"
        # Each generated report is its own review unit; key the workflow by
        # result_id so the UI's by-doc lookup (which uses result_id) finds it.
        existing = get_workflow_for_doc("runbook", result_id, doc_version)
        if existing and existing.get("state") != "draft":
            logger.info(
                "auto_submit: skipping runbook=%s result=%s — workflow %s already in state=%s",
                runbook_id, result_id, existing.get("workflow_id"), existing.get("state"),
            )
            return

        if existing:
            wf = transition(
                existing["workflow_id"],
                existing["state_version"],
                "quality_review",
                owner_user_id,
                quality_reviewer_user_id=quality_reviewer_user_id,
                governance_reviewer_user_id=governance_reviewer_user_id,
                approver_user_id=approver_user_id,
            )
        else:
            wf = create_workflow(
                org_id=org_id,
                doc_type="runbook",
                doc_id=result_id,
                doc_version=doc_version,
                owner_user_id=owner_user_id,
                quality_reviewer_user_id=quality_reviewer_user_id,
                governance_reviewer_user_id=governance_reviewer_user_id,
                approver_user_id=approver_user_id,
            )
            wf = transition(
                wf["workflow_id"], 1, "quality_review", owner_user_id,
                quality_reviewer_user_id=quality_reviewer_user_id,
                governance_reviewer_user_id=governance_reviewer_user_id,
                approver_user_id=approver_user_id,
            )

        logger.info(
            "auto_submit: runbook=%s result=%s workflow=%s submitted to quality_review (QR=%s, GR=%s, AP=%s)",
            runbook_id, result_id, wf.get("workflow_id"),
            quality_reviewer_user_id, governance_reviewer_user_id, approver_user_id,
        )

        try:
            from services.workflow_notifications_service import notify_workflow_event
            notify_workflow_event(wf, "WORKFLOW_SUBMITTED")
        except Exception as notify_exc:
            logger.warning(
                "auto_submit: notification failed for runbook=%s workflow=%s: %s",
                runbook_id, wf.get("workflow_id"), notify_exc,
            )

    except Exception as exc:
        logger.warning(
            "auto_submit: failed for runbook=%s owner=%s (runbook result already saved): %s",
            runbook_id, owner_user_id, exc, exc_info=IS_DEV,
        )


async def run_runbook_execution_engine(
    user_id,
    runbook,
    dbserver=LanceDBServer(),
    structure_file_payload=None,
    files=None,
    structure_file=None,
    result_id=None,
    is_prev_needed=False,
    document_data=None,
    job_id=None,
    session_id=None,
    progress=None,
    is_playbook_based_execution=False,
    custom_playbook_id=None,
):
    from websockets_custom.ws_instance import ws_service, msg_builder_main
    from runbook.utils import send

    msg_builder = msg_builder_main

    # ✅ single flag
    should_emit = bool(job_id and session_id)

    async def emit(msg):
        if should_emit:
            await send(ws_service, msg, user_id)

    conn = connect_to_rds()
    credits = Credits(conn)
    runbook_id = runbook["runbook_id"]
    if not is_playbook_based_execution:
        if "playbook_id" in runbook and runbook["playbook_id"]:
            is_playbook_based_execution = True

    main_source = runbook["main_source"] if "main_source" in runbook else None
    data_sources = runbook["data_sources"] if "data_sources" in runbook else None
    reference_sources = (
        runbook["reference_sources"] if "reference_sources" in runbook else None
    )
    refernce_main_source = (
        runbook.get("refernce_main_source") or runbook.get("reference_main_source")
    )
    logger.debug("Data sources: %s", data_sources)
    logger.debug("Reference sources: %s", refernce_main_source)
    if structure_file:
        structure_file_content = read_json_from_s3(structure_file)
    else:
        structure_file_content = None

    if data_sources and len(data_sources) > 5:
        data_sources = normalize_json_field(data_sources)
    if reference_sources and len(reference_sources) > 5:
        reference_sources = normalize_json_field(reference_sources)

    if not structure_file_payload:
        raw_structure = runbook.get("structure_theme")

        if isinstance(raw_structure, str):
            try:
                structure_file_payload = json.loads(raw_structure)
            except Exception:
                raise ValueError("Invalid JSON in structure_theme")

        elif isinstance(raw_structure, dict):
            structure_file_payload = raw_structure

        else:
            raise ValueError("structure_theme must be str or dict")
    progress = 35

    await emit(
        msg_builder.job_progress(
            job_id,
            session_id,
            "report setup",
            "started creating report",
            progress,
        )
    )

    execution_id = f"exec_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    new_result_id = f"result_{uuid.uuid4().hex[:6]}"
    started_at = int(time.time())
    risk_score = None
    refactor_result = {}
    await dbserver.insert_runbook_result(
        {
            "execution_id": execution_id,
            "result_id": new_result_id,
            "runbook_id": runbook_id,
            "user_id": user_id,
            "status": "running",
            "started_at": started_at,
            "input_mode": runbook.get("input_type"),
        }
    )
    # --------------------------------------------------
    # RENDER RUNBOOK TEMPLATE
    # --------------------------------------------------

    runbook_yaml = render_runbook_yaml(runbook)

    # --------------------------------------------------
    # RESOLVE RUNBOOK INPUT
    # --------------------------------------------------
    logger.debug("Entered structure section 2")
    try:
        analyze_input = ""
        if "analyze_input" in runbook and runbook["analyze_input"]:
            analyze_input = runbook["analyze_input"]
        else:
            analyze_input = runbook["description"] if "description" in runbook else ""

        file_data = ""
        if not result_id and not document_data:
            file_data = await collect_runbook_inputs(runbook)

        user_analyze_input = analyze_input

        # --------------------------------------------------
        # LANGUAGE + WORD COUNT (same radar logic)
        # --------------------------------------------------
        output_language = "English"
        output_word_count = 500
        if user_analyze_input:

            lang_prompt_key = runbook_yaml["radar"]["language_prompt"]

            lang_prompt = RADAR_TEMPLATE[lang_prompt_key]

            lang_prompt = lang_prompt.replace(
                "{{analyze_input}}", str(user_analyze_input or "")
            )

            # print("lang_prompt: ",lang_prompt)

            result = await get_think_fire_response2_og(
                user_message=lang_prompt,
                user_id=user_id,
                credits=credits,
                total_input_chars=len(lang_prompt),
            )
            # The LLM occasionally returns empty / non-JSON / fenced-code
            # responses. Fall back to defaults instead of failing the whole
            # report — English / 800 words are the engine-wide defaults below.
            lang_data: dict = {}
            if result:
                cleaned = result.strip()
                if cleaned.startswith("```"):
                    cleaned = cleaned.strip("`")
                    if cleaned.lower().startswith("json"):
                        cleaned = cleaned[4:].lstrip()
                try:
                    parsed = json.loads(cleaned)
                    if isinstance(parsed, dict):
                        lang_data = parsed
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Language-detection LLM returned unparseable response; "
                        "defaulting to English/800. raw=%r",
                        result[:200] if isinstance(result, str) else result,
                    )

            output_language = lang_data.get("language", "English")
            output_word_count = lang_data.get("word_count") or 800

            progress = 40
            re_msg = f"generating report in {output_language}"

            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "runbook setup",
                    re_msg,
                    progress,
                )
            )
        # ---------------------------------
        # EMBEDDING GENERATION
        # ---------------------------------
        payload = None
        if main_source == "knowledge" or refernce_main_source == "knowledge":

            embedding = await get_firework_embedding()

            vector = embedding.embed_query(user_analyze_input)

            payload = QueryData(
                user_id=user_id,
                embedding=vector,
                top_k=3,
            )

            await credits.update_ai_credits_redis(
                user_id=user_id,
                credit_type="embedding",
                total_chars=len(user_analyze_input),
                reference_id="embedding_generation",
            )
        # --------------------------------------------------
        # OPTIONAL RADAR DATA SOURCES
        # --------------------------------------------------

        import mimetypes as _mimetypes, tempfile as _tempfile

        # Pop playbook evidence blobs (always, so they don't pollute the runbook dict)
        _evidences_urls = runbook.pop("_playbook_evidences_urls", [])
        _ev_overview = runbook.pop("_playbook_evidence_overview", {})
        _ev_questions = runbook.pop("_playbook_ev_questions", [])

        data_checked = []
        reference_RWA = []
        if document_data:
            reference_RWA.append(document_data)

        if is_playbook_based_execution and _evidences_urls:
            _cf_prefix = (os.getenv("CLOUDFRNT", "")).rstrip("/") + "/"
            _s3_client = s3bucket()

            for url in _evidences_urls:
                s3_key = (
                    url.replace(_cf_prefix, "", 1)
                    if url.startswith(_cf_prefix)
                    else url
                )
                fname = os.path.basename(s3_key)
                ext = os.path.splitext(fname)[1].lower()
                try:
                    tmp_path = os.path.join(_tempfile.gettempdir(), fname)
                    _s3_client.download_file(
                        Bucket=S3_BUCKET, Key=s3_key, Filename=tmp_path
                    )
                    with open(tmp_path, "rb") as fh:
                        file_bytes = fh.read()
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass

                    ct = _mimetypes.guess_type(fname)[0] or "application/octet-stream"
                    extracted = extract_files_content(
                        [{"filename": fname, "data": file_bytes, "content_type": ct}]
                    )
                    for item in extracted:
                        if item.get("type") in IMAGE_EXTENSIONS:
                            ct = _mimetypes.guess_type(fname)[0] or "image/jpeg"
                            import base64 as _b64

                            b64 = _b64.b64encode(file_bytes).decode()
                            data_uri = f"data:{ct};base64,{b64}"

                            _ev_admissible = _ev_overview.get("admissible", [])
                            _evidence_summary = (
                                "\n".join(
                                    f"- {ev.get('artifact', '')}"
                                    for ev in _ev_admissible
                                )
                                or "No specific evidence types configured."
                            )

                            logger.info("Running vision extraction on image: %s", fname)
                            vision_result = await get_think_bedrock_vision_image(
                                data_uri=data_uri,
                                evidence_summary=_evidence_summary,
                                user_id=user_id,
                                credits=credits,
                            )

                            if vision_result:
                                meta = vision_result.get("image_meta", {})
                                logger.info(
                                    "Image vision result — type=%s timestamps=%s log_entries=%d",
                                    meta.get("image_type", "unknown"),
                                    meta.get("timestamps", []),
                                    len(meta.get("log_entries", [])),
                                )
                                # Build a single text blob with all extracted info
                                parts = []
                                if meta.get("extracted_text"):
                                    parts.append(
                                        f"Extracted text:\n{meta['extracted_text']}"
                                    )
                                if meta.get("timestamps"):
                                    parts.append(
                                        "Timestamps: " + ", ".join(meta["timestamps"])
                                    )
                                if meta.get("log_entries"):
                                    parts.append(
                                        "Log entries:\n"
                                        + "\n".join(meta["log_entries"])
                                    )
                                for found_item in vision_result.get("found", []):
                                    artifact = found_item.get("artifact", "")
                                    content = found_item.get("content", "")
                                    if artifact and content:
                                        parts.append(f"[{artifact}] {content}")
                                if parts:
                                    data_checked.append(
                                        {
                                            "type": "image",
                                            "source": s3_key,
                                            "data": "\n\n".join(parts),
                                        }
                                    )
                            continue
                        else:
                            data_checked.append(
                                {
                                    "type": "docs",
                                    "source": s3_key,
                                    "data": item["content"],
                                }
                            )
                except Exception as _e:
                    logger.warning(
                        "Playbook evidence extraction failed for %s: %s", s3_key, _e
                    )

        _playbook_ev_ctx = None
        if is_playbook_based_execution and _ev_overview:
            _playbook_ev_ctx = {
                "admissible_evidence": _ev_overview.get("admissible", []),
                "inadmissible_evidence": _ev_overview.get("inadmissible", []),
                "evidence_gap_responses": [
                    {
                        "question": q.get("question"),
                        "information": q.get("information"),
                        "answer_type": q.get("answer_type"),
                        "comment": q.get("comment"),
                        "artifact": q.get("evidence_artifact"),
                        "expectation": q.get("missing_expectation"),
                    }
                    for q in _ev_questions
                    if q.get("user_answer") is not None
                ],
            }

        if main_source and data_sources:
            logger.debug("Processing data sources")

            retrieved = await retreval_from_sources(
                conn,
                dbserver,
                main_source,
                data_sources,
                user_id,
                payload,
            )
            data_checked.extend(retrieved)
            if data_checked and len(data_checked) > 10:
                progress = 45

                await emit(
                    msg_builder.job_progress(
                        job_id,
                        session_id,
                        "report setup",
                        "extracted information from selected Responses & Evidences",
                        progress,
                    )
                )

        if refernce_main_source and reference_sources:
            logger.debug("Processing reference sources")

            reference_RWA = await retreval_from_sources(
                conn,
                dbserver,
                refernce_main_source,
                reference_sources,
                user_id,
                payload,
            )
            if reference_RWA and len(reference_RWA) > 10:
                if main_source and data_sources:
                    progress = 50
                else:
                    progress = 45

                await emit(
                    msg_builder.job_progress(
                        job_id,
                        session_id,
                        "report setup",
                        "extracted information from selected Governance Framework",
                        progress,
                    )
                )
        # ---------------------------------
        # LAST RESPONSE FETCH
        # ---------------------------------

        # if runbook_id or result_id :
        if is_prev_needed:
            val = await dbserver.get_latest_runbook_result(
                user_id=user_id, runbook_id=runbook_id, result_id=result_id
            )

            if val:
                last_runbook_response = json.dumps(val.get("result"))
                if not output_word_count:
                    output_word_count = (
                        val.get("estimated_word_count")
                        or val.get("document_meta", {}).get("estimated_word_count")
                        or 800  # fallback default
                    )

        # --------------------------------------------------
        # PROMPT SELECTION
        # --------------------------------------------------

        # prompts = runbook_yaml["radar"]["prompts"]
        structure_prompts = runbook_yaml["radar"]["structure_prompts"]

        # if structure_file_payload:
        # Prefer structure-based prompts
        review_temp = (
            RADAR_TEMPLATE.get(structure_prompts.get("review"))
            or RADAR_TEMPLATE.get(structure_prompts.get("analysis"))
            or RADAR_TEMPLATE.get(structure_prompts.get("recommendation"))
        )

        # -----------------------------
        # BLOCK-BY-BLOCK EXECUTION
        # -----------------------------
        final_blocks = []

        # -----------------------------
        # FORCE NORMALIZATION (FINAL FIX)
        # -----------------------------
        raw_structure = structure_file_payload or runbook.get("structure_theme")

        if isinstance(raw_structure, str):
            try:
                structure_file_payload = json.loads(raw_structure)
            except Exception as e:
                raise ValueError(f"Invalid JSON structure_theme: {e}")

        elif isinstance(raw_structure, dict):
            structure_file_payload = raw_structure

        else:
            raise ValueError("structure_theme must be str or dict")

        # -----------------------------
        # SAFETY CHECK (IMPORTANT)
        # -----------------------------
        if "blocks" not in structure_file_payload:
            raise ValueError("structure_file_payload missing 'blocks'")

        # print("FINAL TYPE:", type(structure_file_payload))
        # print("BLOCK COUNT:", len(structure_file_payload["blocks"]))
        # print("len file data", len(file_data))
        # print("data checked", len(data_checked))
        # print("reference data", len(reference_RWA))
        # print("last response data", len(last_runbook_response))

        progress = 55

        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "report generation",
                "starting to generate report",
                progress,
            )
        )

        # --------------------------------------------------
        # REDUCE DATA TO STAY WITHIN TOKEN LIMITS
        # --------------------------------------------------
        logger.debug("Reducing data_checked")
        if is_playbook_based_execution and _playbook_ev_ctx:
            reduced_datachecked = await _reduce_by_admissible_evidence(
                data_checked,
                _playbook_ev_ctx.get("admissible_evidence", []),
                user_id,
                credits,
            )
        else:
            reduced_datachecked = await reduce_data_for_report(
                data_checked, structure_file_payload, user_id, credits, label="evidence"
            )
        logger.debug("Reducing reference_rwa")
        reduced_referencerwa = await reduce_data_for_report(
            reference_RWA,
            structure_file_payload,
            user_id,
            credits,
            label="governance framework",
        )

        # Divide the total target across blocks and bound each section to a
        # 75–200 word range. The radar prompt templates enforce
        # ``{{requested_word_count}}`` as a per-block MINIMUM ("NEVER produce
        # fewer than ... expand to reach"), with no upper cap — so even after
        # dividing 850/6 ≈ 141, the LLM would expand each block to 800+ words
        # and the report would still overshoot the total target ~6×. We attach
        # an explicit upper-cap override at the end of each block_prompt below;
        # this clamp sets the per-section target the override will quote.
        n_blocks = max(1, len(structure_file_payload["blocks"]))
        try:
            total_word_count = int(output_word_count)
        except (TypeError, ValueError):
            total_word_count = 800
        # Even split of the total budget across blocks, clamped to [75, 200] —
        # the per-section range the product calls for. When n_blocks is large
        # enough that even_split < 75, we keep 75 as a readability floor and
        # accept that total may slightly exceed the requested budget.
        even_split = total_word_count // n_blocks
        per_block_word_count = max(75, min(200, even_split))
        per_block_max = per_block_word_count  # hard ceiling used in the override
        per_block_min = max(40, per_block_word_count // 2)
        logger.debug(
            "Word-count budget: total=%s blocks=%d even_split=%d target=[%d,%d]",
            output_word_count, n_blocks, even_split, per_block_min, per_block_max,
        )

        # Final-position override appended to every block prompt. Position +
        # explicit contradiction lets it supersede the earlier "MINIMUM ...
        # NEVER produce fewer" language baked into the YAML templates without
        # editing 8+ template sites.
        _WORD_CAP_OVERRIDE = (
            "\n\n"
            "================================================================\n"
            "FINAL WORD-COUNT OVERRIDE — HIGHEST PRIORITY, SUPERSEDES ALL EARLIER WORD-COUNT INSTRUCTIONS\n"
            "================================================================\n"
            f"For THIS block, produce BETWEEN {per_block_min} AND {per_block_max} visible prose words.\n"
            f"HARD UPPER LIMIT: {per_block_max} words. If you produce more, the response is invalid.\n"
            "Any earlier 'REQUIRED MINIMUM', 'expand explanations to reach', or 'NEVER produce fewer'\n"
            "language is SUPERSEDED by this cap. Be concise — favor signal over volume.\n"
            "Truncate, summarize, or omit lower-priority content to stay within the cap.\n"
            "================================================================\n"
        )

        logger.debug("Before generating report")
        for idx, block in enumerate(structure_file_payload["blocks"]):

            block_payload = {"blocks": [block]}  # isolate single block

            block_prompt = (
                review_temp.replace(
                    "{{structure_file_data}}", json.dumps(block_payload)
                )
                .replace("{{analyze_input}}", analyze_input)
                .replace(
                    "{{data_sources}}",
                    (
                        reduced_datachecked
                        if isinstance(reduced_datachecked, str)
                        else json.dumps(reduced_datachecked)
                    ),
                )
                .replace(
                    "{{file_data}}",
                    (
                        file_data
                        if isinstance(file_data, str)
                        else json.dumps(file_data)
                    ),
                )
                .replace(
                    "{{reference_sources}}",
                    (
                        reduced_referencerwa
                        if isinstance(reduced_referencerwa, str)
                        else json.dumps(reduced_referencerwa)
                    ),
                )
                .replace("{{output_language}}", output_language)
                .replace("{{requested_word_count}}", str(per_block_word_count))
            ) + _WORD_CAP_OVERRIDE

            result = await get_think_bedrok_response(
                user_message=block_prompt,
                user_id=user_id,
                credits=credits,
                total_input_chars=len(block_prompt),
                language="English",
                words_count=200,
            )

            parsed = _safe_json_parse(result)

            # -----------------------------
            # STRICT BLOCK EXTRACTION
            # -----------------------------
            if not parsed:
                logger.warning("LLM returned invalid JSON at block %d, skipping", idx)
                continue

            # LLM sometimes wraps output in {"raw_text": "..."} — unwrap and re-parse
            if (
                isinstance(parsed, dict)
                and "raw_text" in parsed
                and "block_id" not in parsed
                and "blocks" not in parsed
            ):
                inner = _safe_json_parse(parsed["raw_text"])
                if inner and isinstance(inner, dict):
                    parsed = inner
                else:
                    logger.warning(
                        "Could not extract block from raw_text at block %d, skipping",
                        idx,
                    )
                    continue

            if isinstance(parsed, dict) and "blocks" in parsed:
                final_blocks.append(parsed["blocks"][0])
            elif isinstance(parsed, dict) and "block_id" in parsed:
                final_blocks.append(parsed)
            else:
                logger.warning(
                    "Unexpected schema at block %d, skipping: keys=%s",
                    idx,
                    (
                        list(parsed.keys())
                        if isinstance(parsed, dict)
                        else type(parsed).__name__
                    ),
                )
                continue
        # -----------------------------
        # FINAL MERGE
        # -----------------------------
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "report generation",
                "generated content for report",
                60,
            )
        )
        merged_result = {
            "document_meta": parsed.get("document_meta", {}),
            "estimated_word_count": sum(b.get("word_count", 0) for b in final_blocks),
            "structure_rationale": "Block-by-block deterministic execution",
            "blocks": final_blocks,
        }
        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "report generation",
                "merging content for report",
                70,
            )
        )
        logger.debug("Reached report section 6")
        if merged_result:
            progress = 85

            await emit(
                msg_builder.job_progress(
                    job_id,
                    session_id,
                    "report generation",
                    "report generated and now trying to make risk analysis",
                    progress,
                )
            )
        # Risk analysis can be turned off per-runbook at creation time. When
        # disabled, skip the whole block (no LLM call) and mark the report so the
        # UI hides the section and the results filter keeps it visible.
        if risk_analysis_disabled(runbook):
            logger.debug("Risk analysis disabled for this runbook; skipping")
            merged_result["risk_analysis"] = None
            merged_result["risk_score"] = None
            merged_result["risk_analysis_disabled"] = True
        else:
            newdata_risk = ""
            if structure_file_content:
                logger.debug("Processing structure content")
                riskbaseprompt = """
                    You are a risk data compressor.

                    INPUT:
                    {{structure_file_content}}

                    TASK:
                    Extract ONLY critical fields needed for risk scoring.

                    STRICT RULES:
                    - Output must be under 1500 tokens
                    - Remove all descriptions, explanations, and duplicates
                    - Keep only:
                    - metrics
                    - scores
                    - counts
                    - risk indicators
                    - important flags
                    - Convert verbose text → short key-value pairs
                    - Ignore UI structure, headings, formatting

                    OUTPUT FORMAT (STRICT JSON):
                    {
                    "key_metrics": {},
                    "risk_indicators": [],
                    "scores": {},
                    "flags": []
                    }
                    """
                newdata_risk = await get_think_fire_response2_og(
                    user_message=riskbaseprompt,
                    user_id=user_id,
                    credits=credits,
                    total_input_chars=len(riskbaseprompt),
                )
                structure_file_content = newdata_risk

            # Per-org configurable scales (default Impact/Likelihood out of 5).
            risk_cfg = get_risk_config(user_id)

            # Load the prior report once: its risks (with stable risk_ids) seed the
            # prompt so the LLM reuses ids for reworded findings, and its overrides
            # are re-applied after scoring so manual edits survive the re-run.
            prior_ra = None
            try:
                prior = await dbserver.get_latest_runbook_result(
                    user_id=user_id, runbook_id=runbook_id, result_id=result_id
                )
                if prior:
                    prior_result = prior.get("result")
                    if isinstance(prior_result, str):
                        prior_result = _safe_json_parse(prior_result) or {}
                    prior_ra = (prior_result or {}).get("risk_analysis")
            except Exception:
                logger.warning("prior risk lookup (re-run) failed", exc_info=IS_DEV)

            risk_prompt = (
                RADAR_TEMPLATE["nist_risk_score_prompt"]
                .replace("{{analysis_result}}", json.dumps(merged_result))
                .replace(
                    "{{report_data}}",
                    json.dumps(structure_file_content)
                    if structure_file_content
                    else "",
                )
                .replace(
                    "{{prior_risks}}",
                    json.dumps(prior_risks_for_prompt(prior_ra)) if prior_ra else "",
                )
                .replace("{{impact_scale}}", str(risk_cfg.get("impact_scale", 5)))
                .replace(
                    "{{likelihood_scale}}", str(risk_cfg.get("likelihood_scale", 5))
                )
            )
            if merged_result:
                progress = 80

                await emit(
                    msg_builder.job_progress(
                        job_id,
                        session_id,
                        "risk analysis",
                        "generating risk analysis",
                        progress,
                    )
                )
            logger.debug("Before risk analysis")

            risk_result = await get_think_bedrok_response(
                user_message=risk_prompt,
                user_id=user_id,
                credits=credits,
                total_input_chars=len(risk_prompt),
            )

            risk_data = _safe_json_parse(risk_result) or {}

            # Deterministic scoring: the LLM only assigned Impact/Likelihood; the
            # backend computes risk_score, final_risk_score and risk_level here.
            computed = compute_risk(risk_data.get("risks", []), risk_cfg)
            computed["justification"] = risk_data.get("justification", "")

            # Re-apply any manual risk overrides from the prior report (loaded above)
            # so user edits survive a re-run. Matching is by the stable risk_id the
            # LLM was asked to carry forward.
            try:
                computed, dropped = apply_risk_overrides(computed, prior_ra)
                if dropped:
                    computed["dropped_overrides"] = dropped
                    logger.info(
                        "Re-run dropped %d manual risk override(s) for runbook %s",
                        len(dropped),
                        runbook_id,
                    )
            except Exception:
                logger.warning("apply_risk_overrides (re-run) failed", exc_info=IS_DEV)

            merged_result["risk_analysis"] = computed
            merged_result["risk_score"] = computed["final_risk_score"]

        if data_checked:
            logger.debug("data_checked length: %d", len(data_checked))
            report_viewer = (data_sources or {}).get("report_viewer")
            evidence_items = await run_evidence_analysis(
                data_checked, report_viewer, user_id, credits
            )
            merged_result["evidence_analysis"] = evidence_items

        if _playbook_ev_ctx:
            merged_result["evidence_analysis"] = {
                "items": merged_result.get("evidence_analysis", []),
                "admissible_evidence": _playbook_ev_ctx["admissible_evidence"],
                "inadmissible_evidence": _playbook_ev_ctx["inadmissible_evidence"],
                "evidence_gap_responses": _playbook_ev_ctx["evidence_gap_responses"],
            }

        await emit(
            msg_builder.job_progress(
                job_id,
                session_id,
                "risk analysis",
                "generated risk analysis and saving it to report",
                95,
            )
        )
        if custom_playbook_id:
            merged_result["document_meta"]["base_playbook_id"] = custom_playbook_id

        # Await tracker push instead of firing-and-forgetting. The engine is
        # often invoked under asyncio.run() (see run_runbook_job_wrapper), which
        # closes the event loop on return — any pending create_task() coroutine
        # gets cancelled mid-flight. That silently dropped tracker pushes for
        # any tracker with linked frameworks, since their per-row LLM analysis
        # ran longer than the parent's return path. _push_blocks_to_trackers
        # already swallows its own exceptions, so awaiting it cannot block the
        # engine from finishing successfully.
        await _push_blocks_to_trackers(user_id, runbook, merged_result, new_result_id)

        # Auto-submit to review workflow when the org is role-based configured.
        # Self-contained and best-effort: never fails runbook generation. The
        # workflow is keyed by result_id so the UI (which queries /workflow/
        # by-doc with result_id) can find it.
        _auto_submit_runbook_workflow(runbook_id, user_id, result_id=new_result_id)

        # Give each report an individual, human-readable name (runbook name +
        # 2-3 word AI descriptor from the first paragraph) so reports of the same
        # runbook don't all share one name. Best-effort; never blocks generation.
        try:
            from runbook.report_naming import build_report_name

            merged_result["report_name"] = await build_report_name(
                runbook.get("name"), merged_result, credits, user_id
            )
        except Exception:
            logger.warning("report_name generation failed", exc_info=IS_DEV)

        # Persist the completed result LAST. The frontend's "Report ready" pill
        # polls /runbook/results and turns green the moment it sees a row with
        # status='completed' — writing the row before tracker push + workflow
        # submission caused the pill to claim readiness while post-processing
        # was still running. Inserting last makes "completed" mean "the full
        # post-generation pipeline finished".
        await dbserver.insert_runbook_result(
            {
                "execution_id": execution_id,
                "result_id": new_result_id,
                "runbook_id": runbook_id,
                "user_id": user_id,
                "status": "completed",
                "risk_score": merged_result["risk_score"],
                "result": merged_result,
                "started_at": int(time.time()),
                "ended_at": int(time.time()),
            }
        )

        if merged_result:
            name = runbook["name"] or runbook_id
            await emit(
                msg_builder.global_msg(
                    f"generated report for {name}",
                )
            )

        return merged_result

    except Exception as e:
        logger.error("Runbook error: %s", e, exc_info=IS_DEV)
        await dbserver.insert_runbook_result(
            {
                "execution_id": execution_id,
                "result_id": new_result_id,
                "runbook_id": runbook_id,
                "user_id": user_id,
                "status": "failed",
                "result": {},
                "started_at": int(time.time()),
                "ended_at": int(time.time()),
            }
        )
        return None


async def trigger_runbooks_for_api_response(user_id, app_id, endpoint_id, record):
    try:
        dbserver = LanceDBServer()

        logger.info("trigger_runbooks_for_api_response started")

        # ✅ 1. GET TEMPLATE RUNBOOK
        runbook = await dbserver.get_runbooks_by_endpoint(
            user_id=user_id, app_id=app_id, endpoint_id=endpoint_id
        )

        if not runbook:
            logger.warning("No runbook found")
            return

        # ✅ safety
        if isinstance(runbook, str):
            runbook = json.loads(runbook)

        if not isinstance(runbook, dict):
            logger.warning("Invalid runbook format")
            return

        logger.info(
            "Using runbook: %s - %s", runbook.get("runbook_id"), runbook.get("name")
        )

        # ✅ 2. PREPARE EXECUTION INPUT
        runtime_input = record.get("original") or record.get("text")
        # print("api trig 1")
        if isinstance(runtime_input, dict):
            runtime_input = json.dumps(runtime_input)

        runbook["runtime_input"] = runtime_input
        # runbook["execution_id"] = f"exec_{int(time.time())}"
        runbook["app_id"] = app_id
        # reconstruct data_sources_full
        # if not runbook.get("data_sources_full"):
        #     runbook["data_sources_full"] = reconstruct_sources(
        #         runbook.get("data_sources", [])
        #     )

        # # reconstruct reference_sources_full
        # if not runbook.get("reference_sources_full"):
        #     runbook["reference_sources_full"] = reconstruct_sources(
        #         runbook.get("reference_sources", [])
        #     )

        # runbook["main_source"] = "app"
        # runbook["reference_main_source"] = "knowledge"
        structure_file = None

        files = runbook.get("files")

        # 🔥 FIX: normalize files if it's a string
        if isinstance(files, str):
            try:
                files = json.loads(files)
            except Exception as e:
                logger.warning("Failed to parse files: %s", e)
                files = {}

        # now safely use it
        if isinstance(files, dict):
            structure_file = files.get("structure_file")
        structure_file_payload = runbook["structure_theme"]

        # print("📥 INPUT:", runtime_input)

        # 💸 COST GUARD: skip the (expensive) AI analysis when the input that
        # actually drives the model is byte-for-byte identical to the last run
        # that COMPLETED for this endpoint+runbook. Scheduled/interval endpoints
        # re-fetch on every tick; without this, each tick re-pays the full Bedrock
        # bill even when nothing changed and nobody is using the feature. The
        # fingerprint is written only AFTER a successful run (see below) so a
        # failed run still retries instead of being suppressed.
        import hashlib
        from services.redis_service import get_redis

        _redis = get_redis()
        _runbook_id = runbook.get("runbook_id") or runbook.get("name") or "rb"
        _fp_key = f"runbook_lasthash:{user_id}:{endpoint_id}:{_runbook_id}"
        _current_fp = hashlib.sha256(
            str(runtime_input or "").encode("utf-8")
        ).hexdigest()
        try:
            _last_fp = await _redis.get(_fp_key)
            if isinstance(_last_fp, bytes):
                _last_fp = _last_fp.decode("utf-8")
            if _last_fp == _current_fp:
                logger.info(
                    "Runbook input unchanged for endpoint %s (runbook %s) — "
                    "skipping AI analysis to avoid duplicate cost",
                    endpoint_id,
                    _runbook_id,
                )
                return {"status": "skipped_unchanged"}
        except Exception as _fp_exc:
            # Never let the cost guard block a real run; fail open.
            logger.warning("runbook change-detection read failed (proceeding): %s", _fp_exc)

        # ✅ 3. EXECUTE (THIS WILL CREATE RESULT ENTRY)
        await run_runbook_execution_engine(
            dbserver=dbserver,
            user_id=user_id,
            runbook=runbook,
            structure_file=structure_file,
            structure_file_payload=structure_file_payload,
        )

        # Persist the fingerprint only after a successful run so that the next
        # identical scheduled tick is skipped, while a failed run still retries.
        try:
            await _redis.set(_fp_key, _current_fp, ex=30 * 24 * 3600)
        except Exception as _fp_exc:
            logger.warning("runbook change-detection write failed: %s", _fp_exc)

        return {"status": "success"}

    except Exception as e:
        logger.error("Error in trigger_runbooks: %s", e, exc_info=IS_DEV)
        raise


def upload_file_object(file, user_id):
    try:
        temp_path = f"/tmp/{uuid.uuid4()}_{file.filename}"

        # save file locally
        file.save(temp_path)

        # upload to S3
        result = upload_any_file(file_path=temp_path, user_id=user_id, type="runbook")

        # cleanup
        os.remove(temp_path)

        return result

    except Exception as e:
        return {"status": "error", "message": str(e)}


def fetch_cloudwatch_logs(log_group, log_stream=None, region="ca-central-1", limit=100):
    try:
        client = boto3.client("logs", region_name=region)

        kwargs = {
            "logGroupName": log_group,
            "limit": limit,
        }

        if log_stream:
            kwargs["logStreamNames"] = [log_stream]

        response = client.filter_log_events(kwargs)

        logs = []
        for event in response.get("events", []):
            logs.append({"timestamp": event["timestamp"], "message": event["message"]})

        return {"status": "success", "logs": logs}

    except Exception as e:
        return {"status": "error", "error": str(e)}


def parse_cloudwatch_url(url: str):
    try:
        parsed = urlparse(url)

        # Extract region
        query_params = parse_qs(parsed.query)
        region = query_params.get("region", ["ca-central-1"])[0]

        fragment = parsed.fragment  # everything after #

        # Decode twice (important for CloudWatch URLs)
        decoded = unquote(unquote(fragment))

        # Extract log group
        log_group_match = re.search(r"log-group/([^/]+)", decoded)
        log_group = unquote(log_group_match.group(1)) if log_group_match else None

        # Extract log stream
        log_stream_match = re.search(r"log-events/(.+)", decoded)
        log_stream = unquote(log_stream_match.group(1)) if log_stream_match else None

        return {
            "status": "success",
            "log_group": log_group,
            "log_stream": log_stream,
            "region": region,
        }

    except Exception as e:
        return {"status": "error", "error": str(e)}


def reconstruct_sources(filenames):
    result = []

    for item in filenames:
        if not item or ":" not in item:
            continue

        ftype, value = item.split(":", 1)

        if ftype == "scrape":
            result.append({"type": "scrape", "url": value})

        elif ftype in ["docs", "voice", "aud"]:
            result.append({"type": ftype, "filename": value})

    return {"filenames": result}


async def trigger_runbook_from_playbook(playbook_id, user_id, runbook_id):
    dbserver = LanceDBServer()
    logger.info("trigger_runbook_from_playbook started")
    logger.debug(
        "Details: playbook=%s runbook=%s user=%s", playbook_id, runbook_id, user_id
    )

    runbook = await dbserver.get_runbook_by_id(user_id=user_id, runbook_id=runbook_id)
    # print(type(runbook), len(runbook), runbook)
    if isinstance(runbook, list):
        runbook = runbook[0] if runbook else None

    if isinstance(runbook, str):
        runbook = json.loads(runbook)

    if not runbook:
        logger.error(
            "Runbook not found in DB: runbook_id=%s user_id=%s playbook_id=%s",
            runbook_id, user_id, playbook_id,
        )
        return {"status": "failed", "error": "runbook_not_found"}

    logger.info(
        "Using runbook: %s - %s", runbook.get("runbook_id"), runbook.get("name")
    )
    # print("out of range 2")
    structure_file = None

    files = runbook.get("files")

    # 🔥 FIX: normalize files if it's a string
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except Exception as e:
            logger.warning("Failed to parse files: %s", e)
            files = {}

    # now safely use it
    if isinstance(files, dict):
        structure_file = files.get("structure_file")

    raw_structure = runbook.get("structure_theme")
    structure_file_payload = None
    if isinstance(raw_structure, str):
        try:
            structure_file_payload = json.loads(raw_structure)
        except Exception:
            raise ValueError("Invalid JSON in structure_theme")

    elif isinstance(raw_structure, dict):
        structure_file_payload = raw_structure
    instruction_data = await get_playbook_instruction(user_id, playbook_id)
    if not instruction_data:
        logger.error(
            "Playbook instruction data missing: playbook=%s user=%s "
            "(expected at S3 key {user}/workflow/<basename>/<playbook>.json)",
            playbook_id, user_id,
        )
        return {"status": "failed", "error": "playbook_instruction_not_found"}
    logger.debug("Instruction data keys: %s", list(instruction_data.keys()))

    # Stash playbook evidence data for the execution engine
    runbook["_playbook_evidences_urls"] = instruction_data.get("evidences_ques", [])
    runbook["_playbook_evidence_overview"] = instruction_data.get(
        "evidence_overview", {}
    )
    runbook["_playbook_ev_questions"] = instruction_data.get(
        "evidence_based_questions", []
    )
    # runbook["runtime_input"] = json.dumps(runtime_input.get("chat", []))

    logger.debug("runtime_input type: %s", type(instruction_data))
    logger.debug("Before question extraction")
    questions = await extract_qna_from_instruction(instruction_data)
    logger.debug("After question extraction")

    logger.info("Total questions: %d", len(questions))
    logger.debug("Sample question: %s", questions[0] if questions else "None")

    logger.debug("Reference sources: %s", runbook.get("reference_sources"))
    document_data = None
    if runbook.get("reference_sources"):
        analyzed_results = await analyze_questions_with_references(
            questions,
            runbook.get("reference_sources"),
            runbook.get("reference_main_source"),
            user_id,
            runbook,
        )
        if not analyzed_results:
            logger.warning("No analysis results generated")

        logger.debug("analyzed_results type: %s", type(analyzed_results))
        logger.debug(
            "analyzed_results first item type: %s",
            type(analyzed_results[0]) if analyzed_results else "empty",
        )
        if analyzed_results:
            merged = await merge_document_data(analyzed_results, instruction_data)
            runbook["runtime_input"] = json.dumps(merged["chat"])
            # document_data = merged.get("chat")
            # print("document_data : ", len(document_data))
            # print("🔍 After merge sample:", merged[0])
    else:
        runbook["runtime_input"] = json.dumps(instruction_data.get("chat", []))

    # print("final: ", str(runbook.get("runtime_input"))[:100])
    logger.info("Executing runbook playbook")
    try:
        result = await run_runbook_execution_engine(
            dbserver=dbserver,
            user_id=user_id,
            runbook=runbook,
            structure_file=structure_file,
            structure_file_payload=structure_file_payload,
            is_playbook_based_execution=True,
            custom_playbook_id=playbook_id,
        )
        # The engine swallows its own exceptions and returns None on failure
        # (after writing a status="failed" runbook_results row). Surface that
        # as a structured failure here so the Celery result isn't misleading.
        if result is None:
            logger.error(
                "run_runbook_execution_engine returned None — engine wrote a "
                "status='failed' result row. playbook=%s runbook=%s user=%s "
                "(see prior 'Runbook error: ...' log for the underlying exception)",
                playbook_id, runbook_id, user_id,
            )
            return {"status": "failed", "error": "engine_returned_none"}
        logger.info(
            "Runbook execution finished: playbook=%s runbook=%s user=%s",
            playbook_id, runbook_id, user_id,
        )
        return {"status": "success", "result": result}
    except Exception as exc:
        logger.exception(
            "run_runbook_execution_engine failed: playbook=%s runbook=%s user=%s",
            playbook_id, runbook_id, user_id,
        )
        return {"status": "failed", "error": str(exc)}


async def extract_qna_from_instruction(instruction_data):
    result = []

    try:
        logger.debug("extract_qna_from_instruction started")

        # ✅ Handle string input
        if isinstance(instruction_data, str):
            instruction_data = json.loads(instruction_data)

        logger.debug("Instruction data keys: %s", list(instruction_data.keys()))

        chats = instruction_data.get("chat", [])
        logger.debug("Total chats: %d", len(chats))

        for chat in chats:
            outputs = chat.get("output", [])
            logger.debug("Outputs count: %d", len(outputs))

            for item in outputs:
                if not isinstance(item, dict):
                    # print("⚠️ Skipping invalid item:", item)
                    continue

                qid = item.get("id")
                question = item.get("question")
                comment = item.get("comment")

                # 🔥 Normalize user answer
                options = item.get("options", {}) or {}
                raw_answer = item.get("user_answer")
                question_type = ""

                if isinstance(raw_answer, str) and raw_answer in options:
                    answer = options.get(raw_answer)
                    question_type = "MCQ"
                else:
                    answer = raw_answer
                    question_type = "DESCRIPTIVE"

                if not qid or not question:
                    continue

                result.append(
                    {
                        "id": qid,
                        "question": question,
                        "user_answer": answer,
                        "options": options,
                        "question_type": question_type,
                        "comment": comment,
                        "section": item.get("section"),
                    }
                )

        logger.info("Extracted questions: %d", len(result))

    except Exception as e:
        logger.error("Error extracting QnA: %s", e)

    return result


import json


async def merge_document_data(analyzed_results, instruction_data):

    # 🔥 Ensure instruction_data is dict
    if isinstance(instruction_data, str):
        instruction_data = json.loads(instruction_data)

    result_map = {
        item.get("id"): item
        for item in analyzed_results
        if isinstance(item, dict) and item.get("id")
    }

    chats = instruction_data.get("chat", [])

    for chat in chats:
        outputs = chat.get("output", [])

        # 🔥 Normalize outputs list
        clean_outputs = []
        for o in outputs:
            if isinstance(o, str):
                try:
                    o = json.loads(o)  # ✅ FIX: convert string → dict
                except Exception:
                    # print("⚠️ Skipping invalid output:", o)
                    continue
            clean_outputs.append(o)

        chat["output"] = clean_outputs  # ✅ overwrite with cleaned data

        for item in clean_outputs:
            if not isinstance(item, dict):
                # print("⚠️ Still invalid item:", item)
                continue

            qid = item.get("id")

            if not qid or qid not in result_map:
                continue

            result = result_map[qid]

            item["document_data"] = result.get("document_data", {})
            item["evaluation_data"] = result.get("evaluation_data", {})

    return instruction_data


async def playbook_runbook_execution(user_id, runbook):

    logger.info("Executing playbook for runbook: %s", runbook["runbook_id"])
    await run_runbook_execution_engine(user_id=user_id, runbook=runbook)


async def create_runbook_for_playbook(playbook_id, user_id):
    playbook_result = await get_playbook_instruction(
        user_id=user_id, filename=playbook_id
    )

    workflow = playbook_result.get("workflow", {})
    name = workflow.get("name", "")
    description = workflow.get("description", "")
    # building runbook data for playbook
    runbook_data = {
        "runbook_id": str(uuid.uuid4()),
        "user_id": user_id,
        "name": name,
        "description": description,
        "runbook_type": "playbook",
        "schedule": "",  # cron expression
        "input_type": "playbook",
        "playbook_id": playbook_id,
        "api_endpoint": "",
        "log_source": "",
        "files": [],
        "links": [],
        "data_sources": [],
        "reference_sources": [],
        "created_at": int(time.time()),
    }
    logger.info("Creating runbook for playbook: %s", playbook_id)
    # Insert runbook details
    result = await dbserver.insert_runbook(runbook_data)

    runbook_data["main_source"] = ""
    runbook_data["reference_main_source"] = ""

    return runbook_data


import json
import re


def safe_json_parsestruy(data):
    # ✅ Case 1: Already parsed
    if isinstance(data, (list, dict)):
        return data

    # ✅ Case 2: Must be string-like
    if not isinstance(data, (str, bytes, bytearray)):
        raise ValueError(f"Unexpected type: {type(data)}")

    text = data.decode() if isinstance(data, (bytes, bytearray)) else data

    # ✅ Extract JSON using regex (handles ```json blocks or extra text)
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if match:
        text = match.group(1)

    # ✅ Final parse
    return json.loads(text)


async def structure_payload_generation(
    user_id,
    analyze_input,
    structure_file,
    emit=None,
    session_id=None,
    job_id=None,
    mprogress=None,
):
    from websockets_custom.ws_instance import ws_service, msg_builder_main

    msg_builder = msg_builder_main
    try:
        structure_file_payload = []
        STR_LINKS = []
        credits = Credits()

        # =============================
        # 🔹 STEP 1: FILE PROCESSING
        # =============================
        if not mprogress:
            if emit:
                await emit(
                    msg_builder.job_progress(
                        job_id=job_id,
                        session_id=session_id,
                        stage="structure_processing",
                        message="📂 Processing structure files...",
                        progress=10,
                    )
                )

        process_file_payloads(
            user_id=user_id,
            files=(
                structure_file if isinstance(structure_file, list) else [structure_file]
            ),
            inp_links=STR_LINKS,
            extracted_payload=structure_file_payload,
        )

        # =============================
        # 🔹 STEP 2: LANGUAGE DETECTION
        # =============================
        if not mprogress:
            if emit:
                await emit(
                    msg_builder.job_progress(
                        job_id=job_id,
                        session_id=session_id,
                        stage="language_detection",
                        message="🌐 Detecting language & word count...",
                        progress=25,
                    )
                )

        lang_prompt = RADAR_TEMPLATE["language_wordcount_extractor"].replace(
            "{{analyze_input}}", str(analyze_input or "")
        )

        result = await get_think_fire_response2_og(
            user_message=lang_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=len(lang_prompt),
        )

        lang_data = json.loads(result)
        output_language = lang_data.get("language", "English")
        output_word_count = lang_data.get("word_count", "800")
        if not output_word_count:
            output_word_count = "800"
        if not mprogress:
            if emit:
                await emit(
                    msg_builder.job_progress(
                        job_id=job_id,
                        session_id=session_id,
                        stage="language_detection",
                        message=f"Using {output_language} as language for report",
                        progress=25,
                    )
                )

        # =============================
        # 🔹 STEP 3: STRUCTURE GENERATION
        # =============================
        if not mprogress:
            if emit:
                await emit(
                    msg_builder.job_progress(
                        job_id=job_id,
                        session_id=session_id,
                        stage="structure_generation",
                        message="🧠 Generating structure...",
                        progress=60,
                    )
                )

        structure_prompt = RADAR_TEMPLATE["structure_prompt_template"]

        structure_prompt = (
            structure_prompt.replace(
                "{{document_file_data}}", json.dumps(structure_file_payload)
            )
            .replace("{{file_links}}", json.dumps(STR_LINKS))
            .replace("{{user_original_prompt_or_context}}", analyze_input or "")
            .replace("{{output_language}}", output_language)
            .replace("{{output_word_count}}", output_word_count)
        )

        base_chars = len(structure_prompt)

        for img in STR_LINKS:
            base_chars -= len(img)
            base_chars += image_credit_cost(img)

        reresult = await get_think_bedrok_response(
            user_message=structure_prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=base_chars,
        )

        structure_file_payload = safe_json_parsestruy(reresult)

        def get_block_hash(block):
            return hashlib.md5(json.dumps(block, sort_keys=True).encode()).hexdigest()

        # ✅ Normalize + Merge blocks WITHOUT duplication
        payload = structure_file_payload

        merged_blocks = []
        seen_hashes = set()
        meta = None

        # 🔥 Handle BOTH cases properly
        if isinstance(payload, list):
            docs = payload

        elif isinstance(payload, dict):
            # if already final structure, just return
            if "blocks" in payload:
                docs = [payload]
            else:
                docs = payload.get("data", {}).get("data", [])

        else:
            docs = []

        for doc in docs:
            blocks = doc.get("blocks", [])

            for b in blocks:
                h = get_block_hash(b)

                # ✅ Skip duplicates safely
                if h in seen_hashes:
                    continue

                seen_hashes.add(h)
                merged_blocks.append(b)

            # take first meta
            if not meta:
                meta = doc.get("document_meta")

        structure_file_payload = {
            "blocks": merged_blocks,
            "document_meta": meta,
            "success": True,
        }

        # =============================
        # 🔹 STEP 4: DONE
        # =============================
        if not mprogress:
            if emit:
                await emit(
                    msg_builder.job_progress(
                        job_id=job_id,
                        session_id=session_id,
                        stage="structure_done",
                        message="✅ Structure ready",
                        progress=50,
                    )
                )

        logger.info("✅ STRUCTURE GENERATED")
        return structure_file_payload

    except Exception:
        logger.exception("❌ STRUCTURE GENERATION FAILED")

        if emit:
            await emit(
                msg_builder.job_error(
                    job_id=job_id,
                    session_id=session_id,
                    message="Structure generation failed",
                )
            )

        raise


async def Modify_default_structure(user_id, analyze_input, default_structure):
    try:
        default_structure_payload = []

        prompt = RUNBOOK_TEMPLATE["default_structure_modification__prompt"]

        prompt = prompt.replace("{{analyze_input}}", analyze_input).replace(
            "{{default_structure}}", json.dumps(default_structure, indent=2)
        )

        base_char = len(prompt)

        result = await get_think_fire_response2_og(
            user_message=prompt,
            user_id=user_id,
            credits=credits,
            total_input_chars=base_char,
        )
        if result == "INSUFFICIENT":
            raise ValueError("Insufficient AI credits to generate structure")
        default_structure_payload = safe_json_parsestruy(result)
        logger.info("✅DEFAULT STRUCTURE MODIFIED")

        return default_structure_payload
    except Exception as e:
        logger.exception("❌ Failed to modify default structure")
        raise e


async def pick_best_source_for_workflow(instruction_text, source_contexts, top_k=2):

    embedding = await get_firework_embedding()
    wf_vec = embedding.embed_query(instruction_text[:3000])  # truncate

    # best_source = None
    # best_score = -1

    selected = []

    for source, ctx in source_contexts.items():
        text = " ".join([c.get("data", "") for c in ctx])
        src_vec = embedding.embed_query(text[:3000])

        score = cosine_similarity(wf_vec, src_vec)

        selected.append((source, score))
        # if score > best_score:
        #     best_score = score
        #     best_source = source

    selected.sort(key=lambda x: x[1], reverse=True)
    return selected[:top_k]


import math


def cosine_similarity(vec1, vec2):
    if not vec1 or not vec2:
        return 0.0

    # 🔥 Ensure same length
    min_len = min(len(vec1), len(vec2))
    vec1 = vec1[:min_len]
    vec2 = vec2[:min_len]

    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return dot_product / (norm1 * norm2)


def extract_sources(reference_source):
    sources = []

    if isinstance(reference_source, dict):
        files = reference_source.get("filenames", [])

        for f in files:
            if isinstance(f, dict) and f.get("filename"):
                sources.append(f["filename"])

    elif isinstance(reference_source, list):
        # fallback (if already list)
        sources = reference_source

    return sources


async def analyze_questions_with_references(
    questions,
    reference_source,
    reference_main_source,
    user_id,
    runbook,
    progress_logs=None,
):

    logger.debug("analyze_questions_with_references started")
    results = []
    payload = None
    if reference_main_source == "knowledge":

        embedding = await get_firework_embedding()
        value = runbook.get("analyze_input") or runbook.get("description")

        vector = embedding.embed_query(value)

        payload = QueryData(
            user_id=user_id,
            embedding=vector,
            top_k=3,
        )

        await credits.update_ai_credits_redis(
            user_id=user_id,
            credit_type="embedding",
            total_chars=len(value),
            reference_id="embedding_generation",
        )
    # 🔥 FETCH CONTEXT ONLY ONCE
    logger.debug("reference_main_source: %s", reference_main_source)
    logger.debug("reference_source: %s", reference_source)

    instruction_text = "\n".join(
        [
            f"Q: {q.get('question', '')} A: {q.get('user_answer', '')}"
            for q in questions
            if isinstance(q, dict)
        ]
    )
    if isinstance(reference_source, str):
        try:
            reference_source = json.loads(reference_source)
        except Exception as e:
            logger.warning("Failed to parse reference_source: %s", e)
            reference_source = {}

    if not isinstance(reference_source, dict):
        reference_source = {}
    if isinstance(reference_source, str):
        try:
            reference_source = json.loads(reference_source)
        except Exception as e:
            logger.warning("Second decode failed: %s", e)
            reference_source = {}
    files = reference_source.get("filenames", [])
    all_source_contexts = {}
    for file in files:
        fname = file.get("filename")
        single_source = {"filenames": [file]}

        ctx = await retreval_from_sources(
            conn,
            dbserver,
            reference_main_source,
            single_source,
            user_id,
            payload,
        )
        all_source_contexts[fname] = ctx

    # 🔥 Step C: Pick best source ONCE

    best_source = await pick_best_source_for_workflow(
        instruction_text, all_source_contexts
    )

    logger.info("Selected best source: %s", best_source)

    # 🔥 Step D: Use ONLY that source
    context_text = ""
    for source in best_source:
        best_context_chunks = all_source_contexts.get(source, [])
        context_text += "\n".join([c.get("data", "") for c in best_context_chunks])

    # print("context :",context_text)

    # 🔥 Create tasks with shared context
    # tasks = [
    #     analyze_single_question(
    #         q,
    #         context_text,  # ✅ pass precomputed context
    #         user_id,
    #     )
    #     for q in questions
    # ]

    # responses = await asyncio.gather(*tasks, return_exceptions=True)

    # for q, res in zip(questions, responses):
    #     if isinstance(res, Exception):
    #         print(f"Error analyzing question {q.get('id')}: {res}")
    #         continue

    #     res["id"] = q.get("id")
    #     results.append(res)
    BATCH_SIZE = 5
    all_results = []

    for i in range(0, len(questions), BATCH_SIZE):
        chunk = questions[i : i + BATCH_SIZE]

        logger.debug("Processing batch %d", i // BATCH_SIZE + 1)

        batch_result = await analyze_single_question(chunk, context_text, user_id)

        if not batch_result:
            # log(f"⚠️ Batch {i//BATCH_SIZE + 1} returned empty")

            logger.warning("Empty batch result")
            continue
        logger.debug("Batch %d completed", i // BATCH_SIZE + 1)
        all_results.extend(batch_result)

    logger.info("Total analyzed results: %d", len(results))

    return all_results

    return results


async def analyze_single_question(
    question_item,
    context_text,  # ✅ already computed
    user_id,
):
    # question_text = question_item.get("question")
    # user_answer = question_item.get("user_answer")
    # options = question_item.get("options")
    # question_type = question_item.get("question_type")
    qna_list = []

    for q in question_item:
        if not isinstance(q, dict):
            continue

        options = q.get("options") if q.get("question_type") == "MCQ" else None

        qna_list.append(
            {
                "id": q.get("id"),
                "question": q.get("question"),
                "question_type": q.get("question_type", "DESCRIPTIVE"),
                "user_answer": q.get("user_answer"),
                "comment": q.get("comment"),
                "options": options,
            }
        )

    # 🧠 Build prompt
    prompt = RUNBOOK_TEMPLATE["process_question_analysis_prompt"]
    # prompt = (
    #     prompt.replace("{{question}}", question_text or "")
    #     .replace("{{user_answer}}", user_answer or "")
    #     .replace("{{question_type}}", question_type or "DESCRIPTIVE")
    #     .replace(
    #         "{{options}}",
    #         (
    #             json.dumps(options, indent=2)
    #             if (question_type == "MCQ" and options)
    #             else None
    #         ),
    #     )
    #     .replace("{{context}}", context_text or "No reference data available")
    # )
    prompt = prompt.replace(
        "{{questions_json}}", json.dumps(qna_list, indent=2)
    ).replace("{{context}}", context_text or "No reference data available")

    # print("🧠 FINAL PROMPT:\n", prompt[:50])
    base_char = len(prompt)

    # 🤖 LLM call
    # result = await get_think_fire_response2_og(
    #     user_message=prompt,
    #     user_id=user_id,
    #     credits=credits,
    #     total_input_chars=base_char,
    # )
    result = await get_fireworks_response2(
        user_id=user_id, user_message=prompt, role="system", credits=credits, temp=0.0
    )

    # 🧹 Safe JSON parsing
    try:
        parsed = json.loads(result)
    except Exception:
        logger.error("Invalid JSON from LLM: %s", result[:500])
        return []

    return parsed


async def store_runbook_trigger_schedule(user_id, runbook_id, schedule):
    try:
        res = await dbserver.update_runbook_schedule(user_id, runbook_id, schedule)
    except:
        raise Exception


async def save_runbook_schedule(
    *,
    user_id: str,
    runbook_id: str,
    schedule_type: str,
    timezone: str,
    data: dict,
):
    import json, asyncio
    from datetime import datetime

    schedule_obj = {
        "type": schedule_type,
        "timezone": timezone,
        "data": data,
        "celery": {
            "task_id": None,
            "entry_name": None,
            "stop_key": None,
        },
        "execution_unique_key": None,
        "status": "scheduled",
        "last_run_at": None,
        "next_run_at": None,
        "created_at": datetime.utcnow().isoformat(),
    }

    res = await dbserver.update_runbook_schedule(user_id, runbook_id, schedule_obj)

    return {"status": "saved", "result": res}


async def activate_runbook_schedule(user_id: str, runbook_id: str):
    import json, asyncio, uuid
    from datetime import datetime

    row = await dbserver.get_runbook_by_id(user_id=user_id, runbook_id=runbook_id)

    if not row:
        raise Exception("Runbook not found")

    schedule = json.loads(row["schedule"])

    schedule_type = schedule["type"]
    timezone = schedule["timezone"]
    data = schedule["data"]

    uniquekey = f"{runbook_id}_{uuid.uuid4()}"

    # -----------------------------------
    # SELECT TASK (NEW CELERY TASKS)
    # -----------------------------------
    # if row.get("playbook_id"):
    #     task_name = "tasks.trigger_runbook_from_playbook_task"
    #     args = [user_id, row["playbook_id"], runbook_id]

    # elif row.get("api_endpoint"):
    #     task_name = "tasks.trigger_runbook_from_api_task"
    #     args = [user_id, row["api_endpoint"], row["api_endpoint"], {}]

    # else:
    #     raise Exception("No trigger source found")

    # -----------------------------------
    # SCHEDULING
    # -----------------------------------
    # if schedule_type == "daily":
    #     hour, minute = map(int, data["startTime"].split(":"))

    #     result = await SchedulerService.schedule_daily(
    #         hour, minute, user_id, task_name, timezone, args
    #     )

    #     schedule["celery"]["entry_name"] = result["entry_name"]

    # elif schedule_type == "weekly":
    #     hour, minute = map(int, data["startTime"].split(":"))

    #     result = await SchedulerService.schedule_weekly(
    #         data["weekday"], hour, minute, user_id, task_name, timezone, args
    #     )

    #     schedule["celery"]["entry_name"] = result["entry_name"]

    # elif schedule_type == "one_time":
    #     dt = datetime.fromisoformat(data["datetime"])

    #     result = await SchedulerService.schedule_one_time(
    #         dt, user_id, task_name, timezone, args
    #     )

    #     schedule["celery"]["task_id"] = result["task_id"]

    # elif schedule_type == "custom":
    #     result = await SchedulerService.schedule_custom(
    #         start_date=data["startDate"],
    #         start_time=data["startTime"],
    #         userid=user_id,
    #         filename=task_name,
    #         timezone=timezone,
    #         contacts=args,
    #     )

    #     schedule["celery"]["task_id"] = result["task_id"]

    # else:
    #     raise Exception("Unsupported schedule type")

    # schedule["execution_unique_key"] = uniquekey

    # -----------------------------------
    # SAVE BACK
    # -----------------------------------
    await dbserver.update_runbook_schedule(user_id, runbook_id, schedule)

    return {
        "status": "activated",
        "runbook_id": runbook_id,
    }


async def trigger_scheduled_playbook_runbook(user_id, runbook_id):
    pass


async def trigger_scheduled_api_runbook(user_id, runbook_id):
    pass
