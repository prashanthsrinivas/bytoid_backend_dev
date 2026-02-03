from datetime import datetime
from decimal import Decimal
import json
from db.rds_db import connect_to_rds
from db.db_checkers import get_email_by_id
from flask import Blueprint, request, jsonify
from services.redis_service import RedisService
import pymysql
from services.stripe_webhook_handler import StripeWebhookHandler
from utils.stripe_config import (
    create_checkout_session,
    dev_stipe as stripe,
    verify_webhook,
)

payments_bp = Blueprint("payments", __name__)

redis_service = RedisService()
REDIS_PLANS_KEY = "plans_cache"


# -------------------------------------------------
# HELPERS
# -------------------------------------------------
def normalize_for_json(data):
    if isinstance(data, list):
        return [normalize_for_json(i) for i in data]
    if isinstance(data, dict):
        return {k: normalize_for_json(v) for k, v in data.items()}
    if isinstance(data, Decimal):
        return float(data)
    return data


def get_all_active_plans():
    connection = connect_to_rds()
    cursor = connection.cursor(pymysql.cursors.DictCursor)

    cursor.execute("SELECT * FROM plans WHERE is_active=TRUE ORDER BY id ASC")
    plans = cursor.fetchall()

    cursor.close()
    connection.close()
    return normalize_for_json(plans)


def get_plan_by_code(plan_code):
    connection = connect_to_rds()
    cursor = connection.cursor(pymysql.cursors.DictCursor)

    cursor.execute(
        "SELECT * FROM plans WHERE plan_code=%s AND is_active=TRUE",
        (plan_code,),
    )
    plan = cursor.fetchone()

    cursor.close()
    connection.close()
    return plan


