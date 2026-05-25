import asyncio
import json
import uuid
import pymysql
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Optional
from db.rds_db import connect_to_rds
from services.redis_service import get_redis

# ---------------------------------------------------------
# DATA MODELS
# ---------------------------------------------------------


@dataclass
class CreditBucket:
    bucket_id: str
    user_id: str
    source_type: str
    credits_total: int
    credits_used: int
    expires_at: datetime


@dataclass
class CreditUsageResult:
    requested: int
    consumed: int
    breakdown: List[Dict]


# ---------------------------------------------------------
# CREDIT MANAGER
# ---------------------------------------------------------


class InsufficientCreditsError(Exception):
    pass


class CreditManager:
    """
    MySQL = Source of Truth
    Redis = Fast cache (available credits only)
    """

    REDIS_KEY = "user:credits:{}"
    summary_key = "user:creditsSummary:{}"

    def __init__(self, db_conn):
        self.db = db_conn
        self.redis = get_redis()

    # -----------------------------------------------------
    # INVITED USER → BILLING ADMIN RESOLUTION
    # -----------------------------------------------------

    def _resolve_billing_user_id(self, user_id: str) -> str:
        """
        If user_id belongs to an invited user, returns the admin's user_id.
        Resolution order:
          1. user_type != 'user' → return user_id unchanged
          2. permissions.invited_by (non-null, non-'system') → look up admin by email
          3. launch_id_fk fallback
          4. Silent fail → return user_id unchanged
        """
        try:
            cur = self.db.cursor(pymysql.cursors.DictCursor)
            cur.execute(
                "SELECT user_type, permissions, launch_id_fk FROM users WHERE user_id=%s",
                (user_id,),
            )
            row = cur.fetchone()
            cur.close()

            if not row or row.get("user_type") != "user":
                return user_id

            # Check permissions.invited_by
            try:
                perms = json.loads(row["permissions"]) if row.get("permissions") else {}
                invited_by_email = perms.get("invited_by") if isinstance(perms, dict) else None
                if invited_by_email and invited_by_email != "system":
                    cur2 = self.db.cursor(pymysql.cursors.DictCursor)
                    cur2.execute(
                        "SELECT user_id FROM users WHERE email=%s AND user_type='admin' LIMIT 1",
                        (invited_by_email,),
                    )
                    admin_row = cur2.fetchone()
                    cur2.close()
                    if admin_row and admin_row.get("user_id"):
                        return admin_row["user_id"]
            except Exception:
                pass

            # Fallback: launch_id_fk
            launch_id = (row.get("launch_id_fk") or "").strip()
            if launch_id:
                return launch_id

        except Exception:
            pass

        return user_id

    # -----------------------------------------------------
    # REDIS HELPERS
    # -----------------------------------------------------

    def _redis_key(self, user_id: str) -> str:
        return self.REDIS_KEY.format(user_id)

    # -----------------------------------------------------
    # SYNC DB → REDIS (AUTHORITATIVE)
    # -----------------------------------------------------

    async def sync_credits_to_redis(self, user_id: str) -> int:
        user_id = self._resolve_billing_user_id(user_id)
        cur = self.db.cursor(pymysql.cursors.DictCursor)
        cur.execute(
            """
            SELECT COALESCE(SUM(credits_total - credits_used), 0) AS total
            FROM credit_buckets
            WHERE user_id = %s
              AND is_expired = 0
            """,
            (user_id,),
        )
        row = cur.fetchone()
        cur.close()

        total = int(row["total"] or 0)

        # if self.redis:
        #     await self.redis.hset(
        #         self._redis_key(user_id),
        #         {
        #             "total_available": total,
        #             "last_synced_at": int(datetime.utcnow().timestamp()),
        #         },
        #     )

        #     await self.redis.expire(self._redis_key(user_id), 3600)
        #     await self._write_credit_summary_to_redis(user_id)

        return total

    # -----------------------------------------------------
    # FAST READ (REDIS → DB FALLBACK)
    # -----------------------------------------------------

    async def get_available_credits(self, user_id: str) -> int:
        user_id = self._resolve_billing_user_id(user_id)
        # if self.redis:
        #     cached = await self.redis.hget(self._redis_key(user_id), "total_available")
        #     if cached is not None:
        #         print("cached data credits", cached)
        #         return int(cached)

        return await self.sync_credits_to_redis(user_id)

    # -----------------------------------------------------
    # PREFLIGHT CHECK
    # -----------------------------------------------------

    async def has_sufficient_credits(self, user_id: str, needed: int) -> bool:
        user_id = self._resolve_billing_user_id(user_id)
        return await self.get_available_credits(user_id) >= needed

    # async def _write_credit_summary_to_redis(self, user_id: str):
    #     print("_write_credit_summary_to_redis")

    #     summary = self.get_credit_summary(user_id)

    #     if not self.redis:
    #         return summary

    #     redis_payload = {
    #         "status": summary["status"],
    #         "message": summary.get("message"),
    #         "available_total": summary.get("available_total", 0),
    #         "available_breakdown": json.dumps(summary.get("available_breakdown", [])),
    #         "usage_breakdown": json.dumps(summary.get("usage_breakdown", [])),
    #         "next_expiry": (
    #             json.dumps(summary.get("next_expiry"))
    #             if summary.get("next_expiry")
    #             else None
    #         ),
    #         "last_updated_at": int(datetime.utcnow().timestamp()),
    #     }

    #     # Remove None values (Redis doesn't like them)
    #     redis_payload = {k: v for k, v in redis_payload.items() if v is not None}

    #     await self.redis.hset(
    #         self.summary_key.format(user_id),
    #         redis_payload,
    #     )

    #     return summary

    # -----------------------------------------------------
    # ADD CREDITS
    # -----------------------------------------------------

    def add_credits(
        self,
        user_id: str,
        credits: int,
        source_type: str,
        expires_at: datetime,
        source_ref: Optional[str] = None,
    ) -> str:

        bucket_id = str(uuid.uuid4())
        cur = self.db.cursor(pymysql.cursors.DictCursor)

        cur.execute(
            """
            INSERT INTO credit_buckets (
                bucket_id, user_id, source_type,
                source_ref, credits_total, expires_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                bucket_id,
                user_id,
                source_type,
                source_ref,
                credits,
                expires_at,
            ),
        )

        self.db.commit()
        cur.close()

        # Redis increment (non-authoritative)
        if self.redis:
            asyncio.run(
                self.redis.hincrby(
                    self._redis_key(user_id),
                    "total_available",
                    credits,
                )
            )

        return bucket_id

    # -----------------------------------------------------
    # CONSUME CREDITS (TRANSACTION SAFE)
    # -----------------------------------------------------

    async def consume_credits(
        self,
        user_id: str,
        credits_needed: int,
        reason: str,
        reference_id: str,
    ) -> CreditUsageResult:
        user_id = self._resolve_billing_user_id(user_id)
        remaining = credits_needed
        breakdown = []

        cur = None

        try:
            try:
                cur = self.db.cursor(pymysql.cursors.DictCursor)
                # print("Cursor created from existing connection")

            except Exception as conn_error:
                # print("Existing DB connection failed, reconnecting...")
                # print("Connection error:", conn_error)

                import traceback

                traceback.print_exc()

                # reconnect
                self.db = connect_to_rds()

                cur = self.db.cursor(pymysql.cursors.DictCursor)

                # print("New DB connection created")

            cur.execute(
                """
                SELECT *
                FROM credit_buckets
                WHERE user_id = %s
                AND is_expired = 0
                AND credits_used < credits_total
                ORDER BY expires_at ASC
                FOR UPDATE
                """,
                (user_id,),
            )

            buckets = cur.fetchall()
            # print("Buckets fetched:", buckets)

            for b in buckets:

                if remaining <= 0:
                    break

                available = b["credits_total"] - b["credits_used"]
                consume = min(available, remaining)

                # print(f"Consuming {consume} from bucket {b['bucket_id']}")

                cur.execute(
                    """
                    UPDATE credit_buckets
                    SET credits_used = credits_used + %s
                    WHERE bucket_id = %s
                    """,
                    (consume, b["bucket_id"]),
                )

                cur.execute(
                    """
                    INSERT INTO credit_usage_log (
                        usage_id, user_id, bucket_id,
                        credits_used, reason, reference_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid.uuid4()),
                        user_id,
                        b["bucket_id"],
                        consume,
                        reason,
                        reference_id,
                    ),
                )

                breakdown.append(
                    {
                        "bucket_id": b["bucket_id"],
                        "source_type": b["source_type"],
                        "used": consume,
                        "expires_at": b["expires_at"],
                    }
                )

                remaining -= consume

            if remaining > 0:
                # print("Error: Not enough credits")
                raise InsufficientCreditsError("Not enough credits")

            # print("Credits consumed successfully")

            return CreditUsageResult(
                requested=credits_needed,
                consumed=credits_needed,
                breakdown=breakdown,
            )

        except Exception as e:
            # print("Error in consume_credits:", str(e))
            import traceback

            traceback.print_exc()
            raise

        finally:
            if cur:
                cur.close()
                # print("Cursor closed")

    def get_credit_summary(self, user_id: str) -> Dict:
        user_id = self._resolve_billing_user_id(user_id)
        cur = self.db.cursor(pymysql.cursors.DictCursor)

        cur.execute(
            """
            SELECT
                source_type,
                source_ref,
                credits_total,
                credits_used,
                expires_at,
                is_expired,
                created_at
            FROM credit_buckets
            WHERE user_id = %s
            ORDER BY expires_at ASC
            """,
            (user_id,),
        )

        rows = cur.fetchall()
        cur.close()

        # --------------------------------------------------
        # 1️⃣ No credit buckets at all
        # --------------------------------------------------
        if not rows:
            return {
                "user_id": user_id,
                "status": "NO_CREDITS",
                "message": "No credits found. Please purchase or subscribe.",
                "available_total": 0,
                "available_breakdown": [],
                "usage_breakdown": [],
                "next_expiry": None,
            }

        available_total = 0
        available_breakdown = {}
        usage_breakdown = {}
        next_expiry = None
        has_available = False

        # --------------------------------------------------
        # 2️⃣ Process buckets
        # --------------------------------------------------
        for r in rows:
            remaining = r["credits_total"] - r["credits_used"]

            # -------------------------
            # Usage (ALWAYS count)
            # -------------------------
            usage_breakdown.setdefault(r["source_type"], {"total": 0, "used": 0})
            usage_breakdown[r["source_type"]]["total"] += r["credits_total"]
            usage_breakdown[r["source_type"]]["used"] += r["credits_used"]

            # -------------------------
            # Availability (ONLY usable)
            # -------------------------
            if r["is_expired"] or remaining <= 0:
                continue

            has_available = True
            available_total += remaining

            available_breakdown.setdefault(r["source_type"], {"remaining": 0})
            available_breakdown[r["source_type"]]["remaining"] += remaining

            # nearest expiry
            if not next_expiry and r["expires_at"]:
                next_expiry = {
                    "source_type": r["source_type"],
                    "source_ref": r["source_ref"],
                    "total": r["credits_total"],
                    "used": r["credits_used"],
                    "remaining": remaining,
                    "expires_at": r["expires_at"].isoformat(),
                }

        # --------------------------------------------------
        # 3️⃣ No usable credits
        # --------------------------------------------------
        if not has_available:
            status = "NO_ACTIVE_CREDITS"
            message = "All credits are used or expired."
        else:
            status = "ACTIVE"
            message = "Credits are active and available."

        # --------------------------------------------------
        # 4️⃣ Final response
        # --------------------------------------------------
        return {
            "user_id": user_id,
            "status": status,
            "message": message,
            # usable credits
            "available_total": available_total,
            "available_breakdown": [
                {
                    "source_type": k,
                    "remaining": v["remaining"],
                }
                for k, v in available_breakdown.items()
            ],
            # usage visibility (this is what you were missing)
            "usage_breakdown": [
                {
                    "source_type": k,
                    "total": v["total"],
                    "used": v["used"],
                    "remaining": v["total"] - v["used"],
                }
                for k, v in usage_breakdown.items()
            ],
            "next_expiry": next_expiry,
        }

    def check_if_remaining(self, user_id: str) -> Dict:
        user_id = self._resolve_billing_user_id(user_id)
        cur = self.db.cursor(pymysql.cursors.DictCursor)

        cur.execute(
            """
            SELECT
                credits_total,
                credits_used,
                expires_at,
                is_expired
            FROM credit_buckets
            WHERE user_id = %s
            ORDER BY expires_at ASC
            """,
            (user_id,),
        )

        rows = cur.fetchall()
        cur.close()

        # 1️⃣ No credit buckets
        if not rows:
            return {
                "user_id": user_id,
                "available": "False",
                "status": "NO_CREDITS",
                "message": "No credits found. Please purchase or subscribe.",
            }

        found_non_expired_bucket = False

        for r in rows:
            remaining = r["credits_total"] - r["credits_used"]

            # Skip expired buckets
            if r["is_expired"]:
                continue

            found_non_expired_bucket = True

            # If any valid remaining credits exist → SUCCESS
            if remaining > 500:
                return {
                    "user_id": user_id,
                    "available": "True",
                    "status": "HAS_CREDITS",
                    "message": "User has enough credits",
                }

        # 2️⃣ No usable credits found
        if found_non_expired_bucket:
            return {
                "user_id": user_id,
                "available": "False",
                "status": "NO_CREDITS",
                "message": "All available credits are exhausted.",
            }

        return {
            "user_id": user_id,
            "available": "False",
            "status": "CREDITS_EXPIRED",
            "message": "All credits are expired. Please purchase or subscribe.",
        }

    # -----------------------------------------------------
    # CREDIT BALANCE (UI)
    # -----------------------------------------------------

    def get_credit_balance(self, user_id: str) -> Dict:
        user_id = self._resolve_billing_user_id(user_id)
        cur = self.db.cursor(pymysql.cursors.DictCursor)
        cur.execute(
            """
            SELECT
                source_type,
                SUM(credits_total - credits_used) AS remaining
            FROM credit_buckets
            WHERE user_id = %s
              AND is_expired = 0
            GROUP BY source_type
            """,
            (user_id,),
        )

        rows = cur.fetchall()
        cur.close()

        return {
            "user_id": user_id,
            "breakdown": rows,
            "total": sum(r["remaining"] for r in rows if r["remaining"]),
        }

    # -----------------------------------------------------
    # MONTHLY ROLLOVER
    # -----------------------------------------------------

    def rollover_monthly_credits(self, user_id: str, next_month_end: datetime):
        cur = self.db.cursor(pymysql.cursors.DictCursor)
        cur.execute(
            """
            SELECT *
            FROM credit_buckets
            WHERE user_id = %s
              AND source_type = 'SUBSCRIPTION'
              AND is_expired = 0
            """,
            (user_id,),
        )
        buckets = cur.fetchall()
        cur.close()

        for b in buckets:
            unused = b["credits_total"] - b["credits_used"]
            if unused > 0:
                self.add_credits(
                    user_id=user_id,
                    credits=unused,
                    source_type="ROLLOVER",
                    expires_at=next_month_end,
                    source_ref=b["bucket_id"],
                )

        # Redis rebuild (safe)
        if self.redis:
            self.redis.delete(self._redis_key(user_id))

    # -----------------------------------------------------
    # EXPIRY CRON
    # -----------------------------------------------------

    def expire_credits(self):
        cur = self.db.cursor(pymysql.cursors.DictCursor)
        cur.execute(
            """
            UPDATE credit_buckets
            SET is_expired = 1
            WHERE expires_at < NOW()
              AND is_expired = 0
            """
        )
        # self.db.commit()
        cur.close()
