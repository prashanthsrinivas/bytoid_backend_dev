PERMISSION_ROUTES = {

    "apps.endpoint.view": [
        "/apps/get-endpoints",
        "/apps/endpoint-details"
    ],

    "apps.endpoint.add": [
        "/apps/create-endpoint"
    ],

    "apps.endpoint.edit": [
        "/apps/update-endpoint"
    ],

    "apps.endpoint.delete": [
        "/apps/delete-endpoint"
    ],

    "trackers.table.view": [
        "/tracker/list",
        "/tracker/details",
        "/tracker/view",
        "/tracker/check-duplicate"
    ],

    "trackers.table.create": [
        "/tracker/create"
    ],

    "trackers.table.delete": [
        "/tracker/delete"
    ],

    "trackers.table.edit": [
        "/tracker/modify",
        "/tracker/sync-from-block",
        "/tracker/upload-evidence",
        "/tracker/ai/complete_tracker_change",
        "/tracker/ai/selected_tracker_change",
        "/tracker/ai/selected_row_tracker_change",
        "/tracker/ai/selected_column_tracker_change",
        "/tracker/ai/selected_rows_tracker_change",
        "/tracker/ai/selected_columns_tracker_change",
        "/tracker/ai/save_tracker_change"
    ],

    "trackers.row.add": [
        "/tracker/append",
        "/tracker/add-entry"
    ],

    "trackers.column.add": [
        "/tracker/add-column"
    ],

    "trackers.column.delete": [
        "/tracker/delete-column"
    ],

    "trackers.framework.add": [
        "/tracker/add-framework"
    ],

    "trackers.framework.edit": [
        "/tracker/update-framework"
    ],

    "trackers.framework.delete": [
        "/tracker/remove-framework"
    ],

    "trackers.logs.view": [],

    # ================= WORKFLOW BUILDER =================
    "workflow.process.view": [
        "/playbook/jbs/<job_id>",
        "/get_all_instructions",
        "/get_single_instruction",
        "/test-email-checks",
        "/get-allfunctions",
        "/list_chat_config",
        "/check_runbook_exists_playbook",
        "/evidence_confirmation",
        "/questionarie_confirmation"
    ],

    "workflow.process.create": [
        "/create_instruction",
        "/pb_temp_clone",
        "/install_global_playbook"
    ],

    "workflow.process.edit": [
        "/update_instruction",
        "/add_a_step",
        "/edit_a_step",
        "/update_step_arguments",
        "/delete_step_argument",
        "/delete_a_step",
        "/modify_instruction",
        "/clear-playground-data",
        "/clear-testing-data",
        "/update-questions",
        "/update-questions-bulk",
        "/update-form-field",
        "/update-form-fields-bulk",
        "/autocheck-status-update",
        "/wf-form",
        "/generate_ques_by_file",
        "/make_ans_by_files",
        "/evidence_ques_ans_attach_playbook",
        "/make_s3upload",
        "/clear_runbook_exists_playbook",
        "/edit_assigned_question",
        "/delete_assigned_question",
        "/morph_question",
        "/assign_evidence_to_question"
    ],

    "workflow.process.delete": [
        "/delete_instruction",
        "/pb_delete_clone"
    ],

    "workflow.process.execute": [
        "/run_workflow",
        "/run_workflow_step",
        "/test-playground-step",
        "/generate-workflow-input",
        "/test-mid",
        "/autocheck-workflow",
        "/workflow/conversation"
    ],

    "workflow.process.schedule": [
        "/schedule-workflow",
        "/schedule-workflow-checker"
    ],

    "workflow.process.share": [
        "/share_playbook_template",
        "/undo_share_playbook_template"
    ],

    "workflow.template.view": [
        "/get_all_global_instructions",
        "/get_single_global_instruction"
    ],

    "workflow.template.create": [
        "/make_global_playbook"
    ],

    "workflow.template.delete": [
        "/delete_global_playbook"
    ],

    # ================= COMPLIANCE ENGINE - RUNBOOK =================
    "compliance.runbook.read": [
        "/runbook/status/<job_id>",
        "/runbook/results/<runbook_id>",
        "/runbook/results_list/<user_id>",
        "/runbooks/list/<user_id>",
        "/runbook/<runbook_id>/<user_id>",
        "/allrunbook/<user_id>",
        "/runbook/check_playbook/<playbook_id>",
        "/result/<result_id>",
        "/check_pb_output",
        "/runbook/evidence/config",
        "/runbook_evidence_config",
        "/evidence_check"
    ],

    "compliance.runbook.create": [
        "/runbook/create",
        "/create_playbook_runbook",
        "/runbook/structure_extract"
    ],

    "compliance.runbook.edit": [
        "/runbook/modify",
        "/runbook/delete_result",
        "/runbook/update/<runbook_id>",
        "/runbook/results_delete/<runbook_id>",
        "/result/<result_id>/evidence_analysis",
        "/result/<result_id>/evidence_admissibility",
        "/runbook/structure_extract_modify",
        "/runbook/evidence/add",
        "/runbook_evidence_configure"
    ],

    "compliance.runbook.delete": [
        "/runbook/delete/<runbook_id>",
        "/runbook/delete_all"
    ],

    "compliance.runbook.execute": [
        "/schedule_runbook"
    ],

    # ================= COMPLIANCE ENGINE - POLICY HUB =================
    "compliance.report.read": [
        "/policy-hub/status",
        "/policy-hub/edit-status",
        "/policy-hub/list",
        "/policy-hub/frameworks/available",
        "/policy-hub/frameworks/access",
        "/policy-hub/frameworks",
        "/policy-hub/frameworks/list",
        "/policy-hub/frameworks/search",
        "/policy-hub/frameworks/<framework_id>"
    ],

    "compliance.report.create": [
        "/policy-hub/generate"
    ],

    "compliance.report.edit": [
        "/policy-hub/edit",
        "/policy-hub/update"
    ],

    "compliance.report.delete": [
        "/policy-hub/delete"
    ],

    "compliance.framework.create": [
        "/policy-hub/frameworks/upload",
        "/policy-hub/frameworks/save"
    ],

    "compliance.framework.delete": [
        "/policy-hub/frameworks/<framework_id>"
    ],

    # ================= APPS / API CONNECTOR =================
    "apps.endpoint.view": [
        "/apiconnector/apps/<user_id>",
        "/apiconnector/apps/<int:app_id>/endpoints",
        "/apiconnector/apps/endpoints/<int:endpoint_id>/runs",
        "/apiconnector/apps/endpoints/<int:endpoint_id>/runs/<filename>"
    ],

    "apps.create": [
        "/apiconnector/apps"
    ],

    "apps.edit": [
        "/apiconnector/apps/<int:app_id>",
        "/apiconnector/apps/<int:app_id>/auth"
    ],

    "apps.delete": [
        "/apiconnector/apps/<int:app_id>",
        "/apiconnector/apps/<int:app_id>/hard-delete"
    ],

    "apps.endpoint.add": [
        "/apiconnector/apps/<int:app_id>/endpoints"
    ],

    "apps.endpoint.edit": [
        "/apiconnector/apps/endpoints/<int:endpoint_id>"
    ],

    "apps.endpoint.delete": [
        "/apiconnector/apps/endpoints/<int:endpoint_id>"
    ],

    "apps.endpoint.test": [
        "/apiconnector/apps/test",
        "/apiconnector/apps/endpoints/<int:endpoint_id>/test",
        "/apiconnector/apps/<int:app_id>/test",
        "/apiconnector/apps/global/apps/<app_id>/<endpoint_id>/test"
    ],

    "apps.endpoint.execute": [
        "/apiconnector/apps/<int:app_id>/execute",
        "/apiconnector/apps/endpoints/<int:endpoint_id>/execute"
    ],

    "apps.endpoint.schedule": [
        "/apiconnector/apps/<int:app_id>/schedule",
        "/apiconnector/apps/endpoints/<int:endpoint_id>/schedule",
        "/apiconnector/apps/endpoints/<int:endpoint_id>/schedules/stop"
    ],

    "apps.endpoint.push": [
        "/apiconnector/apps/admin/pushapp",
        "/apiconnector/apps/admin/pushapp_endpoint",
        "/apiconnector/apps/global/apps/change",
        "/apiconnector/apps/global/app_endpoint/change"
    ],

    "apps.view": [
        "/apiconnector/apps/global/apps/<user_id>",
        "/apiconnector/apps/global/apps/<user_id>/<app_id>/endpoints"
    ],

    "apps.install": [
        "/apiconnector/apps/user/global-app/instantiate",
        "/apiconnector/apps/user/global-endpoint/instantiate"
    ],

    # ================= TASKBOX (EMAIL) =================
    "taskbox.email.view": [
        "/gmail/drafts",
        "/gmail/threads",
        "/gmail/spam",
        "/gmail/trash",
        "/gmail/inbox_info/<userid>",
        "/gmail/datewise/<userid>",
        "/gmail/start_watch/<userid>",
        "/gmail/history_check/<userid>/<hisid>",
        "/microsoft/get_emails_infinite",
        "/microsoft/get_email_detail",
        "/microsoft/get_email",
        "/microsoft/get_emails_batch",
        "/microsoft/get_emails_count",
        "/microsoft/fetch_all_emails",
        "/microsoft/trigger_email_fetch",
        "/process-outlook",
        "/microsoft/sent_items",
        "/microsoft/drafts",
        "/microsoft/spam",
        "/microsoft/trash",
        "/check-microsoft-user",
        "/get_microsoft_client_id",
        "/get_all_messages2/<user_id>",
        "/check_umail/<userid>",
        "/get_all_messages/<user_id>",
        "/conversations/<user_id>/<next_cursor>",
        "/conversations_og/<user_id>/<next_cursor>",
        "/conversations_test/<user_id>/<next_cursor>",
        "/selected_conversation/<conversation_id>/<user_id>",
        "/async_message/<userid>",
        "/sync/check_should_sync/<user_id>",
        "/sync/trigger_on_login/<user_id>",
        "/sync/trigger_manual/<user_id>",
        "/sync/status/<user_id>",
        "/sync/reset_timer/<user_id>",
        "/check-lastmsg/<user_id>/<thread_id>",
        "/set_mailbox_setting",
        "/unified_drafts",
        "/get_active_customers",
        "/get_dormant_customers",
        "/get_active_leads",
        "/get_dormant_leads",
        "/get_snoozed_customers",
        "/get_no_of_customers",
        "/get_assignee_list"
    ],

    "taskbox.email.send": [
        "/gmail/respond",
        "/gmail/forward",
        "/microsoft/send_mail",
        "/start-conversation",
        "/send-reply",
        "/send-reply_test",
        "/send-reply-with-attachments",
        "/send-mail"
    ],

    "taskbox.email.draft": [
        "/gmail/drafts/<draft_id>",
        "/gmail/create_draft"
    ],

    "taskbox.email.attachments.view": [
        "/attachment-test",
        "/attach-file",
        "/attach-files"
    ],

    "taskbox.email.attachments.download": [
        "/gmail/attachment/download",
        "/gmail/download_attachment"
    ],

    "taskbox.email.change_status": [
        "/snooze_customer"
    ],

    "taskbox.autopilot.enable": [
        "/ai_autopilot",
        "/ai_autopilot-reset/<userid>",
        "/auto-reply-email"
    ],

    "taskbox.autopilot.cancel": [
        "/ai_autopilot-revoke"
    ],

    "taskbox.ai.switch": [
        "/ai_autopilot-mode"
    ],

    "taskbox.ai.autopilot": [
        "/ai_autopilot/<userid>"
    ],

    "taskbox.ai.suggest": [
        "/ai_suggest",
        "/test_functions"
    ],

    "taskbox.agent.assign": [
        "/ai_autopilot-update-agent",
        "/change_assignee"
    ],

    "notes.create": [
        "/create_note"
    ],

    "notes.edit": [
        "/update_note",
        "/search_users_for_sharing",
        "/share_note_by_email"
    ],

    "notes.delete": [
        "/delete_note"
    ],

    "notes.filter": [
        "/get_conversation_notes",
        "/get_note_permissions",
        "/get_user_notes"
    ],

    "admin.manage_users": [
        "/deletedb/<user_id>",
        "/delete_user_cache/<primary_user_id>",
        "/delete_user/<user_id>",
        "/start_gmail_watches",
        "/check_redis",
        "/microsoft/session-debug",
        "/check_notes_tables"
    ],

    # ================= KNOWLEDGE BASE (RADAR / DOCS) =================
    "kb.doc.view": [
        "/radar/apps/list/<userid>",
        "/radar/reviews/<userid>",
        "/radar/docs",
        "/radar/status",
        "/radar/current",
        "/get-usersDocs"
    ],

    "kb.doc.upload": [
        "/process-drive",
        "/process-local"
    ],

    "kb.doc.edit": [
        "/radar/review",
        "/radar/analyze",
        "/radar/decide",
        "/radar/changeblock",
        "/radar/changeblock/confirm",
        "/radar/knowledge/analyze"
    ],

    "kb.doc.delete": [
        "/radar/delete",
        "/delete_file"
    ],

    # ================= KNOWLEDGE BASE (WEB / SCRAPE) =================
    "kb.web.view": [
        "/get-youtube-summaries",
        "/get-website-summaries",
        "/get-web-summaries",
        "/get-website-details",
        "/get-website-summary",
        "/list-scraped-websites",
        "/check-scrape-check"
    ],

    "kb.web.add": [
        "/scrape-youtube",
        "/scrape",
        "/scrape-and-summarize",
        "/scrape-website-fast",
        "/save-website-summary",
        "/scrape-website-page",
        "/scrape-and-summarize-fast"
    ],

    "kb.web.edit": [
        "/edit-website-summary",
        "/edit-internal_link-summary",
        "/update-contacts-scraped",
        "/update-scraped-status"
    ],

    "kb.web.delete": [
        "/delete-youtube-summary",
        "/delete-website-summary",
        "/delete-internal_link-summary"
    ],

    # ================= KNOWLEDGE BASE (VOICE / RECORDINGS) =================
    "kb.recording.view": [
        "/get-audio-config",
        "/get-audio-transcript"
    ],

    "kb.recording.upload": [
        "/process_audio"
    ],

    "kb.recording.delete": [
        "/delete-audio"
    ],

    "kb.voice.manage": [
        "/update-transcript",
        "/update-audio-contacts"
    ]
}