def update_plan_stripe_ids(plan_code, product_id, price_id):
    connection = connect_to_rds()
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE plans
        SET stripe_product_id=%s, stripe_price_id=%s
        WHERE plan_code=%s
        """,
        (product_id, price_id, plan_code),
    )

    connection.commit()
    cursor.close()
    connection.close()


# -------------------------------------------------
# STRIPE PRODUCT + PRICE CREATION
# -------------------------------------------------
def ensure_stripe_plan(plan):
    if plan.get("stripe_product_id") and plan.get("stripe_price_id"):
        return plan["stripe_product_id"], plan["stripe_price_id"]

    product = stripe.Product.create(
        name=plan["name"],
        description=plan["description"] or plan["name"],
        metadata={"plan_code": plan["plan_code"]},
    )

    price = stripe.Price.create(
        product=product.id,
        unit_amount=plan["amount_cents"],
        currency=plan["currency"].lower(),
        recurring={"interval": plan["billing_interval"]},
    )

    update_plan_stripe_ids(plan["plan_code"], product.id, price.id)
    return product.id, price.id


# Temp JSON log file
PAYMENTS_LOG_FILE = "payments_log.json"


def log_payment_event(data):
    """Append event data to JSON file for temporary tracking"""
    try:
        try:
            with open(PAYMENTS_LOG_FILE, "r") as f:
                logs = json.load(f)
        except FileNotFoundError:
            logs = []

        logs.append({**data, "logged_at": datetime.utcnow().isoformat()})
        with open(PAYMENTS_LOG_FILE, "w") as f:
            # print("saving to log file", PAYMENTS_LOG_FILE)
            json.dump(logs, f, indent=4, default=str)
    except Exception as e:
        print("❌ Failed to log payment event:", e)


# -------------------------------------------------
# SUBSCRIPTION CHECKOUT
# -------------------------------------------------
@payments_bp.route("/payments/subscribe", methods=["POST"])
def subscribe():
    body = request.json or {}

    user_id = body.get("user_id")
    email = body.get("email")
    plan_code = body.get("plan_code")
    price_id = body.get("price_id")

    if not user_id or not price_id:
        return jsonify({"error": "user_id & price_id required"}), 400

    conn = connect_to_rds()
    cur = conn.cursor()
    if not email:
        email=get_email_by_id(user_id)

    try:
        # -----------------------------
        # 1️⃣ Fetch new plan amount
        # -----------------------------
        cur.execute(
            "SELECT amount_cents,monthly_token_limit FROM plans WHERE stripe_price_id = %s",
            (price_id,),
        )
        plan_row = cur.fetchone()
        if not plan_row:
            return jsonify({"error": "Plan not found"}), 400

        new_price_cents = plan_row[0]
        actualTokens = plan_row[1]

        # -----------------------------
        # 2️⃣ Fetch active subscription
        # -----------------------------
        cur.execute(
            """
            SELECT stripe_subscription_id
            FROM subscriptions
            WHERE user_id = %s AND status = 'active'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        row = cur.fetchone()

        # =====================================================
        # 🔁 CASE A: USER HAS ACTIVE SUBSCRIPTION → UPGRADE
        # =====================================================
        if row:
            subscription_id = row[0]

            subscription = stripe.Subscription.retrieve(
                subscription_id, expand=["default_payment_method", "latest_invoice"]
            )

            # 2.1 Current plan
            current_price_id = subscription["items"]["data"][0]["price"]["id"]

            cur.execute(
                "SELECT amount_cents FROM plans WHERE stripe_price_id = %s",
                (current_price_id,),
            )
            current_plan = cur.fetchone()
            if not current_plan:
                return jsonify({"error": "Current plan not found"}), 400

            current_price_cents = current_plan[0]
            # print("current price", current_price_cents)
            # print("new price cents", new_price_cents)

            # 2.2 Prevent downgrade here
            if new_price_cents <= current_price_cents:
                return (
                    jsonify({"error": "New plan must be higher than current plan"}),
                    400,
                )

            # -----------------------------
            # 3️⃣ Check if card exists
            # -----------------------------
            has_payment_method = bool(subscription.default_payment_method)

            # -----------------------------
            # 4️⃣ If NO card → redirect to Checkout
            # -----------------------------
            if not has_payment_method:
                session = create_checkout_session(
                    mode="subscription",
                    line_items=[{"price": price_id, "quantity": 1}],
                    email=email,
                    metadata={
                        "user_id": user_id,
                        "plan_code": plan_code,
                        "type": "upgrade",
                        "subscription_id": subscription_id,
                        "actualTokens": actualTokens,
                    },
                )
                return jsonify(
                    {
                        "payment_required": True,
                        "url": session.url,
                        "checkout_session_id": session.id,
                    }
                )

            # -----------------------------
            # 5️⃣ Upgrade with proration
            # -----------------------------
            updated_subscription = stripe.Subscription.modify(
                subscription_id,
                cancel_at_period_end=False,
                proration_behavior="create_prorations",
                items=[
                    {
                        "id": subscription["items"]["data"][0]["id"],
                        "price": price_id,
                    }
                ],
                metadata={
                    "user_id": user_id,
                    "plan_code": plan_code,
                    "type": "upgrade",
                    "actualTokens": actualTokens,
                },
            )

            # 🔥 Force invoice creation
            invoice = stripe.Invoice.create(
                customer=updated_subscription["customer"],
                subscription=updated_subscription["id"],
                auto_advance=True,  # auto-finalize
            )

            # 🔥 Force payment
            stripe.Invoice.pay(invoice["id"])

            return jsonify(
                {
                    "message": "Subscription upgraded",
                    "billing": "Proration invoice will be charged automatically",
                }
            )

        # =====================================================
        # 🆕 CASE B: NO SUBSCRIPTION → NEW CHECKOUT
        # =====================================================
        session = create_checkout_session(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            email=email,
            metadata={
                "user_id": user_id,
                "plan_code": plan_code,
                "type": "subscription",
                "actualTokens": actualTokens,
            },
        )

        return jsonify({"url": session.url, "checkout_session_id": session.id})

    finally:
        cur.close()
        conn.close()


