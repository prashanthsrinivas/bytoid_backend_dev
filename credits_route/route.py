from flask import Blueprint, request, jsonify
from db.rds_db import connect_to_rds
import pymysql
from services.credit_system import CreditManager, InsufficientCreditsError
from datetime import datetime, timedelta
from utils.app_configs import ACCESSIBLE_IDS

# load_dotenv()  # Load from .env into environment variables
credits_bp = Blueprint("credits", __name__)


class Credits:
    CREDIT_MULTIPLIER = 0.25

    def __init__(self, db=None):
        self.db = db or connect_to_rds()
        self.cm = CreditManager(self.db)
        self.owns_db = db is None

    def get_db(self):
        """
        Ensure DB connection is alive before use.
        """
        try:
            if not self.db:
                self.db = connect_to_rds()

            self.db.ping(reconnect=True)

        except Exception:
            self.db = connect_to_rds()

        return self.db

    # -------------------------------------------------
    # READ-ONLY CREDIT CHECK (OPTIONAL PREFLIGHT)
    # -------------------------------------------------
    async def has_ai_credits(self, total_chars: int, user_id: str) -> bool:

        if not user_id or not total_chars:
            return False

        credits_needed = int(total_chars * self.CREDIT_MULTIPLIER)

        if credits_needed <= 0:
            return True

        # 🔹 Refresh DB connection
        self.db = self.get_db()
        self.cm.db = self.db

        return await self.cm.has_sufficient_credits(
            user_id=user_id,
            needed=credits_needed,
        )

    # -------------------------------------------------
    # CONSUME CREDITS (AUTHORITATIVE)
    # -------------------------------------------------
    async def update_ai_credits_redis(
        self,
        credit_type: str,
        total_chars: int,
        user_id: str,
        reference_id=None,
    ):
        """
        Consumes credits.

        Rules:
        - No preflight
        - Safe for concurrent requests
        - Transaction handled based on ownership
        """

        if not user_id or not total_chars:
            return None
        # print(f"credit type: {credit_type}")
        # print("actual chars", total_chars)
        # print("reference id", reference_id)

        # 🔹 Refresh DB connection
        self.db = self.get_db()
        self.cm.db = self.db

        credits_to_consume = int(total_chars * self.CREDIT_MULTIPLIER)
        # print("credits needed to decrease", credits_to_consume)
        if credits_to_consume <= 0:
            return None

        try:
            # print("before consumption of credits", user_id)
            await self.cm.consume_credits(
                user_id=user_id,
                credits_needed=credits_to_consume,
                reason=credit_type.upper(),
                reference_id=reference_id or "AI_EXECUTION",
            )
            self.db.commit()

            # print("returning creed")
            return {
                "status": "ok",
                "credit_type": credit_type,
                "chars": total_chars,
                "credits_used": credits_to_consume,
            }

        except InsufficientCreditsError:
            self.db.rollback()

            return {
                "status": "error",
                "error": "INSUFFICIENT_CREDITS",
            }

        except Exception:
            # Rollback ONLY if Credits owns DB
            if self.owns_db:
                self.db.rollback()
            raise

        finally:
            if self.owns_db and self.db:
                try:
                    self.db.close()
                except Exception:
                    pass


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

    # print(f"called update_ai_credits:")
    # print(f"user_id : {user_id}")
    # print(f"credit_type : {credit_type}")
    # print(f"total_chars : {total_chars}")

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
async def get_credits():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    if not user_id or user_id in ("failure", "None"):
        return jsonify({"error": "user_id is required"}), 400
    # redis = RedisService()
    # key = CreditManager.summary_key.format(user_id)

    # cached = await redis.hgetall(key)
    # if cached:
    #    #print("cached credits data")
    #     return jsonify(cached)

    conn = connect_to_rds()
    cm = CreditManager(conn)

    summary = cm.get_credit_summary(user_id)

    conn.close()
    return jsonify(summary)


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
    view = request.args.get("view", "all")
    group_by = request.args.get("group_by", "date")
    limit = int(request.args.get("limit", 5))
    offset = int(request.args.get("offset", 0))
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    time_filter = ""
    params = [user_id]

    if view == "custom":
        if not from_date or not to_date:
            return jsonify({"error": "from_date and to_date required"}), 400
        time_filter = "AND DATE(u.created_at) BETWEEN %s AND %s"
        params += [from_date, to_date]
    elif view == "daily":
        time_filter = "AND DATE(u.created_at) = CURDATE()"
    elif view == "monthly":
        time_filter = "AND YEAR(u.created_at) = YEAR(CURDATE()) AND MONTH(u.created_at) = MONTH(CURDATE())"
    elif view == "yearly":
        time_filter = "AND YEAR(u.created_at) = YEAR(CURDATE())"

    # -----------------------------
    # 1️⃣ Summary (reason-wise)
    # -----------------------------
    cur.execute(
        f"""
        SELECT
            u.reason,
            SUM(u.credits_used) AS total_used
        FROM credit_usage_log u
        WHERE u.user_id = %s
        {time_filter}
        GROUP BY u.reason
        """,
        params,
    )
    summary = cur.fetchall()

    # -----------------------------
    # 2️⃣ Trend (date/month wise)
    # -----------------------------
    period_expr = (
        "DATE(u.created_at)"
        if group_by == "date"
        else "DATE_FORMAT(u.created_at, '%Y-%m')"
    )

    cur.execute(
        f"""
        SELECT
            {period_expr} AS period,
            SUM(u.credits_used) AS total_used
        FROM credit_usage_log u
        WHERE u.user_id = %s
        {time_filter}
        GROUP BY period
        ORDER BY period DESC
        """,
        params,
    )
    trend = cur.fetchall()

    # -----------------------------
    # 3️⃣ Logs (limited)
    # -----------------------------
    cur.execute(
        f"""
        SELECT
            u.created_at AS used_at,
            u.credits_used,
            u.reason,
            u.reference_id,
            b.source_type
        FROM credit_usage_log u
        JOIN credit_buckets b ON u.bucket_id = b.bucket_id
        WHERE u.user_id = %s
        {time_filter}
        ORDER BY u.created_at DESC
        LIMIT %s OFFSET %s
        """,
        params + [limit, offset],
    )
    logs = cur.fetchall()

    cur.close()
    conn.close()

    return jsonify(
        {
            "user_id": user_id,
            "view": view,
            "summary": summary,
            "trend": trend,
            "logs": logs,
            "limit": limit,
            "offset": offset,
        }
    )


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


