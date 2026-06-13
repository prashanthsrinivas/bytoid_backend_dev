"""Data-access layer for the Strategy governance module.

Tables (see ``create_db.py``): ``strategic_objectives``, ``programs``,
``projects``, ``project_doc_links``, ``project_tracker_links``,
``strategy_milestones``.

Multi-tenancy: every row carries an ``org_id`` resolved from the workspace
user's ``launch_id_fk`` (falling back to ``company_name`` then the user_id) so
strategy is visible org-wide — matching the "scope of the admin / organization"
requirement. The ``owner_user_id`` column is the *assigned* responsible user and
is independent of the org scope.

Health/drill-down reuse the existing reverse-lookup graph in
``services.statement_tracker_refs`` (imported lazily to avoid import-time DB
coupling). Pure aggregation lives in ``strategy/rollup.py``.
"""

import uuid
from datetime import date, datetime

import pymysql.cursors

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger
from strategy import rollup as _rollup

logger = get_logger(__name__)


# ── connection / serialization helpers ────────────────────────────────────────

def _conn():
    conn = connect_to_rds()
    if conn is None:
        raise ConnectionError("No RDS connection available.")
    return conn


def _ser(row: dict) -> dict:
    """Make a DictCursor row JSON-safe (dates/datetimes → ISO strings)."""
    if not row:
        return row
    out = {}
    for k, v in row.items():
        if isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _date(v):
    """Normalize an incoming date value to 'YYYY-MM-DD' or None."""
    if not v:
        return None
    s = str(v).strip()
    return s[:10] if s else None


# ── org scope ─────────────────────────────────────────────────────────────────

def resolve_org_key(user_id: str, conn=None) -> str:
    """Return the org scope key for a workspace user.

    Prefers ``launch_id_fk``, then ``company_name``, then the user_id itself, so
    members of the same org compute the same key. Mirrors the org definition in
    ``invited_users/routes.py::_same_org``.
    """
    if not user_id:
        return user_id
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT launch_id_fk, company_name FROM users WHERE user_id=%s",
                (user_id,),
            )
            row = cur.fetchone() or {}
        launch = (row.get("launch_id_fk") or "").strip()
        if launch:
            return launch
        company = (row.get("company_name") or "").strip()
        if company:
            return f"company:{company.lower()}"
        return user_id
    finally:
        if own:
            conn.close()


def user_in_scope(reference_user_id: str, target_user_id: str, conn=None) -> bool:
    """True when ``target_user_id`` is in the same org as ``reference_user_id``.

    Used to validate owner assignment: an owner must belong to the workspace's
    organization. Self-assignment is always allowed.
    """
    if not target_user_id:
        return False
    if target_user_id == reference_user_id:
        return True
    own = conn is None
    conn = conn or _conn()
    try:
        ref = resolve_org_key(reference_user_id, conn)
        tgt = resolve_org_key(target_user_id, conn)
        return bool(ref) and ref == tgt
    finally:
        if own:
            conn.close()


# ── generic CRUD helpers ──────────────────────────────────────────────────────

_OBJECTIVE_COLS = "id, owner_user_id, org_id, created_by, title, description, status, start_date, target_date, created_at, updated_at"
_PROGRAM_COLS = "id, objective_id, owner_user_id, org_id, created_by, name, description, status, start_date, target_date, created_at, updated_at"
_PROJECT_COLS = "id, objective_id, program_id, owner_user_id, org_id, created_by, name, description, status, start_date, target_date, created_at, updated_at"


# Objectives -------------------------------------------------------------------

def create_objective(org_id, owner_user_id, created_by, data) -> dict:
    oid = str(uuid.uuid4())
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "INSERT INTO strategic_objectives "
                "(id, owner_user_id, org_id, created_by, title, description, status, start_date, target_date) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    oid, owner_user_id, org_id, created_by,
                    data.get("title"), data.get("description"),
                    data.get("status", "draft"),
                    _date(data.get("start_date")), _date(data.get("target_date")),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return get_objective(oid, org_id)


