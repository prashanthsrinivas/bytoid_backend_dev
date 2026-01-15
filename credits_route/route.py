from flask import Blueprint, request, jsonify, session, redirect
from db.rds_db import connect_to_rds
from db.db_checkers import check_onboarding_user
import uuid
import os
from services.credit_system import CreditManager
from services.redis_service import RedisService
import json
from request_context import current_user_id
from db.rds_db import safe_execute
from utils.s3_utils import (
    upload_any_file,
    read_json_from_s3,
)
from utils.normal import ensure_dir
from cust_helpers import pathconfig
from datetime import datetime, timezone, timedelta


# load_dotenv()  # Load from .env into environment variables
credits_bp = Blueprint("credits", __name__)

# Credit mapping based on pricing tiers
PLAN_CREDITS = {
    "Bytoid™ Support for Consultants": {
        "monthly": [
            {"price_usd": 25, "credits": 250},
            {"price_usd": 35, "credits": 500},
        ],
        "yearly": [
            {"price_usd": 300, "credits": 3000},
            {"price_usd": 420, "credits": 6000},
        ],
    },
    "Bytoid™ Support - Part-time AI Worker": {
        "monthly": [
            {"price_usd": 50, "credits": 1000},
            {"price_usd": 75, "credits": 1500},
            {"price_usd": 100, "credits": 2500},
        ],
        "yearly": [
            {"price_usd": 600, "credits": 12000},
            {"price_usd": 900, "credits": 18000},
            {"price_usd": 1200, "credits": 30000},
        ],
    },
    "Bytoid™ Support - Full time AI Worker": {
        "monthly": [
            {"price_usd": 150, "credits": 5000},
            {"price_usd": 200, "credits": 7500},
        ],
        "yearly": [
            {"price_usd": 1800, "credits": 60000},
            {"price_usd": 2400, "credits": 90000},
        ],
    },
    "Bytoid™ Support - 24/7 AI Worker": {
        "monthly": [{"price_usd": 500, "credits": 15000}],
        "yearly": [{"price_usd": 6000, "credits": 180000}],
    },
}


# class Credits:
#     def __init__(self):
#         self.redis_client = RedisService()

#     async def update_ai_credits_redis(
#         self, credit_type: str, total_chars: int, user_id
#     ):

#         if not user_id:
#             # log and exit — never write under None
#             return

#         try:
#             await self.redis_client.hincrby(
#                 name=f"user_credits:{user_id}",
#                 key=credit_type,
#                 amount=total_chars,
#             )
#             credits = await self.redis_client.hgetall(f"user_credits:{user_id}")

#             print(f"redis  updated for credits")
#             # print(f"total_chars : {total_chars}")
#             # print("Final credits:", credits)

#             # ----------- update the s3 credits daily json file -------------

#             result = await self.store_daily_credits_to_s3(
#                 user_id, total_chars, credit_type
#             )

#         except Exception as e:
#             print(f"redis not updated for credits : {e} ")
#             raise

#     async def store_daily_credits_to_s3(
#         self, user_id: str, total_chars: int, credit_type: str
#     ):
#         """
#         Append credit event to daily JSON file in S3.
#         Path: credits/events/{user_id}/daily/YYYY/MM/DD.json
#         """

#         now = datetime.utcnow()
#         year = now.strftime("%Y")
#         month = now.strftime("%m")
#         day = now.strftime("%d")

#         credits_folder = os.path.join(pathconfig.basepath, "credits", user_id)
#         ensure_dir(credits_folder)

#         credits_daily_filepath = os.path.join(credits_folder, "daily_credits.json")

#         s3_key = f"credits/events/{user_id}/daily/" f"{year}/{month}/{day}.json"

#         s3_data = read_json_from_s3(s3_key)
#         if s3_data is None:
#             s3_data = {}

#         now = datetime.now(timezone.utc)
#         date_key = now.strftime("%Y-%m-%d")

#         new_record = {
#             "timestamp": now.isoformat().replace("+00:00", "Z"),
#             "credits": total_chars,
#             "credit_type": credit_type,
#         }

#         s3_data.setdefault("date", date_key)
#         s3_data.setdefault("events", []).append(new_record)

#         with open(credits_daily_filepath, "w", encoding="utf-8") as f:
#             json.dump(s3_data, f, indent=2)

#         result = upload_any_file(
#             credits_daily_filepath,
#             user_id,
#             type="credits",
#             s3_key_C=s3_key,
#         )