# -------------------------------------------------
# ONE-TIME PAYMENT / TOPUP
# -------------------------------------------------
@payments_bp.route("/payments/topup", methods=["POST"])
def paymenttopup():
    body = request.json or {}

    user_id = body.get("user_id")
    email = body.get("email")
    plan_code = body.get("plan_code")

    if not user_id or not plan_code:
        return jsonify({"error": "user_id & plan_code required"}), 400

    connection = connect_to_rds()
    if not email:
        email = get_email_by_id(user_id, connection=connection)
    cursor = connection.cursor(pymysql.cursors.DictCursor)

    try:
        cursor.execute(
            """
            SELECT stripe_price_id, is_topup,monthly_token_limit
            FROM plans
            WHERE plan_code=%s
            """,
            (plan_code,),
        )

        plan = cursor.fetchone()

        if not plan:
            return jsonify({"error": "Invalid plan"}), 404

        if not plan["is_topup"]:
            return jsonify({"error": "Not a topup plan"}), 400

        session = create_checkout_session(
            mode="payment",
            line_items=[{"price": plan["stripe_price_id"], "quantity": 1}],
            metadata={
                "user_id": user_id,
                "plan_code": plan_code,
                "type": "topup",
                "credits": plan["monthly_token_limit"],
            },
            email=email,
        )

        return jsonify({"url": session.url})

    finally:
        cursor.close()
        connection.close()


# -------------------------------------------------
# CANCEL SUBSCRIPTION
# -------------------------------------------------
@payments_bp.route("/payments/subscription/cancel", methods=["POST"])
def cancel_subscription():
    body = request.json or {}
    subscription_id = body.get("subscription_id")
    user_id = body.get("user_id")

    if not subscription_id or not user_id:
        return jsonify({"error": "subscription_id & user_id required"}), 400

    try:
        # Cancel in Stripe — webhook will handle DB update
        stripe.Subscription.delete(subscription_id)

        # log_payment_event(
        #     {
        #         "action": "cancel_subscription",
        #         "subscription_id": subscription_id,
        #         "user_id": user_id,
        #     }
        # )
        return jsonify({"status": "cancellation_requested"})
    except Exception as e:
        # print("❌ Error cancelling subscription:", e)
        return jsonify({"error": str(e)}), 400


# -------------------------------------------------
# STRIPE WEBHOOK
# -------------------------------------------------
@payments_bp.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    # print("paylod on webhook", payload)
    sig = request.headers.get("Stripe-Signature")

    try:
        event = verify_webhook(payload, sig)
    except Exception as e:
        print("❌ Webhook signature verification failed:", e)
        return jsonify({"error": str(e)}), 400

    handler = StripeWebhookHandler(event)
    handler.process()

    # Always return 200 to Stripe
    return jsonify({"status": "received"}), 200


def format_subscription(sub, plan):
    return {
        "subscription_id": sub["stripe_subscription_id"],
        "status": sub["status"].capitalize(),
        "plan_name": plan["name"],
        "amount": f"${plan['amount_cents'] / 100:.2f}",
        "currency": plan["currency"].upper(),
        "interval": plan["billing_interval"],
        "created_at": sub["created_at"].strftime("%Y-%m-%d"),
    }