def list_objectives(org_id) -> list:
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT {_OBJECTIVE_COLS} FROM strategic_objectives "
                "WHERE org_id=%s ORDER BY created_at DESC",
                (org_id,),
            )
            return [_ser(r) for r in (cur.fetchall() or [])]
    finally:
        conn.close()


def get_objective(oid, org_id) -> dict | None:
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT {_OBJECTIVE_COLS} FROM strategic_objectives "
                "WHERE id=%s AND org_id=%s",
                (oid, org_id),
            )
            row = cur.fetchone()
            return _ser(row) if row else None
    finally:
        conn.close()


def update_objective(oid, org_id, data) -> dict | None:
    fields, params = _build_update(
        data, ["title", "description", "status"], date_fields=["start_date", "target_date"]
    )
    if data.get("owner_user_id"):
        fields.append("owner_user_id=%s")
        params.append(data["owner_user_id"])
    if not fields:
        return get_objective(oid, org_id)
    params.extend([oid, org_id])
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"UPDATE strategic_objectives SET {', '.join(fields)} WHERE id=%s AND org_id=%s",
                tuple(params),
            )
        conn.commit()
    finally:
        conn.close()
    return get_objective(oid, org_id)


def delete_objective(oid, org_id) -> int:
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "DELETE FROM strategic_objectives WHERE id=%s AND org_id=%s", (oid, org_id)
            )
            deleted = cur.rowcount
            # Cascade in app layer (no hard FKs): drop child programs/projects.
            cur.execute("DELETE FROM programs WHERE objective_id=%s AND org_id=%s", (oid, org_id))
            cur.execute("DELETE FROM projects WHERE objective_id=%s AND org_id=%s", (oid, org_id))
        conn.commit()
    finally:
        conn.close()
    return deleted


# Programs ---------------------------------------------------------------------

def create_program(org_id, owner_user_id, created_by, data) -> dict:
    pid = str(uuid.uuid4())
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "INSERT INTO programs "
                "(id, objective_id, owner_user_id, org_id, created_by, name, description, status, start_date, target_date) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    pid, data.get("objective_id"), owner_user_id, org_id, created_by,
                    data.get("name"), data.get("description"),
                    data.get("status", "draft"),
                    _date(data.get("start_date")), _date(data.get("target_date")),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return get_program(pid, org_id)


def list_programs(org_id, objective_id=None) -> list:
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            if objective_id:
                cur.execute(
                    f"SELECT {_PROGRAM_COLS} FROM programs "
                    "WHERE org_id=%s AND objective_id=%s ORDER BY created_at DESC",
                    (org_id, objective_id),
                )
            else:
                cur.execute(
                    f"SELECT {_PROGRAM_COLS} FROM programs WHERE org_id=%s ORDER BY created_at DESC",
                    (org_id,),
                )
            return [_ser(r) for r in (cur.fetchall() or [])]
    finally:
        conn.close()


def get_program(pid, org_id) -> dict | None:
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT {_PROGRAM_COLS} FROM programs WHERE id=%s AND org_id=%s", (pid, org_id)
            )
            row = cur.fetchone()
            return _ser(row) if row else None
    finally:
        conn.close()


def update_program(pid, org_id, data) -> dict | None:
    fields, params = _build_update(
        data, ["name", "description", "status", "objective_id"],
        date_fields=["start_date", "target_date"],
    )
    if data.get("owner_user_id"):
        fields.append("owner_user_id=%s")
        params.append(data["owner_user_id"])
    if not fields:
        return get_program(pid, org_id)
    params.extend([pid, org_id])
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"UPDATE programs SET {', '.join(fields)} WHERE id=%s AND org_id=%s",
                tuple(params),
            )
        conn.commit()
    finally:
        conn.close()
    return get_program(pid, org_id)


def delete_program(pid, org_id) -> int:
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("DELETE FROM programs WHERE id=%s AND org_id=%s", (pid, org_id))
            deleted = cur.rowcount
            # Detach projects from the deleted program (keep them under the objective).
            cur.execute("UPDATE projects SET program_id=NULL WHERE program_id=%s AND org_id=%s", (pid, org_id))
        conn.commit()
    finally:
        conn.close()
    return deleted