#         if result.get("status") == "success":
#             os.remove(credits_daily_filepath)
#             # print(f"s3 daily added")
#             # print(f"daily credits in s3: {s3_data}")

#         # ---------- uplaoding monthly credits -------------#

#         s3_key = f"credits/events/{user_id}/monthly/{year}/{month}.json"

#         s3_data = read_json_from_s3(s3_key)
#         if not isinstance(s3_data, dict):
#             s3_data = {}

#         # Update credits
#         s3_data[credit_type] = s3_data.get(credit_type, 0) + total_chars

#         credits_folder = os.path.join(pathconfig.basepath, "credits", user_id)
#         ensure_dir(credits_folder)

#         credits_monthly_filepath = os.path.join(credits_folder, "monthly_credits.json")

#         with open(credits_monthly_filepath, "w", encoding="utf-8") as f:
#             json.dump(s3_data, f, indent=2)

#         result = upload_any_file(
#             credits_monthly_filepath,
#             user_id,
#             type="credits",
#             s3_key_C=s3_key,
#         )

#         if result.get("status") == "success":
#             os.remove(credits_monthly_filepath)
#             # print(f"s3 monthly added")
#             # print(f"{s3_data}")

#         return {
#             "status": "ok",
#             "s3_key": s3_key,
#             "records_today": s3_data,
#         }


class Credits:
    """
    Backward-compatible adapter.
    Existing callers continue using update_ai_credits_redis()
    """

    CREDIT_MULTIPLIER = 0.25  # chars → credits

    def __init__(self):
        self.db = connect_to_rds()
        self.cm = CreditManager(self.db)

    # -------------------------------------------------
    # MAIN ENTRY (USED EVERYWHERE)
    # -------------------------------------------------
    async def update_ai_credits_redis(
        self,
        credit_type: str,
        total_chars: int,
        user_id: str,
        reference_id: str = None,
    ):
        """
        credit_type: normal | embedding | evaluator | etc
        total_chars: raw character count
        """

        if not user_id or not total_chars:
            return
        print("credit type", credit_type, reference_id)
        print("total chars we got", total_chars)
        # ✅ FIXED CREDIT CALCULATION
        credits_to_consume = int(total_chars * self.CREDIT_MULTIPLIER)
        print("total credits modified", credits_to_consume)

        if credits_to_consume <= 0:
            return

        try:
            self.cm.consume_credits(
                user_id=user_id,
                credits_needed=credits_to_consume,
                reason=credit_type.upper(),
                reference_id=reference_id or "AI_EXECUTION",
            )

        except Exception as e:
            # This will surface INSUFFICIENT_CREDITS correctly
            print(f"❌ Credit consumption failed: {e}")
            raise

        return {
            "status": "ok",
            "credit_type": credit_type,
            "chars": total_chars,
            "credits_used": credits_to_consume,
        }


@credits_bp.route("/load_credits_page_monthly", methods=["POST"])
async def load_credits_page():
    data = request.get_json() or {}
    user_id = data.get("user_id")
    start_date = data.get("start_date")
    end_date = data.get("end_date")

    if start_date == end_date:

        print("inside if part")
        start_month, start_year = map(int, start_date.split("/"))
        s3_key = (
            f"credits/events/{user_id}/monthly/" f"{start_year}/{start_month:02d}.json"
        )
        print(f"key : {s3_key}")
        s3_data = read_json_from_s3(s3_key)
        print(f"s3_data : {s3_data}")
        if s3_data is None:
            return {}
        else:
            return s3_data

    else:
        start_month, start_year = map(int, start_date.split("/"))
        end_month, end_year = map(int, end_date.split("/"))

        keys = []
        monthly_record = []

        for year in range(start_year, end_year + 1):
            sm = start_month if year == start_year else 1
            em = end_month if year == end_year else 12

            for month in range(sm, em + 1):
                keys.append(f"credits/events/{user_id}/monthly/{year}/{month:02d}.json")

        for key in keys:
            s3_data = read_json_from_s3(key)
            monthly_record.append(s3_data)

        print(f"monthly data: {monthly_record}")
        if monthly_record is None:
            return {}
        else:
            return monthly_record