# -------------------------------------------------
# GET USER SUBSCRIPTIONS
# -------------------------------------------------
@payments_bp.route("/payments/subscriptions/<user_id>", methods=["GET"])
def get_user_subscriptions(user_id):
    conn = connect_to_rds()
    if not conn:
        return jsonify({"error": "DB connection failed"}), 500

    cur = conn.cursor(pymysql.cursors.DictCursor)

    try:
        # 1️⃣ Fetch subscriptions
        cur.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE user_id = %s
            ORDER BY current_period_end DESC
            """,
            (user_id,),
        )
        subs = cur.fetchall()

        if not subs:
            return jsonify({"subscriptions": []})

        results = []

        for sub in subs:
            # 2️⃣ Fetch plan for each subscription
            cur.execute(
                """
                SELECT *
                FROM plans
                WHERE stripe_price_id = %s
                LIMIT 1
                """,
                (sub["stripe_price_id"],),
            )
            plan = cur.fetchone()

            if not plan:
                continue

            results.append(format_subscription(sub, plan))

        return jsonify({"subscriptions": results})

    finally:
        cur.close()
        conn.close()


def format_payment_row(p):
    return {
        "type": p["payment_type"].capitalize(),
        "date": p["created_at"].strftime("%Y-%m-%d"),
        "amount": f"${p['amount_cents'] / 100:.2f}",
        "status": p["status"].capitalize(),
        "invoice_url": p["invoice_url"],
    }


def reconcile_pending_payment(payment_row):
    payment_type = payment_row["payment_type"]

    # -------------------------
    # SUBSCRIPTION
    # -------------------------
    if payment_type == "subscription":
        sub_id = payment_row["stripe_subscription_id"]
        if not sub_id:
            return "failed"

        sub = stripe.Subscription.retrieve(sub_id)

        status = sub["status"]
        if status == "active":
            return "succeeded"
        elif status in ("canceled", "incomplete_expired", "unpaid"):
            return "failed"

    # -------------------------
    # ONE-TIME PAYMENT
    # -------------------------
    else:
        session_id = payment_row["stripe_checkout_session_id"]
        if not session_id:
            return "failed"

        session = stripe.checkout.Session.retrieve(session_id)
        ps = session.get("payment_status")

        if ps == "paid":
            return "succeeded"
        elif ps in ("unpaid", "expired"):
            return "failed"

    return None


def update_payment(payment_id, status=None, invoice_url=None):
    conn = connect_to_rds()
    cur = conn.cursor()

    fields = []
    values = []

    if status:
        fields.append("status = %s")
        values.append(status)

    if invoice_url:
        fields.append("invoice_url = %s")
        values.append(invoice_url)

    if not fields:
        return

    values.append(payment_id)

    cur.execute(
        f"""
        UPDATE payments
        SET {", ".join(fields)}
        WHERE id = %s
        """,
        tuple(values),
    )

    conn.commit()
    cur.close()
    conn.close()


def reconcile_missing_invoice(payment_row):
    """
    Returns:
    - invoice_url (str) if found
    - "failed" if payment should be marked failed
    - None if no change
    """

    payment_type = payment_row["payment_type"]

    # -------------------------
    # SUBSCRIPTION
    # -------------------------
    if payment_type == "subscription":
        sub_id = payment_row["stripe_subscription_id"]
        if not sub_id:
            return "failed"

        sub = stripe.Subscription.retrieve(sub_id, expand=["latest_invoice"])

        if sub["status"] != "active":
            return "failed"

        invoice = sub.get("latest_invoice")
        if invoice and invoice.get("hosted_invoice_url"):
            return invoice["hosted_invoice_url"]

    # -------------------------
    # ONE-TIME PAYMENT
    # -------------------------
    else:
        session_id = payment_row["stripe_checkout_session_id"]
        if not session_id:
            return "failed"

        session = stripe.checkout.Session.retrieve(session_id, expand=["invoice"])

        invoice = session.get("invoice")
        if invoice and invoice.get("hosted_invoice_url"):
            return invoice["hosted_invoice_url"]

    return None


# -------------------------------------------------
# GET USER PAYMENTS
# -------------------------------------------------
@payments_bp.route("/payments/<user_id>", methods=["GET"])
def get_user_payments(user_id):
    conn = connect_to_rds()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    try:
        cur.execute(
            """
            SELECT *
            FROM payments
            WHERE user_id = %s
            ORDER BY created_at DESC
            """,
            (user_id,),
        )

        rows = cur.fetchall()

        for p in rows:
            # -------------------------
            # 1️⃣ Reconcile pending
            # -------------------------
            if p["status"] == "pending":
                new_status = reconcile_pending_payment(p)
                if new_status and new_status != p["status"]:
                    update_payment(p["id"], status=new_status)
                    p["status"] = new_status

            # -------------------------
            # 2️⃣ Fix succeeded but missing invoice
            # -------------------------
            if p["status"] == "succeeded" and not p["invoice_url"]:
                result = reconcile_missing_invoice(p)

                if result == "failed":
                    update_payment(p["id"], status="failed")
                    p["status"] = "failed"

                elif isinstance(result, str):
                    update_payment(p["id"], invoice_url=result)
                    p["invoice_url"] = result

        formatted = [format_payment_row(p) for p in rows]
        return jsonify({"payments": formatted})

    finally:
        cur.close()
        conn.close()