# Projects ---------------------------------------------------------------------

def create_project(org_id, owner_user_id, created_by, data) -> dict:
    pid = str(uuid.uuid4())
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "INSERT INTO projects "
                "(id, objective_id, program_id, owner_user_id, org_id, created_by, name, description, status, start_date, target_date) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    pid, data.get("objective_id"), data.get("program_id") or None,
                    owner_user_id, org_id, created_by,
                    data.get("name"), data.get("description"),
                    data.get("status", "draft"),
                    _date(data.get("start_date")), _date(data.get("target_date")),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return get_project(pid, org_id)


def list_projects(org_id, objective_id=None, program_id=None) -> list:
    clauses = ["org_id=%s"]
    params = [org_id]
    if objective_id:
        clauses.append("objective_id=%s")
        params.append(objective_id)
    if program_id:
        clauses.append("program_id=%s")
        params.append(program_id)
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT {_PROJECT_COLS} FROM projects WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC",
                tuple(params),
            )
            return [_ser(r) for r in (cur.fetchall() or [])]
    finally:
        conn.close()


def get_project(pid, org_id) -> dict | None:
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"SELECT {_PROJECT_COLS} FROM projects WHERE id=%s AND org_id=%s", (pid, org_id)
            )
            row = cur.fetchone()
            return _ser(row) if row else None
    finally:
        conn.close()


def update_project(pid, org_id, data) -> dict | None:
    fields, params = _build_update(
        data, ["name", "description", "status", "objective_id", "program_id"],
        date_fields=["start_date", "target_date"],
    )
    if data.get("owner_user_id"):
        fields.append("owner_user_id=%s")
        params.append(data["owner_user_id"])
    if not fields:
        return get_project(pid, org_id)
    params.extend([pid, org_id])
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                f"UPDATE projects SET {', '.join(fields)} WHERE id=%s AND org_id=%s",
                tuple(params),
            )
        conn.commit()
    finally:
        conn.close()
    return get_project(pid, org_id)


def delete_project(pid, org_id) -> int:
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("DELETE FROM projects WHERE id=%s AND org_id=%s", (pid, org_id))
            deleted = cur.rowcount
            cur.execute("DELETE FROM project_doc_links WHERE project_id=%s", (pid,))
            cur.execute("DELETE FROM project_tracker_links WHERE project_id=%s", (pid,))
        conn.commit()
    finally:
        conn.close()
    return deleted


def _build_update(data, text_fields, date_fields=None):
    """Build a partial-UPDATE SET clause from provided keys only."""
    fields, params = [], []
    for f in text_fields:
        if f in data:
            fields.append(f"{f}=%s")
            params.append(data[f])
    for f in date_fields or []:
        if f in data:
            fields.append(f"{f}=%s")
            params.append(_date(data[f]))
    return fields, params


# ── project ↔ doc / tracker links ─────────────────────────────────────────────

def link_doc(project_id, policy_id, doc_type) -> dict:
    lid = str(uuid.uuid4())
    dt = doc_type if doc_type in ("policy", "procedure", "standard") else "policy"
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "INSERT INTO project_doc_links (id, project_id, policy_id, doc_type) "
                "VALUES (%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE doc_type=VALUES(doc_type)",
                (lid, project_id, policy_id, dt),
            )
        conn.commit()
    finally:
        conn.close()
    return {"project_id": project_id, "policy_id": policy_id, "doc_type": dt}


def unlink_doc(project_id, policy_id) -> int:
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "DELETE FROM project_doc_links WHERE project_id=%s AND policy_id=%s",
                (project_id, policy_id),
            )
            deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return deleted


def link_tracker(project_id, tracker_id) -> dict:
    lid = str(uuid.uuid4())
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "INSERT INTO project_tracker_links (id, project_id, tracker_id, pinned) "
                "VALUES (%s,%s,%s,1) "
                "ON DUPLICATE KEY UPDATE pinned=1",
                (lid, project_id, tracker_id),
            )
        conn.commit()
    finally:
        conn.close()
    return {"project_id": project_id, "tracker_id": tracker_id, "pinned": True}