def load_events_paginated(keys, cursor=None, limit=20):
    file_idx = cursor.get("file_index", 0) if cursor else 0
    event_idx = cursor.get("event_index", 0) if cursor else 0

    results = []

    while file_idx < len(keys):
        s3_data = read_json_from_s3(keys[file_idx])
        events = s3_data.get("events", []) if s3_data else []

        while event_idx < len(events):
            results.append(events[event_idx])
            event_idx += 1

            if len(results) == limit:
                return results, {"file_index": file_idx, "event_index": event_idx}

        # move to next file
        file_idx += 1
        event_idx = 0

    return results, None


def generate_daily_keys(user_id, start_date, end_date):
    keys = []

    start_dt = datetime.strptime(start_date, "%d/%m/%Y")
    end_dt = datetime.strptime(end_date, "%d/%m/%Y")

    if start_dt > end_dt:
        raise ValueError("start_date cannot be after end_date")

    current = start_dt
    while current <= end_dt:
        keys.append(
            f"credits/events/{user_id}/daily/"
            f"{current.year}/{current.month:02d}/{current.day:02d}.json"
        )
        current += timedelta(days=1)

    return keys


@credits_bp.route("/load_credits_page_daily", methods=["POST"])
async def load_credits_page_daily():
    data = request.get_json() or {}

    user_id = data.get("user_id")
    start_date = data.get("start_date")  # "DD/MM/YYYY"
    end_date = data.get("end_date")  # "DD/MM/YYYY"
    cursor = data.get("cursor")  # optional
    limit = 20

    if not all([user_id, start_date, end_date]):
        return {"error": "Missing required fields"}, 400

    # 1. Generate keys (pure logic, no S3)
    keys = generate_daily_keys(user_id, start_date, end_date)

    # 2. Load paginated events
    records, next_cursor = load_events_paginated(keys=keys, cursor=cursor, limit=limit)

    return {"data": records, "next_cursor": next_cursor, "limit": limit}


@credits_bp.route("/delete_redis_credits", methods=["POST"])
async def delete_redis_credits():
    data = request.get_json() or {}
    user_id = data.get("user_id")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    redis = RedisService()
    deleted = await redis.delete(f"user_credits:{user_id}")

    return jsonify({"status": "ok", "deleted": bool(deleted), "user_id": user_id}), 200


def get_credits_for_plan(plan_name, billing_type="monthly", tier_index=0):
    """Get credits for a specific plan, billing type, and pricing tier"""
    if plan_name in PLAN_CREDITS:
        plan_data = PLAN_CREDITS[plan_name]
        if billing_type in plan_data and plan_data[billing_type]:
            if 0 <= tier_index < len(plan_data[billing_type]):
                return plan_data[billing_type][tier_index]["credits"]
            else:
                return plan_data[billing_type][0]["credits"]  # Default to first tier
    return 0


def validate_plan(plan_name):
    """Validate if plan name exists"""
    return plan_name in PLAN_CREDITS


def get_plan_details(plan_name):
    """Get detailed information for a specific plan"""
    if plan_name in PLAN_CREDITS:
        return {
            "name": plan_name,
            "billing_options": PLAN_CREDITS[plan_name],
            "available_tiers": {
                "monthly": len(PLAN_CREDITS[plan_name].get("monthly", [])),
                "yearly": len(PLAN_CREDITS[plan_name].get("yearly", [])),
            },
        }
    return None


# @credits_bp.route("/api/plans/<plan_name>", methods=["GET"])
# def get_plan_details_route(plan_name):
#     """Get specific plan details and pricing tiers"""
#     try:
#         plan_details = get_plan_details(plan_name)

#         if not plan_details:
#             return jsonify({"success": False, "message": "Plan not found"}), 404

#         return jsonify({"success": True, "plan": plan_details})

#     except Exception as e:
#         return jsonify({"success": False, "message": str(e)}), 500


# @credits_bp.route("/api/plans/calculate-credits", methods=["POST"])
# def calculate_credits():
#     """Calculate credits for a specific plan and pricing tier"""
#     try:
#         data = request.get_json()
#         plan_name = data.get("planName")
#         billing_type = data.get("billingType", "monthly")
#         tier_index = data.get("tierIndex", 0)

#         if not plan_name:
#             return jsonify({"success": False, "message": "Plan name required"}), 400

#         if not validate_plan(plan_name):
#             return jsonify({"success": False, "message": "Invalid plan name"}), 400

