import json
from flask import Blueprint, request, jsonify
from .ws_instance import ws_service

ws_bp = Blueprint("ws_bytoid", __name__)


@ws_bp.route("/ws", methods=["POST"])
async def websocket_handler():
    try:
        data = request.json or {}

        route = data.get("requestContext", {}).get("routeKey")
        connection_id = data.get("requestContext", {}).get("connectionId")

        # -------- CONNECT --------
        if route == "$connect":
            return jsonify({"statusCode": 200})

        # -------- DISCONNECT --------
        elif route == "$disconnect":
            if connection_id:
                await ws_service.delete_connection(connection_id)
            return jsonify({"statusCode": 200})

        # -------- REGISTER --------
        elif route == "register":
            body = json.loads(data.get("body", "{}"))

            user_id = body.get("user_id")
            session_id = body.get("session_id")

            if not all([connection_id, user_id, session_id]):
                return jsonify({"statusCode": 400, "error": "Missing fields"})

            await ws_service.store_connection(user_id, session_id, connection_id)

            return jsonify({"statusCode": 200})

        # -------- CALL SIGNALING (WebRTC) --------
        # Relay in-house audio-call signaling between participants of a thread.
        # Body: {from_user, to_user?, thread_id, call_id?, kind, data?}
        #   to_user set   -> targeted (offer/answer/ice)
        #   to_user unset -> broadcast to the thread's other participants (join/leave)
        elif route == "signal":
            body = json.loads(data.get("body", "{}"))
            from_user = body.get("from_user")
            to_user = body.get("to_user")
            thread_id = body.get("thread_id")
            kind = body.get("kind")
            if not from_user or not kind:
                return jsonify({"statusCode": 400, "error": "Missing fields"})

            # Authorize the sender as a participant of the thread.
            try:
                from assessment_chat.service import _require_participant, list_participants
                if thread_id:
                    _require_participant(thread_id, from_user)
            except Exception:
                return jsonify({"statusCode": 403, "error": "not a participant"})

            extra = {
                "thread_id": thread_id,
                "call_id": body.get("call_id"),
                "from_user": from_user,
                "kind": kind,
                "data": body.get("data") or {},
                "event": kind,
            }

            if to_user:
                targets = [to_user]
            elif thread_id:
                try:
                    targets = [
                        p["user_id"] for p in list_participants(thread_id)
                        if p.get("user_id") and p.get("user_id") != from_user
                    ]
                except Exception:
                    targets = []
            else:
                targets = []

            for uid in targets:
                try:
                    await ws_service.emit(
                        user_id=uid, message="", scope="user",
                        msg_type="call_signal", feature="call_signal", extra=extra,
                    )
                except Exception:
                    pass
            return jsonify({"statusCode": 200})

        return jsonify({"statusCode": 400, "error": "Unknown route"})

    except Exception as e:
        return jsonify({"statusCode": 500, "error": str(e)})


# @ws_bp.route("/ws/connect", methods=["POST"])
# def ws_connect():

#     data = request.get_json(silent=True)

#     if data:
#         route = data.get("requestContext", {}).get("routeKey")
#         connection_id = data.get("requestContext", {}).get("connectionId")

#         print("\nROUTE:", route)
#         print("CONNECTION:", connection_id)

#     print("===== END DEBUG =====\n")

#     return {"statusCode": 200}


@ws_bp.route("/ws/connect", methods=["POST"])
def ws_connect():
    print("CONNECT HIT")
    # print("HEADERS:", dict(request.headers))
    # print("RAW:", request.data)

    return "", 200


@ws_bp.route("/ws/disconnect", methods=["POST"])
async def ws_disconnect():
    try:
        print("DISCONNECT HIT")
        # print("HEADERS:", dict(request.headers))
        # print("RAW:", request.data)
        data = request.json or {}

        connection_id = data.get("requestContext", {}).get("connectionId")

        if connection_id:
            await ws_service.delete_connection(connection_id)

        return {"statusCode": 200}

    except Exception as e:
        return {"statusCode": 500, "error": str(e)}


@ws_bp.route("/ws/register", methods=["POST"])
async def ws_register():
    try:
        print("REGISTER HIT")

        data = request.get_json(silent=True) or {}

        # print("RAW:", request.data)

        request_context = data.get("requestContext", {})
        connection_id = request_context.get("connectionId")

        body = data.get("body", {})  # already dict

        user_id = body.get("user_id")
        session_id = body.get("session_id")

        # print("connection_id:", connection_id)
        # print("user_id:", user_id)
        # print("session_id:", session_id)

        if not all([connection_id, user_id, session_id]):
            return {"statusCode": 400, "error": "Missing fields"}

        await ws_service.store_connection(user_id, session_id, connection_id)

        return {"statusCode": 200}

    except Exception as e:
        return {"statusCode": 500, "error": str(e)}


@ws_bp.route("/ws/test-send", methods=["POST"])
def test_send_message():
    import threading
    import random
    import asyncio
    import time

    async def send_messages_background(user_id, session_id, scope):
        messages = [
            ("init", "Starting process...", 10),
            ("fetch", "Fetching data...", 30),
            ("process_1", "Processing step 1...", 50),
            ("process_2", "Processing step 2...", 70),
            ("finalizing", "Almost done...", 90),
            ("completed", "Completed successfully 🎉", 100),
        ]

        for stage, msg, progress in messages:
            print(f"[WS TEST] Sending: {msg}")

            await ws_service.emit(
                user_id=user_id,
                message=msg,
                scope=scope,  # 🔥 key change
                session_id=session_id,
                job_id="test_job_123" if scope == "job" else None,
                msg_type="progress" if progress < 100 else "success",
                stage=stage,
                progress=progress,
                feature="test",
            )

            await asyncio.sleep(random.uniform(0.5, 1.5))

    def runner(user_id, session_id, scope):
        asyncio.run(send_messages_background(user_id, session_id, scope))

    try:
        data = request.get_json(silent=True) or {}

        user_id = data.get("user_id")
        session_id = data.get("session_id")
        scope = data.get("scope", "session")  # default session

        if not user_id:
            return jsonify({"error": "user_id required"}), 400

        if scope in ["session", "job"] and not session_id:
            return jsonify({"error": "session_id required for this scope"}), 400

        print("TEST WS →", user_id, session_id, scope)

        threading.Thread(
            target=runner,
            args=(user_id, session_id, scope),
            daemon=True,
        ).start()

        return jsonify(
            {
                "status": "started",
                "scope": scope,
                "note": "Messages are being streamed via WebSocket",
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500