def unlink_tracker(project_id, tracker_id) -> int:
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "DELETE FROM project_tracker_links WHERE project_id=%s AND tracker_id=%s",
                (project_id, tracker_id),
            )
            deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return deleted


def _linked_docs(project_id, conn) -> list:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT policy_id, doc_type FROM project_doc_links WHERE project_id=%s",
            (project_id,),
        )
        return [dict(r) for r in (cur.fetchall() or [])]


def _pinned_trackers(project_id, conn) -> list:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT tracker_id FROM project_tracker_links WHERE project_id=%s",
            (project_id,),
        )
        return [r["tracker_id"] for r in (cur.fetchall() or [])]


def get_project_links(project_id) -> dict:
    """Return a project's linked docs, auto-surfaced trackers and pinned trackers.

    Auto-surfaced trackers are those already referencing any linked doc (via
    ``statement_tracker_refs``); pinned trackers are explicit. The UI shows both,
    distinguishing pinned from auto.
    """
    from services.statement_tracker_refs import get_trackers_for_policy

    conn = _conn()
    try:
        docs = _linked_docs(project_id, conn)
        pinned = set(_pinned_trackers(project_id, conn))
    finally:
        conn.close()

    auto = {}
    for d in docs:
        try:
            for t in get_trackers_for_policy(d["policy_id"]) or []:
                tid = t.get("tracker_id")
                if not tid:
                    continue
                entry = auto.setdefault(
                    tid,
                    {"tracker_id": tid, "tracker_abbrev": t.get("tracker_abbrev"),
                     "mapped_row_count": 0, "pinned": tid in pinned, "source": "auto"},
                )
                entry["mapped_row_count"] += int(t.get("mapped_row_count", 0) or 0)
                entry["pinned"] = entry["pinned"] or tid in pinned
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("get_project_links: tracker lookup failed for %s: %s", d.get("policy_id"), exc)

    # Pinned trackers that reference no linked doc still appear (source=pinned).
    for tid in pinned:
        if tid not in auto:
            auto[tid] = {"tracker_id": tid, "tracker_abbrev": None,
                         "mapped_row_count": 0, "pinned": True, "source": "pinned"}

    return {"docs": docs, "trackers": list(auto.values())}


def _project_refs(project_id, conn) -> list:
    """All statement_tracker_refs across a project's linked docs."""
    from services.statement_tracker_refs import get_refs_for_policy

    refs = []
    for d in _linked_docs(project_id, conn):
        try:
            for r in get_refs_for_policy(d["policy_id"]) or []:
                rr = dict(r)
                rr.setdefault("doc_type", d.get("doc_type"))
                refs.append(rr)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("_project_refs: refs lookup failed for %s: %s", d.get("policy_id"), exc)
    return refs


def compute_project_health(project_id) -> dict:
    conn = _conn()
    try:
        refs = _project_refs(project_id, conn)
    finally:
        conn.close()
    return _rollup.rollup_status(refs)


def compute_objective_health(objective_id, org_id) -> dict:
    """Roll an objective's health up from all its projects (under programs and
    attached directly to the objective)."""
    projects = list_projects(org_id, objective_id=objective_id)
    summaries = [compute_project_health(p["id"]) for p in projects]
    return _rollup.aggregate_rollups(summaries)


# ── milestones ────────────────────────────────────────────────────────────────

def create_milestone(parent_type, parent_id, data) -> dict:
    if parent_type not in ("objective", "program", "project"):
        raise ValueError("invalid parent_type")
    mid = str(uuid.uuid4())
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "INSERT INTO strategy_milestones (id, parent_type, parent_id, title, due_date, status) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (mid, parent_type, parent_id, data.get("title"),
                 _date(data.get("due_date")), data.get("status", "planned")),
            )
        conn.commit()
    finally:
        conn.close()
    return {"id": mid, "parent_type": parent_type, "parent_id": parent_id,
            "title": data.get("title"), "due_date": _date(data.get("due_date")),
            "status": data.get("status", "planned")}