#         credits = get_credits_for_plan(plan_name, billing_type, tier_index)
#         plan_data = (
#             PLAN_CREDITS[plan_name][billing_type][tier_index]
#             if tier_index < len(PLAN_CREDITS[plan_name][billing_type])
#             else PLAN_CREDITS[plan_name][billing_type][0]
#         )

#         return jsonify(
#             {
#                 "success": True,
#                 "plan_name": plan_name,
#                 "billing_type": billing_type,
#                 "tier_index": tier_index,
#                 "credits": credits,
#                 "price_usd": plan_data["price_usd"],
#             }
#         )

#     except Exception as e:
#         return jsonify({"success": False, "message": str(e)}), 500


# @credits_bp.route("/api/plans/store-selection", methods=["POST"])
# def store_plan_selection():
#     """Store plan selection in the plans table"""
#     try:
#         data = request.get_json()
#         plan_name = data.get("planName")
#         billing_type = data.get("billingType", "monthly")
#         tier_index = data.get("tierIndex", 0)
#         subscribe_id = data.get("subscribeId", str(uuid.uuid4()))
#         add_ons = data.get("addOns", [])
#         add_ons_measurement = data.get("addOnsMeasurement", {})

#         print(
#             data,
#             plan_name,
#             billing_type,
#             tier_index,
#             subscribe_id,
#             add_ons,
#             add_ons_measurement,
#         )

#         if not plan_name:
#             return jsonify({"success": False, "message": "Plan name required"}), 400

#         if not validate_plan(plan_name):
#             return jsonify({"success": False, "message": "Invalid plan name"}), 400

#         credits = get_credits_for_plan(plan_name, billing_type, tier_index)
#         plan_id = str(uuid.uuid4())

#         conn = get_db_connection()
#         cursor = conn.cursor()

#         cursor.execute(
#             """
#             INSERT INTO plans (plans_id, subscribe_id, plans, credits, `add-ons`, `add-ons-measurement`, created_in, updated_in)
#             VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
#         """,
#             (
#                 plan_id,
#                 subscribe_id,
#                 plan_name,
#                 str(credits),
#                 add_ons,
#                 add_ons_measurement,
#                 datetime.datetime.now(),
#                 datetime.datetime.now(),
#             ),
#         )

#         conn.commit()
#         conn.close()

#         return jsonify(
#             {
#                 "success": True,
#                 "message": "Plan selection stored successfully",
#                 "plan_id": plan_id,
#                 "subscribe_id": subscribe_id,
#                 "plan_name": plan_name,
#                 "billing_type": billing_type,
#                 "tier_index": tier_index,
#                 "credits": credits,
#             }
#         )

#     except Exception as e:
#         return jsonify({"success": False, "message": str(e)}), 500


# @credits_bp.route("/api/plans/available", methods=["GET"])
# def get_available_plans():
#     """Get all available plans and their credit mappings"""
#     return jsonify({"success": True, "plans": PLAN_CREDITS})


# @credits_bp.route("/api/plans/by-subscription/<subscribe_id>", methods=["GET"])
# def get_plans_by_subscription(subscribe_id):
#     """Get plans for a specific subscription"""
#     try:
#         conn = get_db_connection()
#         cursor = conn.cursor()

#         cursor.execute(
#             """
#             SELECT plans_id, subscribe_id, plans, credits, `add-ons`, `add-ons-measurement`, created_in, updated_in
#             FROM plans
#             WHERE subscribe_id = %s
#             ORDER BY created_in DESC
#         """,
#             (subscribe_id,),
#         )

#         plans = cursor.fetchall()
#         conn.close()

#         # Convert MySQL results to dict format
#         plans_dict = []
#         for plan in plans:
#             if cursor.description:
#                 plan_dict = {}
#                 for i, column in enumerate(cursor.description):
#                     plan_dict[column[0]] = plan[i]
#                 plans_dict.append(plan_dict)

#         return jsonify(
#             {"success": True, "subscribe_id": subscribe_id, "plans": plans_dict}
#         )

#     except Exception as e:
#         return jsonify({"success": False, "message": str(e)}), 500


