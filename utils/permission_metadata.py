PERMISSION_METADATA = {
    # ================= WORKSPACE =================
    "workspace.intake_workflow": {
        "label": "Intake Workflow Access",
        "module": "Workspace",
        "type": "access",
        "dependencies": []
    },
    "workspace.compliance_engine": {
        "label": "Compliance Engine Access",
        "module": "Workspace",
        "type": "access",
        "dependencies": []
    },
    "workspace.trackers": {
        "label": "Trackers Access",
        "module": "Workspace",
        "type": "access",
        "dependencies": []
    },
    

    # ================= INTAKE WORKFLOW =================
    "intake.bytoid_pro": {
        "label": "Bytoid Pro",
        "module": "Intake",
        "type": "access",
        "dependencies": ["workspace.intake_workflow"]
    },
    "intake.bytoid_reference": {
        "label": "Bytoid Reference",
        "module": "Intake",
        "type": "access",
        "dependencies": ["workspace.intake_workflow"]
    },
    "intake.bytoid_support": {
        "label": "Bytoid Support",
        "module": "Intake",
        "type": "access",
        "dependencies": ["workspace.intake_workflow"]
    },
    "intake.workflow_process": {
        "label": "Workflow Process",
        "module": "Intake",
        "type": "access",
        "dependencies": ["workspace.intake_workflow"]
    },

    # ================= COMPLIANCE ENGINE =================
    "compliance.runbook.read": {
        "label": "View Runbooks",
        "module": "Compliance",
        "type": "read",
        "dependencies": ["workspace.compliance_engine", "radar.view"]
    },
    "compliance.runbook.create": {
        "label": "Create Runbooks",
        "module": "Compliance",
        "type": "create",
        "dependencies": ["workspace.compliance_engine", "compliance.runbook.read"]
    },
    "compliance.runbook.edit": {
        "label": "Edit Runbooks",
        "module": "Compliance",
        "type": "update",
        "dependencies": ["compliance.runbook.create"]
    },
    "compliance.runbook.delete": {
        "label": "Delete Runbooks",
        "module": "Compliance",
        "type": "delete",
        "dependencies": ["compliance.runbook.create"]
    },
    "compliance.runbook.execute": {
        "label": "Execute Runbook",
        "module": "Compliance",
        "type": "execute",
        "dependencies": ["compliance.runbook.read"]
    },
    "compliance.standalone.create": {
        "label": "Create Standalone Reports",
        "module": "Compliance",
        "type": "create",
        "dependencies": ["workspace.compliance_engine"]
    },
    "compliance.standalone.edit": {
        "label": "Edit Standalone Reports",
        "module": "Compliance",
        "type": "update",
        "dependencies": ["compliance.standalone.create"]
    },
    "compliance.standalone.delete": {
        "label": "Delete Standalone Reports",
        "module": "Compliance",
        "type": "delete",
        "dependencies": ["compliance.standalone.create"]
    },

    # ================= TRACKERS =================
    "trackers.table.view": {
        "label": "View Trackers",
        "module": "Trackers",
        "type": "read",
        "dependencies": ["workspace.trackers"]
    },
    "trackers.framework.add": {
        "label": "Add Framework",
        "module": "Trackers",
        "type": "create",
        "dependencies": ["workspace.trackers"]
    },
    "trackers.framework.edit": {
        "label": "Edit Framework",
        "module": "Trackers",
        "type": "update",
        "dependencies": ["trackers.framework.add"]
    },
    "trackers.framework.delete": {
        "label": "Delete Framework",
        "module": "Trackers",
        "type": "delete",
        "dependencies": ["trackers.framework.add"]
    },
    "trackers.column.add": {
        "label": "Add Columns",
        "module": "Trackers",
        "type": "create",
        "dependencies": ["workspace.trackers"]
    },
    "trackers.column.delete": {
        "label": "Delete Columns",
        "module": "Trackers",
        "type": "delete",
        "dependencies": ["trackers.column.add"]
    },
    "trackers.row.add": {
        "label": "Add Rows",
        "module": "Trackers",
        "type": "create",
        "dependencies": ["workspace.trackers"]
    },
    "trackers.table.create": {
        "label": "Create Table",
        "module": "Trackers",
        "type": "create",
        "dependencies": ["workspace.trackers", "trackers.table.view"]
    },
    "trackers.table.edit": {
        "label": "Edit Table",
        "module": "Trackers",
        "type": "update",
        "dependencies": ["trackers.table.create"]
    },
    "trackers.table.delete": {
        "label": "Delete Table",
        "module": "Trackers",
        "type": "delete",
        "dependencies": ["trackers.table.create"]
    },
    "trackers.table.chat": {
        "label": "Tracker AI Chat",
        "module": "Trackers",
        "type": "execute",
        "dependencies": ["trackers.table.view"]
    },

    # ================= WORKFLOW BUILDER =================
    "workflow.process.view": {
        "label": "View Processes",
        "module": "Workflow Builder",
        "type": "read",
        "dependencies": []
    },
    "workflow.template.view": {
        "label": "View Global Templates",
        "module": "Workflow Builder",
        "type": "read",
        "dependencies": []
    },
    "workflow.process.create": {
        "label": "Create Process",
        "module": "Workflow Builder",
        "type": "create",
        "dependencies": ["workflow.process.view"]
    },
    "workflow.process.edit": {
        "label": "Edit Process",
        "module": "Workflow Builder",
        "type": "update",
        "dependencies": ["workflow.process.create"]
    },
    "workflow.process.delete": {
        "label": "Delete Process",
        "module": "Workflow Builder",
        "type": "delete",
        "dependencies": ["workflow.process.create"]
    },
    "workflow.process.share": {
        "label": "Share Process",
        "module": "Workflow Builder",
        "type": "update",
        "dependencies": ["workflow.process.create"]
    },
    "workflow.process.execute": {
        "label": "Execute Process",
        "module": "Workflow Builder",
        "type": "execute",
        "dependencies": ["workflow.process.view"]
    },
    "workflow.process.schedule": {
        "label": "Schedule Process",
        "module": "Workflow Builder",
        "type": "schedule",
        "dependencies": ["workflow.process.view"]
    },
    "workflow.template.create": {
        "label": "Make Global Templates",
        "module": "Workflow Builder",
        "type": "create",
        "dependencies": ["workflow.process.create"]
    },
    "workflow.template.delete": {
        "label": "Delete Global Templates",
        "module": "Workflow Builder",
        "type": "delete",
        "dependencies": ["workflow.template.create"]
    },

    # ================= EVIDENCE ===============================
    "evidence.view": {
        "label": "View Evidence",
        "module": "Evidence",
        "type": "read",
        "dependencies": []
    },

    "evidence.create": {
        "label": "Create Evidence",
        "module": "Evidence",
        "type": "create",
        "dependencies": ["evidence.view"]
    },

    "evidence.edit": {
        "label": "Edit Evidence",
        "module": "Evidence",
        "type": "update",
        "dependencies": ["evidence.create"]
    },

    "evidence.delete": {
        "label": "Delete Evidence",
        "module": "Evidence",
        "type": "delete",
        "dependencies": ["evidence.create"]
    },

    "evidence.execute": {
        "label": "Run Evidence Check",
        "module": "Evidence",
        "type": "execute",
        "dependencies": ["evidence.view"]
    },

    # ================ AGENT HUB ================================
    "agents.view": {
        "label": "View Agents",
        "module": "Agents",
        "type": "read",
        "dependencies": []
    },

    "agents.share": {
        "label": "Share Agent Permission",
        "module": "Agents",
        "type": "update",
        "dependencies": ["agents.view"]
    },

    "agents.unshare": {
        "label": "Unshare Agent Permission",
        "module": "Agents",
        "type": "update",
        "dependencies": ["agents.view"]
    },

    "agents.delete": {
        "label": "Remove Agent",
        "module": "Agents",
        "type": "delete",
        "dependencies": ["agents.view"]
        },

    # ================= KNOWLEDGE BASE (PROFILE) =================
    
    "kb.api.regenerate": {
        "label": "Regenerate API Key",
        "module": "Knowledge Base",
        "type": "update",
        "dependencies": []
    },
    # ================= KNOWLEDGE BASE (WEB) =================
    "kb.web.view": {
        "label": "View Web Source",
        "module": "Knowledge Base",
        "type": "read",
        "dependencies": []
    },
    "kb.web.add": {
        "label": "Add Web Source",
        "module": "Knowledge Base",
        "type": "create",
        "dependencies": ["kb.web.view"]
    },
    "kb.web.delete": {
        "label": "Delete Web Source",
        "module": "Knowledge Base",
        "type": "delete",
        "dependencies": ["kb.web.add"]
    },
    "kb.web.edit": {
        "label": "Edit Web Source",
        "module": "Knowledge Base",
        "type": "update",
        "dependencies": ["kb.web.add"]
    },

    # ================= KNOWLEDGE BASE (RECORDINGS) =================
    "kb.recording.upload": {
        "label": "Upload Recording",
        "module": "Knowledge Base",
        "type": "create",
        "dependencies": ["kb.recording.view"]
    },
    "kb.recording.view": {
        "label": "View Recordings",
        "module": "Knowledge Base",
        "type": "read",
        "dependencies": []
    },
    "kb.recording.delete": {
        "label": "Delete Recording",
        "module": "Knowledge Base",
        "type": "delete",
        "dependencies": ["kb.recording.view"]
    },
    "kb.voice.manage": {
        "label": "Manage Voice Data",
        "module": "Knowledge Base",
        "type": "update",
        "dependencies": ["kb.recording.view"]
    },

    # ================= KNOWLEDGE BASE (DOCUMENTS) =================
    "kb.doc.upload": {
        "label": "Upload Document",
        "module": "Knowledge Base",
        "type": "create",
        "dependencies": []
    },
    "kb.doc.view": {
        "label": "View Documents",
        "module": "Knowledge Base",
        "type": "read",
        "dependencies": []
    },
    "kb.doc.delete": {
        "label": "Delete Document",
        "module": "Knowledge Base",
        "type": "delete",
        "dependencies": ["kb.doc.upload"]
    },
    
    # ================= REPORTS =================
    "radar.view": {
        "label": "View Reports",
        "module": "Reports",
        "type": "read",
        "dependencies": ["workspace.compliance_engine"]
    },

    "radar.edit": {
        "label": "Edit Reports",
        "module": "Reports",
        "type": "update",
        "dependencies": ["radar.view"]
    },

    "radar.delete": {
        "label": "Delete Reports",
        "module": "Reports",
        "type": "delete",
        "dependencies": ["radar.view"]
    },
    # ================= TASKBOX (EMAIL) =================
    "taskbox.email.view": {
        "label": "View Email Content",
        "module": "Taskbox",
        "type": "read",
        "dependencies": []
    },
    "taskbox.email.send": {
        "label": "Send Email",
        "module": "Taskbox",
        "type": "create",
        "dependencies": ["taskbox.email.view"]
    },
    "taskbox.email.draft": {
        "label": "Manage Drafts",
        "module": "Taskbox",
        "type": "update",
        "dependencies": ["taskbox.email.view"]
    },
    "taskbox.email.change_status": {
        "label": "Change Status",
        "module": "Taskbox",
        "type": "update",
        "dependencies": ["taskbox.email.view"]
    },
    "taskbox.email.attachments.view": {
        "label": "View Attachments",
        "module": "Taskbox",
        "type": "read",
        "dependencies": ["taskbox.email.view"]
    },
    "taskbox.email.attachments.download": {
        "label": "Download Attachments",
        "module": "Taskbox",
        "type": "read",
        "dependencies": ["taskbox.email.attachments.view"]
    },

    # ================= TASKBOX (AI ASSIST) =================
    "taskbox.ai.suggest": {
        "label": "View AI Suggest Tab",
        "module": "Taskbox",
        "type": "read",
        "dependencies": ["taskbox.email.view"]
    },
    "taskbox.ai.autopilot": {
        "label": "View Autopilot Tab",
        "module": "Taskbox",
        "type": "read",
        "dependencies": ["taskbox.email.view"]
    },
    "taskbox.ai.switch": {
        "label": "Switch AI Modes",
        "module": "Taskbox",
        "type": "update",
        "dependencies": ["taskbox.ai.suggest", "taskbox.ai.autopilot"]
    },

    # ================= TASKBOX (AUTOPILOT) =================
    
    "taskbox.autopilot.enable": {
        "label": "Enable Autopilot",
        "module": "Taskbox",
        "type": "update",
        "dependencies": ["taskbox.ai.autopilot"]
    },
    
    "taskbox.autopilot.cancel": {
        "label": "Cancel Enable",
        "module": "Taskbox",
        "type": "update",
        "dependencies": ["taskbox.autopilot.enable"]
    },

    # ================= TASKBOX (AGENTS) =================
    
    "taskbox.agent.assign": {
        "label": "Assign Agent",
        "module": "Taskbox",
        "type": "update",
        "dependencies": ["taskbox.email.view"]
    },

    # ================= NOTES =================
    "notes.create": {
        "label": "Create Note",
        "module": "Notes",
        "type": "create",
        "dependencies": []
    },
    "notes.edit": {
        "label": "Edit Note",
        "module": "Notes",
        "type": "update",
        "dependencies": ["notes.create"]
    },
    "notes.delete": {
        "label": "Delete Note",
        "module": "Notes",
        "type": "delete",
        "dependencies": ["notes.create"]
    },
    "notes.filter": {
        "label": "Filter Notes",
        "module": "Notes",
        "type": "read",
        "dependencies": []
    },

    # ================= CALENDAR =================
    "calender.create": {
        "label": "Create Event",
        "module": "Calendar",
        "type": "create",
        "dependencies": []
    },

    "calender.view.cancelled": {
        "label": "View Cancelled Events",
        "module": "Calendar",
        "type": "read",
        "dependencies": []
    },

    "calender.view.confirmed": {
        "label": "View Confirmed Events",
        "module": "Calendar",
        "type": "read",
        "dependencies": []
    
    },
    "calender.edit": {
        "label": "Edit Calendar Events",
        "module": "Calendar",
        "type": "update",
        "dependencies": ["calender.view.confirmed"]
    },

    "calender.delete": {
        "label": "Delete Calendar Events",
        "module": "Calendar",
        "type": "delete",
        "dependencies": ["calender.view.confirmed"]
    },

    # ================= MY APPS =================
    "apps.create": {
        "label": "Create App",
        "module": "My Apps",
        "type": "create",
        "dependencies": []
    },
    "apps.edit": {
        "label": "Edit App",
        "module": "My Apps",
        "type": "update",
        "dependencies": ["apps.create"]
    },
    "apps.delete": {
        "label": "Delete App",
        "module": "My Apps",
        "type": "delete",
        "dependencies": ["apps.create"]
    },
    "apps.view": {
        "label": "View Global Apps",
        "module": "My Apps",
        "type": "read",
        "dependencies": []
    },
    "apps.install": {
        "label": "Install App",
        "module": "My Apps",
        "type": "create",
        "dependencies": ["apps.view"]
    },
    "apps.endpoint.view": {
        "label": "View Endpoints",
        "module": "My Apps",
        "type": "read",
        "dependencies": []
    },
    "apps.endpoint.add": {
        "label": "Add Endpoint",
        "module": "My Apps",
        "type": "create",
        "dependencies": ["apps.endpoint.view"]
    },
    "apps.endpoint.edit": {
        "label": "Edit Endpoint",
        "module": "My Apps",
        "type": "update",
        "dependencies": ["apps.endpoint.add"]
    },
    "apps.endpoint.schedule": {
        "label": "Schedule Endpoint",
        "module": "My Apps",
        "type": "create",
        "dependencies": ["apps.endpoint.view"]
    },
    "apps.endpoint.test": {
        "label": "Test Endpoint",
        "module": "My Apps",
        "type": "execute",
        "dependencies": ["apps.endpoint.view"]
    },
    "apps.endpoint.execute": {
        "label": "Execute Endpoint",
        "module": "My Apps",
        "type": "execute",
        "dependencies": ["apps.endpoint.view"]
    },
    "apps.endpoint.push": {
        "label": "Push to Global",
        "module": "My Apps",
        "type": "update",
        "dependencies": ["apps.endpoint.add"]
    },
    "apps.endpoint.delete": {
        "label": "Delete Endpoint",
        "module": "My Apps",
        "type": "delete",
        "dependencies": ["apps.endpoint.add"]
    },

    # ================= POLICY HUB =================

    "policyhub.view": {
        "label": "View Policy Hub",
        "module": "Policy Hub",
        "type": "read",
        "dependencies": []
    },

    "policyhub.create": {
        "label": "Create Policies",
        "module": "Policy Hub",
        "type": "create",
        "dependencies": ["policyhub.view"]
    },

    "policyhub.edit": {
        "label": "Edit Policies",
        "module": "Policy Hub",
        "type": "update",
        "dependencies": ["policyhub.create"]
    },

    "policyhub.delete": {
        "label": "Delete Policies",
        "module": "Policy Hub",
        "type": "delete",
        "dependencies": ["policyhub.create"]
    },
    "policyhub.framework.view": {
        "label": "View Frameworks",
        "module": "Policy Hub",
        "type": "read",
        "dependencies": ["policyhub.view"]
    },

    "policyhub.framework.create": {
        "label": "Upload Frameworks",
        "module": "Policy Hub",
        "type": "create",
        "dependencies": ["policyhub.view"]
    },

    "policyhub.framework.delete": {
        "label": "Delete Frameworks",
        "module": "Policy Hub",
        "type": "delete",
        "dependencies": ["policyhub.framework.create"]
    },

    # ================= TEAM =================
    "team.search": {
        "label": "Search Agents",
        "module": "Team",
        "type": "read",
        "dependencies": []
    },
    "team.filter": {
        "label": "Filter by Status",
        "module": "Team",
        "type": "read",
        "dependencies": ["team.search"]
    },
    "team.add_vendor": {
        "label": "Add Vendor",
        "module": "Team",
        "type": "create",
        "dependencies": ["team.search"]
    },
    "team.workflow.view": {
        "label": "View Workflows",
        "module": "Team",
        "type": "read",
        "dependencies": ["team.search"]
    },
    "team.status.view": {
        "label": "View Status",
        "module": "Team",
        "type": "read",
        "dependencies": ["team.search"]
    },
    "team.access.add": {
        "label": "Add Access",
        "module": "Team",
        "type": "create",
        "dependencies": ["team.search"]
    },
    "team.access.share": {
        "label": "Share Access",
        "module": "Team",
        "type": "update",
        "dependencies": ["team.access.add"]
    },
    "team.member.view": {
        "label": "View Member Details",
        "module": "Team",
        "type": "read",
        "dependencies": ["team.search"]
    },

    "team.member.edit": {
        "label": "Edit Member",
        "module": "Team",
        "type": "update",
        "dependencies": ["team.member.view"]
    },

    "team.member.delete": {
        "label": "Delete Member",
        "module": "Team",
        "type": "delete",
        "dependencies": ["team.member.view"]
    },

    "team.group.view": {
        "label": "View Groups",
        "module": "Team",
        "type": "read",
        "dependencies": ["team.search"]
    },

    "team.group.create": {
        "label": "Create Groups",
        "module": "Team",
        "type": "create",
        "dependencies": ["team.group.view"]
    },

    "team.group.edit": {
        "label": "Edit Groups",
        "module": "Team",
        "type": "update",
        "dependencies": ["team.group.create"]
    },

    "team.group.delete": {
        "label": "Delete Groups",
        "module": "Team",
        "type": "delete",
        "dependencies": ["team.group.create"]
    },
    

    # ================= ADMIN =================
    "admin.manage_users": {
        "label": "Manage Users",
        "module": "Admin",
        "type": "admin",
        "dependencies": []
    },
    "admin.manage_admins": {
        "label": "Manage Admin Access",
        "module": "Admin",
        "type": "admin",
        "dependencies": ["admin.manage_users"]
    },

    # ============= Trust Center ==============
    "trustcenter.view": {
        "label": "View Trust Center",
        "module": "Trust Center",
        "type": "view",
        "dependencies": []
    },

    "trustcenter.share": {
        "label": "Share Trust Center",
        "module": "Trust Center",
        "type": "share",
        "dependencies": ["trustcenter.view"]
    },

    "trustcenter.whitepaper.regenerate": {
        "label": "Regenerate Whitepaper",
        "module": "Trust Center",
        "type": "execute",
        "dependencies": ["trustcenter.view"]
    },

    "trustcenter.document.download": {
        "label": "Download PDF",
        "module": "Trust Center",
        "type": "view",
        "dependencies": ["trustcenter.view"]
    },

    "trustcenter.whitepaper.edit": {
        "label": "Edit Whitepaper",
        "module": "Trust Center",
        "type": "edit",
        "dependencies": ["trustcenter.view"]
    },

    "trustcenter.document.upload": {
        "label": "Upload Documents",
        "module": "Trust Center",
        "type": "create",
        "dependencies": ["trustcenter.view"]
    },
    "trustcenter.document.delete": {
        "label": "Delete Documents",
        "module": "Trust Center",
        "type": "delete",
        "dependencies": ["trustcenter.document.upload"]
    },
}


DEFAULT_USER_PERMISSIONS = [
    "workspace.compliance_engine",
    "compliance.runbook.read",
    "radar.view",
    "kb.doc.view",
    "taskbox.email.view",
    "notes.filter",
    "trackers.table.view",
    "policyhub.view",
]