# ====================================================
# 6. ADD CREDITS (ADMIN TOP-UP)
# ====================================================
@credits_bp.route("/credits/add", methods=["POST"])
def add_credits_route():
    body = request.get_json()

    if not body:
        return jsonify({"error": "Request body is required"}), 400

    user_id = body.get("user_id")
    customer_id = body.get("customer_id")
    customer_email = body.get("customer_email")
    number_credits = body.get("number_credits")

    if not user_id or (not customer_id and not customer_email) or number_credits is None:
        return jsonify({"error": "user_id, number_credits, and either customer_id or customer_email are required"}), 400

    if user_id not in ACCESSIBLE_IDS:
        return jsonify({"error": "Unauthorized"}), 403

    expires_at = datetime.utcnow() + timedelta(days=365)

    conn = connect_to_rds()
    try:
        cur = conn.cursor()

        if customer_email:
            cur.execute("SELECT user_id FROM users WHERE email = %s", (customer_email,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "No user found with that email"}), 404
            resolved_customer_id = row[0]
        else:
            cur.execute("SELECT user_id FROM users WHERE user_id = %s", (customer_id,))
            if not cur.fetchone():
                return jsonify({"error": "No user found with that customer_id"}), 404
            resolved_customer_id = customer_id

        cur.close()

        cm = CreditManager(conn)
        bucket_id = cm.add_credits(
            user_id=resolved_customer_id,
            credits=number_credits,
            source_type="TOPUP",
            expires_at=expires_at,
        )
        return jsonify({
            "success": True,
            "bucket_id": bucket_id,
            "customer_id": resolved_customer_id,
            "credits_added": number_credits,
            "expires_at": expires_at.isoformat(),
        }), 200
    finally:
        if conn:
            conn.close()
