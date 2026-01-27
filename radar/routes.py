import pymysql
from agent_route.doc_clarity import QueryData
from apiConnector.helpers import _execute_endpoint_internal
from db.db_checkers import get_notes_data
from db.lance_db_service import LanceDBServer
from flask import Blueprint, jsonify, request
from db.rds_db import connect_to_rds
import uuid
from umail.routes import get_sorted_lance_emails
from utils.fireworkzz import get_firework_embedding
import os

radar_bp = Blueprint("radar", __name__)


@radar_bp.route("/radar/apps/list/<userid>", methods=["GET"])
def radarapp(userid):
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    cur.execute(
        """
        SELECT 
            a.id AS app_id,
            a.app_name,

            e.id   AS endpoint_id,
            e.name,
            e.path,
            e.updated_at

        FROM external_apps a
        LEFT JOIN external_app_endpoints e
            ON a.id = e.app_id
        WHERE a.user_id = %s
        ORDER BY a.id, e.id
    """,
        (userid,),
    )

    rows = cur.fetchall()
    apps = {}

    for row in rows:
        app_id = row["app_id"]

        if app_id not in apps:
            apps[app_id] = {"id": app_id, "app_name": row["app_name"], "endpoints": []}

        # Only add endpoint if it exists
        if row["endpoint_id"] is not None:
            endpoint = {
                "id": row["endpoint_id"],
                "name": row["name"],
                "path": row["path"],
                "updated_at": row["updated_at"],
            }

            apps[app_id]["endpoints"].append(endpoint)

    return jsonify(list(apps.values()))


async def retreval_from_sources(
    conn, dbserver, main_source, datasources, userid, user_analyze_input
):

    embedding = await get_firework_embedding()
    data_for_review = []
    # -------------------------
    # APP SOURCE
    # -------------------------
    if main_source == "app":
        endpoint_ids = datasources.get("endpoint_ids", [])

        for endpoint_id in endpoint_ids:
            try:
                result = await _execute_endpoint_internal(
                    endpoint_id=endpoint_id,
                    userid=userid,
                )
                data_for_review.append(
                    {
                        "type": "app",
                        "endpoint_id": endpoint_id,
                        "data": result.get("response"),
                    }
                )
            except Exception as e:
                data_for_review.append({"endpoint_id": endpoint_id, "error": str(e)})

    # -------------------------
    # NOTES SOURCE
    # -------------------------
    elif main_source == "notes":
        note_ids = datasources.get("note_ids", [])
        all_notes = get_notes_data(userid)  # expect list[ {note_id, content, ...} ]
        # print("len of all_notes", len(all_notes), all_notes)
        for note in all_notes.get("notes"):
            # print("type of note", type(note), note)
            if note.get("note_id") in note_ids:
                data_for_review.append(
                    {"type": "notes", "note_id": note.get("note_id"), "data": note}
                )

    # -------------------------
    # EMAIL SOURCE
    # -------------------------
    elif main_source == "emails":
        client_ids = datasources.get("client_ids", [])
        for i in client_ids:
            data_for_review.append(
                {
                    "type": "emails",
                    "clientid": i,
                    "data": get_sorted_lance_emails(
                        connection=conn, user_id=userid, client_id=i
                    ),
                }
            )
        # all_emails = get_emails_data(userid)

    # -------------------------
    # KNOWLEDGE SOURCE (LanceDB / Docs)
    # -------------------------
    elif main_source == "knowledge":
        vector = embedding.embed_query(user_analyze_input)
        payload = QueryData(
            user_id=userid,
            embedding=vector,
            top_k=1,
        )
        print("len of vectror", len(vector))

        filenames = datasources.get("filenames", [])
        for file in filenames:
            if file.get("type") == "docs":
                fname = file.get("filename")
                results = await dbserver.query_vector_filename(
                    query=payload, filename=fname
                )
                if results:
                    data_for_review.append(
                        {
                            "type": "docs",
                            "source": fname,
                            "data": results[0].get("text", ""),
                        }
                    )
            if file.get("type") == "aud":
                bfname = file.get("filename")
                base = os.path.basename(bfname)
                name_without_ext = os.path.splitext(base)[0]

                # Step 3: add transcript suffix
                fname = f"{name_without_ext}_transcript.json"

                print("fname for checking", fname)

                results = await dbserver.rec_query_vector_foldername(
                    query=payload, foldername=fname
                )
                if results:
                    data_for_review.append(
                        {
                            "type": "audio",
                            "source": fname,
                            "data": results[0].get("text"),
                        }
                    )

    return data_for_review


@radar_bp.route("/radar/review", methods=["POST"])
async def radar_review():
    data = request.get_json(force=True)

    userid = data.get("userid")
    user_analyze_input = data.get("analyze_input")
    main_source = data.get("main_source")  # "app", "notes", "emails", "knowledge"
    datasources = data.get("data_sources", {})
    reference_sources = data.get("reference_sources")

    if not userid:
        return jsonify({"error": "userid is required"}), 400

    data_for_review = []
    conn = connect_to_rds()
    dbserver = LanceDBServer()
    data_checked = await retreval_from_sources(
        conn, dbserver, main_source, datasources, userid, user_analyze_input
    )

    # -------------------------
    # FINAL RESPONSE
    # -------------------------
    return jsonify(data_checked)


@radar_bp.route("/radar/execute", methods=["POST"])
def radar_execute():
    data = request.get_json(force=True)
    userid = data.get("userid")

    if not userid:
        return jsonify({"error": "userid is required"}), 400

    return jsonify(
        {
            "userid": userid,
            "action": "execute",
            "status": "success",
            "message": "Workflow execution triggered",
            "execution_id": str(uuid.uuid4()),
        }
    )


@radar_bp.route("/radar/insights", methods=["POST"])
def radar_insights():
    data = request.get_json(force=True)
    userid = data.get("userid")

    if not userid:
        return jsonify({"error": "userid is required"}), 400

    return jsonify(
        {
            "userid": userid,
            "action": "insights",
            "status": "success",
            "insights": [
                "Your Stripe app is being used the most",
                "You have unused endpoints in Shopify",
                "API failure rate dropped by 23%",
            ],
        }
    )