def delete_milestone(mid) -> int:
    conn = _conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("DELETE FROM strategy_milestones WHERE id=%s", (mid,))
            deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return deleted


def _milestones_for(parent_ids, conn) -> dict:
    """Return {parent_id: [milestone, ...]} for the given parent ids."""
    if not parent_ids:
        return {}
    placeholders = ",".join(["%s"] * len(parent_ids))
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT id, parent_type, parent_id, title, due_date, status "
            f"FROM strategy_milestones WHERE parent_id IN ({placeholders}) "
            "ORDER BY due_date",
            tuple(parent_ids),
        )
        out = {}
        for r in cur.fetchall() or []:
            out.setdefault(r["parent_id"], []).append(_ser(r))
        return out


# ── roadmap (whole tree + bottom-up health) ───────────────────────────────────

def get_roadmap(org_id) -> dict:
    objectives = list_objectives(org_id)
    programs = list_programs(org_id)
    projects = list_projects(org_id)

    # Per-project health.
    health_by_project = {p["id"]: compute_project_health(p["id"]) for p in projects}

    # Milestones for every node.
    all_ids = [o["id"] for o in objectives] + [p["id"] for p in programs] + [p["id"] for p in projects]
    conn = _conn()
    try:
        milestones = _milestones_for(all_ids, conn)
    finally:
        conn.close()

    programs_by_obj = {}
    for pr in programs:
        pr = dict(pr)
        pr["milestones"] = milestones.get(pr["id"], [])
        pr["projects"] = []
        programs_by_obj.setdefault(pr["objective_id"], []).append(pr)
    program_index = {pr["id"]: pr for prs in programs_by_obj.values() for pr in prs}

    direct_projects_by_obj = {}
    for prj in projects:
        prj = dict(prj)
        prj["milestones"] = milestones.get(prj["id"], [])
        prj["health"] = health_by_project.get(prj["id"])
        if prj.get("program_id") and prj["program_id"] in program_index:
            program_index[prj["program_id"]]["projects"].append(prj)
        else:
            direct_projects_by_obj.setdefault(prj["objective_id"], []).append(prj)

    tree = []
    for obj in objectives:
        obj = dict(obj)
        obj_programs = programs_by_obj.get(obj["id"], [])
        obj_direct = direct_projects_by_obj.get(obj["id"], [])
        obj["milestones"] = milestones.get(obj["id"], [])
        obj["programs"] = obj_programs
        obj["direct_projects"] = obj_direct

        for pr in obj_programs:
            pr["health"] = _rollup.aggregate_rollups([p.get("health") for p in pr["projects"]])

        child_summaries = [pr["health"] for pr in obj_programs] + [p.get("health") for p in obj_direct]
        obj["health"] = _rollup.aggregate_rollups(child_summaries)
        tree.append(obj)

    return {"objectives": tree, "generated_at": datetime.utcnow().isoformat() + "Z"}


# ── drill-down (root-cause path for a project) ─────────────────────────────────

def get_drilldown(project_id, org_id) -> dict | None:
    project = get_project(project_id, org_id)
    if not project:
        return None

    objective = get_objective(project.get("objective_id"), org_id) if project.get("objective_id") else None
    program = get_program(project.get("program_id"), org_id) if project.get("program_id") else None

    conn = _conn()
    try:
        refs = _project_refs(project_id, conn)
    finally:
        conn.close()

    project_meta = {
        "id": project["id"],
        "name": project.get("name"),
        "objective_id": project.get("objective_id"),
        "objective_title": objective.get("title") if objective else None,
        "program_id": project.get("program_id"),
        "program_name": program.get("name") if program else None,
    }
    paths = _rollup.build_drilldown_paths(project_meta, {}, refs)
    return {
        "project": project_meta,
        "summary": _rollup.rollup_status(refs),
        "paths": paths,
    }