def update_ai_credits_to_db(user_id: str, credit_type: str, total_chars: int):
    """
    Updates AI usage credits stored in JSON column `credits`.

    credits format:
    {
        "text_to_audio": 123,
        "audio_to_text": 456,
        "embedding": 789,
        "Normal": 100,
        "evaluator": 50,
        "ai_suggest": 25
    }
    """
    connection = connect_to_rds()

    print(f"called update_ai_credits:")
    print(f"user_id : {user_id}")
    print(f"credit_type : {credit_type}")
    print(f"total_chars : {total_chars}")

    query = """
        UPDATE users
        SET credits = JSON_SET(
            COALESCE(credits, '{}'),
            CONCAT('$.', %s),
            COALESCE(
                JSON_EXTRACT(credits, CONCAT('$.', %s)),
                0
            ) + %s
        )
        WHERE user_id = %s
    """

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            (
                credit_type,
                credit_type,
                total_chars,
                user_id,
            ),
        )

    connection.commit()

    cursor.close()
    connection.close()


# ====================================================
# 1. GET TOTAL CREDIT BALANCE (DASHBOARD / PREFLIGHT)
# ====================================================
@credits_bp.route("/credits", methods=["GET"])
def get_credits():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    conn = connect_to_rds()
    cm = CreditManager(conn)
    next_expiry = None

    balance = cm.get_credit_balance(user_id)
    if not balance:

        cur = conn.cursor()  # no dictionary=True
        cur.execute(
            """
            SELECT
                (credits_total - credits_used) AS remaining,
                expires_at,
                source_type
            FROM credit_buckets
            WHERE user_id = %s
            AND is_expired = 0
            AND expires_at IS NOT NULL
            ORDER BY expires_at ASC
            LIMIT 1
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if row:
            next_expiry = {
                "remaining": row[0],
                "expires_at": row[1].isoformat() if row[1] else None,
                "source_type": row[2],
            }

    cur.close()
    conn.close()

    return jsonify(
        {
            "user_id": user_id,
            "total_credits": balance["total"],
            "breakdown": balance["breakdown"],
            "next_expiry": next_expiry,
        }
    )


# ====================================================
# 2. FAST CREDIT CHECK (USED BEFORE AI EXECUTION)
# ====================================================
@credits_bp.route("/credits/check", methods=["GET"])
def check_credits():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    conn = connect_to_rds()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT COALESCE(SUM(credits_total - credits_used), 0)
        FROM credit_buckets
        WHERE user_id = %s
          AND is_expired = 0
        """,
        (user_id,),
    )

    total = cur.fetchone()[0]
    cur.close()
    conn.close()

    return jsonify({"has_credits": total > 0, "total_credits": total})


# ====================================================
# 3. GET CREDIT BUCKETS (DEBUG / ADMIN / SUPPORT)
# ====================================================
@credits_bp.route("/credits/buckets", methods=["GET"])
def get_credit_buckets():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    conn = connect_to_rds()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            bucket_id,
            source_type,
            credits_total,
            credits_used,
            (credits_total - credits_used) AS remaining,
            expires_at,
            created_at
        FROM credit_buckets
        WHERE user_id = %s
          AND is_expired = 0
        ORDER BY expires_at ASC
        """,
        (user_id,),
    )

    buckets = cur.fetchall()
    cur.close()
    conn.close()

    return jsonify({"buckets": buckets})


# ====================================================
# 4. GET CREDIT USAGE HISTORY
# ====================================================
@credits_bp.route("/credits/usage", methods=["GET"])
def get_credit_usage():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))

    conn = connect_to_rds()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            u.created_at AS used_at,
            u.credits_used,
            u.reason,
            u.reference_id,
            b.source_type
        FROM credit_usage_log u
        JOIN credit_buckets b ON u.bucket_id = b.bucket_id
        WHERE u.user_id = %s
        ORDER BY u.created_at DESC
        LIMIT %s OFFSET %s
        """,
        (user_id, limit, offset),
    )

    usage = cur.fetchall()
    cur.close()
    conn.close()

    return jsonify({"usage": usage, "limit": limit, "offset": offset})


# ====================================================
# 5. GET CREDIT SUMMARY (FOR BILLING / UI)
# ====================================================
@credits_bp.route("/credits/summary", methods=["GET"])
def credit_summary():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    conn = connect_to_rds()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            source_type,
            SUM(credits_total) AS total,
            SUM(credits_used) AS used,
            SUM(credits_total - credits_used) AS remaining
        FROM credit_buckets
        WHERE user_id = %s
          AND is_expired = 0
        GROUP BY source_type
        """,
        (user_id,),
    )

    summary = cur.fetchall()
    cur.close()
    conn.close()

    return jsonify({"user_id": user_id, "summary": summary})
