import uuid, pymysql
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Dict, Optional

# ---------------------------------------------------------
# DATA MODELS
# ---------------------------------------------------------


@dataclass
class CreditBucket:
    bucket_id: str
    user_id: str
    source_type: str  # SUBSCRIPTION | ROLLOVER | TOPUP | BONUS
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
class CreditManager:
    """
    DB = source of truth
    Redis = live counters only
    """

    def __init__(self, db_conn, redis_client=None):
        self.db = db_conn
        self.redis = redis_client

    # -----------------------------------------------------
    # ADD CREDITS (subscription / topup / rollover)
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

        # Ensure wallet exists
        cur.execute(
            """
            INSERT IGNORE INTO credit_wallets (user_id)
            VALUES (%s)
        """,
            (user_id,),
        )

        self.db.commit()
        cur.close()
        return bucket_id

    # -----------------------------------------------------
    # CONSUME CREDITS (AI execution)
    # -----------------------------------------------------

    def consume_credits(
        self,
        user_id: str,
        credits_needed: int,
        reason: str,
        reference_id: str,
    ) -> CreditUsageResult:

        remaining = credits_needed
        breakdown = []

        cur = self.db.cursor(pymysql.cursors.DictCursor)

        try:
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

            for b in buckets:
                if remaining <= 0:
                    break

                available = b["credits_total"] - b["credits_used"]
                consume = min(available, remaining)

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
                raise Exception("INSUFFICIENT_CREDITS")

            self.db.commit()

        except Exception:
            self.db.rollback()
            raise

        finally:
            cur.close()

        # Redis live tracking (non-authoritative)
        if self.redis:
            self.redis.hincrby(f"user_credits:{user_id}", "normal", credits_needed)

        return CreditUsageResult(
            requested=credits_needed,
            consumed=credits_needed,
            breakdown=breakdown,
        )

    # -----------------------------------------------------
    # CREDIT BALANCE (UI / API)
    # -----------------------------------------------------

    def get_credit_balance(self, user_id: str) -> Dict:
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
        self.db.commit()
        cur.close()
