import requests
import asyncio
from datetime import datetime, date
from db.rds_db import connect_to_rds
from flask import Blueprint, request, jsonify
import pymysql
import json
from services.redis_service import RedisService
from dotenv import load_dotenv
from utils.stripe_config import dev_stipe as stripe
from utils.stripe_config import (
    create_new_price_and_disable_old,
    create_stripe_product_and_price,
)

load_dotenv()
plans_bp = Blueprint("plans_bp", __name__)

# Preconfigured list of IDs allowed to modify plans
ACCESSIBLE_IDS = ["109161866299858012556", "113605503284012967393"]


redis_service = RedisService()
REDIS_PLANS_KEY = "plans_cache"

BASE_CURRENCY = "USD"
REDIS_RATE_KEY_PREFIX = "currency:rates"

# ------------------------------
# Access helpers
# ------------------------------


def check_access(user_id):
    return user_id in ACCESSIBLE_IDS


from decimal import Decimal


def normalize_for_json(data):
    if isinstance(data, list):
        return [normalize_for_json(item) for item in data]

    elif isinstance(data, dict):
        return {k: normalize_for_json(v) for k, v in data.items()}

    elif isinstance(data, Decimal):
        return float(data)

    elif isinstance(data, (datetime, date)):
        return data.isoformat()  # ✅ FIX

    else:
        return data


# =====================================================
# GET ALL PLANS (CACHED)
# =====================================================

@plans_bp.route("/plans/", methods=["GET"])
def get_all_plans():
    # 1️⃣ Try Redis cache
    try:
        cached = asyncio.run(redis_service.get(REDIS_PLANS_KEY))
        if cached:
            return jsonify({"plans": cached})
    except Exception as e:
        print("Redis GET error:", e)

    connection = connect_to_rds()
    cursor = connection.cursor(pymysql.cursors.DictCursor)

    try:
        cursor.execute(
            """
            SELECT *
            FROM plans
            WHERE is_active = TRUE
            ORDER BY id ASC
            """
        )
        plans = cursor.fetchall()
        updated = False

        for plan in plans:
            # 🔒 RULE: FREE plans must NEVER touch Stripe
            if plan["is_free"] == 1:
                continue

            # 🔒 RULE: Only subscription or topup plans use Stripe
            if plan["is_subscription"] == 0 and plan["is_topup"] == 0:
                continue

            # 🔒 RULE: billing_interval validation
            if plan["is_subscription"] == 1 and plan["billing_interval"] == "one_time":
                raise ValueError(
                    f"Invalid billing_interval for subscription: {plan['plan_code']}"
                )

            # 2️⃣ Create Stripe objects only if missing
            if not plan.get("stripe_product_id") or not plan.get("stripe_price_id"):
                stripe_product_id, stripe_price_id = create_stripe_product_and_price(
                    {
                        "plan_code": plan["plan_code"],
                        "name": plan["name"],
                        "description": plan.get("description", ""),
                        "amount_cents": plan["amount_cents"],
                        "currency": plan["currency"],
                        "billing_interval": plan["billing_interval"],
                        "is_subscription": plan["is_subscription"],
                        "is_topup": plan["is_topup"],
                    }
                )

                # 3️⃣ Update DB
                cursor.execute(
                    """
                    UPDATE plans
                    SET stripe_product_id = %s,
                        stripe_price_id = %s
                    WHERE id = %s
                    """,
                    (stripe_product_id, stripe_price_id, plan["id"]),
                )

                plan["stripe_product_id"] = stripe_product_id
                plan["stripe_price_id"] = stripe_price_id
                updated = True

        if updated:
            connection.commit()

        # 4️⃣ Normalize for JSON + Redis
        safe_plans = normalize_for_json(plans)

        # 5️⃣ Cache
        asyncio.run(redis_service.set(REDIS_PLANS_KEY, safe_plans, ex=300))

        return jsonify({"plans": safe_plans})

    except Exception as e:
        connection.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        connection.close()


# =====================================================
# ADD PLAN (STRIPE SYNC)
# =====================================================


@plans_bp.route("/plans/", methods=["POST"])
def add_plan():
    body = request.json or {}
    user_id = body.get("user_id")

    if not user_id or not check_access(user_id):
        return jsonify({"error": "Unauthorized"}), 403

    # Required
    plan_code = body.get("plan_code")
    name = body.get("name")

    if not plan_code or not name:
        return jsonify({"error": "plan_code and name required"}), 400

    # Optional / defaults
    description = body.get("description", "")
    amount_cents = int(body.get("amount_cents", 0))
    currency = body.get("currency", "USD")
    billing_interval = body.get("billing_interval", "month")
    monthly_token_limit = int(body.get("monthly_token_limit", 0))
    overage_price_per_million = float(body.get("overage_price_per_million", 0))
    details = body.get("details", {})
    is_free = bool(body.get("is_free", False))

    # Plan type (EXACTLY ONE must be true)
    is_subscription = bool(body.get("is_subscription", True))
    is_topup = bool(body.get("is_topup", False))

    if (is_subscription + is_topup) != 1:
        return jsonify({
            "error": "Exactly one of is_subscription or is_topup must be true"
        }), 400

    # -----------------------------------
    # Billing interval validation
    # -----------------------------------
    if is_free:
        billing_interval = "one_time"
    elif is_subscription and billing_interval not in ("month", "year"):
        return jsonify({
            "error": "Invalid billing_interval for subscription (month/year only)"
        }), 400
    elif is_topup:
        billing_interval = "one_time"

    stripe_product_id = None
    stripe_price_id = None

    # -----------------------------------
    # Stripe creation (ONLY when needed)
    # -----------------------------------
    if not is_free:
        stripe_product_id, stripe_price_id = create_stripe_product_and_price(
            {
                "plan_code": plan_code,
                "name": name,
                "description": description,
                "amount_cents": amount_cents,
                "currency": currency,
                "billing_interval": billing_interval,
            },
            is_subscription=is_subscription,
            is_topup=is_topup,
        )

    connection = connect_to_rds()
    cursor = connection.cursor()

    try:
        cursor.execute(
            """
            INSERT INTO plans (
                plan_code,
                name,
                description,
                amount_cents,
                currency,
                billing_interval,
                monthly_token_limit,
                overage_price_per_million,
                details,
                is_free,
                is_subscription,
                is_topup,
                stripe_product_id,
                stripe_price_id
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                plan_code,
                name,
                description,
                amount_cents,
                currency,
                billing_interval,
                monthly_token_limit,
                overage_price_per_million,
                json.dumps(details),
                is_free,
                is_subscription,
                is_topup,
                stripe_product_id,
                stripe_price_id,
            ),
        )

        connection.commit()
        asyncio.run(redis_service.delete(REDIS_PLANS_KEY))

        return jsonify({
            "message": "Plan created",
            "plan_code": plan_code
        }), 201

    except Exception as e:
        connection.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        connection.close()


# =====================================================
# EDIT PLAN (SAFE STRIPE UPDATE)
# =====================================================


@plans_bp.route("/plans/", methods=["PUT"])
def edit_plan():
    body = request.json or {}
    user_id = body.get("user_id")
    plan_code = body.get("plan_code")

    if not user_id or not check_access(user_id):
        return jsonify({"error": "Unauthorized"}), 403

    if not plan_code:
        return jsonify({"error": "plan_code required"}), 400

    allowed = {
        "name",
        "description",
        "amount_cents",
        "currency",
        "billing_interval",
        "monthly_token_limit",
        "overage_price_per_million",
        "details",
        "is_free",
        "is_subscription",
        "is_topup",
    }

    fields = {
        k: (json.dumps(v) if k == "details" else v)
        for k, v in body.items()
        if k in allowed
    }

    if not fields:
        return jsonify({"error": "Nothing to update"}), 400

    conn = connect_to_rds()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:
        # 🔎 Load existing plan
        cursor.execute(
            """
            SELECT *
            FROM plans
            WHERE plan_code=%s
            """,
            (plan_code,),
        )
        plan = cursor.fetchone()

        if not plan:
            return jsonify({"error": "Plan not found"}), 404

        # Current values
        curr_sub = plan["is_subscription"]
        curr_topup = plan["is_topup"]
        curr_free = plan["is_free"]
        curr_billing = plan["billing_interval"]

        # New values (fallback to existing)
        new_sub = fields.get("is_subscription", curr_sub)
        new_topup = fields.get("is_topup", curr_topup)
        new_free = fields.get("is_free", curr_free)
        new_billing = fields.get("billing_interval", curr_billing)

        # ----------------------------------
        # PLAN TYPE CONSTRAINT
        # ----------------------------------
        if (new_sub + new_topup) != 1:
            return jsonify({
                "error": "Exactly one of is_subscription or is_topup must be true"
            }), 400

        # ----------------------------------
        # BILLING INTERVAL RULES
        # ----------------------------------
        if new_free:
            fields["billing_interval"] = "one_time"
        elif new_sub and new_billing not in ("month", "year"):
            return jsonify({
                "error": "Invalid billing_interval for subscription (month/year only)"
            }), 400
        elif new_topup:
            fields["billing_interval"] = "one_time"

        # ----------------------------------
        # STRIPE PRICE UPDATE (ONLY IF NEEDED)
        # ----------------------------------
        stripe_price_fields = {"amount_cents", "currency", "billing_interval"}

        if not new_free and stripe_price_fields.intersection(fields):
            new_price_id = create_new_price_and_disable_old(
                {
                    "plan_code": plan_code,
                    "amount_cents": fields.get("amount_cents", plan["amount_cents"]),
                    "currency": fields.get("currency", plan["currency"]),
                    "billing_interval": fields.get(
                        "billing_interval", plan["billing_interval"]
                    ),
                    "stripe_product_id": plan["stripe_product_id"],
                },
                plan["stripe_price_id"],
                is_subscription=new_sub,
                is_topup=new_topup,
            )

            fields["stripe_price_id"] = new_price_id

        # ----------------------------------
        # APPLY UPDATE
        # ----------------------------------
        set_clause = ", ".join(f"{k}=%s" for k in fields)
        cursor.execute(
            f"""
            UPDATE plans
            SET {set_clause}
            WHERE plan_code=%s
            """,
            list(fields.values()) + [plan_code],
        )

        conn.commit()
        asyncio.run(redis_service.delete(REDIS_PLANS_KEY))

        return jsonify({"message": "Plan updated successfully"})

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        conn.close()

# =====================================================
# DELETE PLAN (SOFT DELETE + STRIPE DEACTIVATE)
# =====================================================


@plans_bp.route("/plans/", methods=["DELETE"])
def delete_plan():
    body = request.json or {}
    user_id = body.get("user_id")
    plan_code = body.get("plan_code")

    if not user_id or not check_access(user_id):
        return jsonify({"error": "Unauthorized"}), 403

    connection = connect_to_rds()
    cursor = connection.cursor()

    try:
        cursor.execute(
            "SELECT stripe_product_id FROM plans WHERE plan_code=%s", (plan_code,)
        )
        row = cursor.fetchone()

        if row and row[0]:
            stripe.Product.modify(row[0], active=False)

        cursor.execute(
            "UPDATE plans SET is_active=FALSE WHERE plan_code=%s", (plan_code,)
        )

        connection.commit()
        asyncio.run(redis_service.delete(REDIS_PLANS_KEY))

        return jsonify({"message": "Plan deactivated"})

    finally:
        cursor.close()
        connection.close()


# =====================================================
# EXCHANGE RATES (REDIS CACHED)
# =====================================================


async def get_exchange_rates():
    today = date.today().isoformat()
    redis_key = f"{REDIS_RATE_KEY_PREFIX}:{BASE_CURRENCY}:{today}"

    cached = await redis_service.get(redis_key)
    if cached:
        return cached

    resp = requests.get(
        f"https://api.frankfurter.app/latest?from={BASE_CURRENCY}",
        timeout=5,
    )
    resp.raise_for_status()

    payload = {
        "base": BASE_CURRENCY,
        "date": today,
        "rates": resp.json().get("rates", {}),
    }

    await redis_service.set(redis_key, payload, ex=25 * 60 * 60)
    return payload


@plans_bp.route("/plans/rates", methods=["GET"])
def get_rates():
    return jsonify({"exchange": asyncio.run(get_exchange_rates())})


# @plans_bp.route("/stripe_webhook", methods=["POST"])
# def stripe_webhook():
#     payload = request.data
#     sig_header = request.headers.get("Stripe-Signature")

#     # print("\n========== STRIPE WEBHOOK RECEIVED ==========")
#     # print("Headers:")
#     # print(dict(request.headers))

#     # print("\nRaw Payload:")
#     # print(payload.decode("utf-8"))

#     try:
#         event = stripe.Webhook.construct_event(
#             payload=payload,
#             sig_header=sig_header,
#             secret=STRIPE_WEBHOOK_SECRET,
#         )
#     except stripe.error.SignatureVerificationError as e:
#         # print("❌ Signature verification failed:", str(e))
#         return jsonify({"error": "Invalid signature"}), 400
#     except Exception as e:
#         # print("❌ Webhook error:", str(e))
#         return jsonify({"error": str(e)}), 400

#     # print("\nParsed Event:")
#     # print("Event ID:", event["id"])
#     # print("Event Type:", event["type"])

#     # print("\nEvent Data Object:")
#     # print(event["data"]["object"])

#     # print("========== END STRIPE WEBHOOK ==========\n")

#     return jsonify({"status": "received"}), 200


@plans_bp.route("/stripe/products", methods=["GET"])
def list_products():
    try:
        products = stripe.Product.list(limit=100)

        return jsonify(
            {
                "products": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "description": p.description,
                        "active": p.active,
                        "metadata": p.metadata,
                    }
                    for p in products.data
                ]
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@plans_bp.route("/stripe/prices", methods=["GET"])
def list_prices():
    product_id = request.args.get("product_id")

    try:
        prices = stripe.Price.list(
            product=product_id,
            limit=100,
            active=True,
        )

        return jsonify(
            {
                "prices": [
                    {
                        "id": p.id,
                        "product": p.product,
                        "unit_amount": p.unit_amount,
                        "currency": p.currency,
                        "interval": p.recurring["interval"] if p.recurring else None,
                    }
                    for p in prices.data
                ]
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500
