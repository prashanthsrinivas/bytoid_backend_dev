from datetime import datetime
from decimal import Decimal
import json
from create_db import connect_to_rds
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
            print("saving to log file", PAYMENTS_LOG_FILE)
            json.dump(logs, f, indent=4, default=str)
    except Exception as e:
        print("❌ Failed to log payment event:", e)


@payments_bp.route("/payments/create-intent", methods=["POST"])
def create_payment_intent():
    body = request.json or {}

    user_id = body.get("user_id")
    price_id = body.get("price_id")
    description = body.get("description", "Payment")

    if not user_id or not price_id:
        return jsonify({"error": "user_id and price_id required"}), 400

    try:
        # 1️⃣ Fetch price from Stripe
        price = stripe.Price.retrieve(price_id)

        if not price.get("unit_amount"):
            return jsonify({"error": "Invalid price"}), 400

        amount = price["unit_amount"]
        currency = price["currency"]

        # 2️⃣ Create PaymentIntent
        intent = stripe.PaymentIntent.create(
            amount=amount,
            currency=currency,
            description=description,
            metadata={
                "user_id": user_id,
                "price_id": price_id,
                "product_id": price["product"],
                "type": "one_time_price_payment",
            },
        )

        return jsonify(
            {
                "client_secret": intent.client_secret,
                "amount": amount,
                "currency": currency,
            }
        )

    except stripe.error.StripeError as e:
        print("error on intent creation", e)
        return jsonify({"error": str(e)}), 400


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
    session = create_checkout_session(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        email=email,
        metadata={"user_id": user_id, "plan_code": plan_code, "type": "subscription"},
    )

    return jsonify({"url": session.url})


# -------------------------------------------------
# ONE-TIME PAYMENT / TOPUP
# -------------------------------------------------
@payments_bp.route("/payments/topup", methods=["POST"])
def topup():
    body = request.json or {}

    user_id = body.get("user_id")
    amount_cents = int(body.get("amount_cents"))  # MUST be int
    currency = body.get("currency", "INR").lower()
    email = body.get("email")

    if not user_id or not amount_cents or not currency:
        return jsonify({"error": "Invalid request"}), 400

    session = create_checkout_session(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": currency,
                    "unit_amount": amount_cents,
                    "product_data": {"name": "Token Topup"},
                },
                "quantity": 1,
            }
        ],
        metadata={"user_id": user_id, "type": "topup"},
        email=email,
    )

    return jsonify({"url": session.url})


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
        stripe.Subscription.delete(subscription_id)
        log_payment_event(
            {
                "action": "cancel_subscription",
                "subscription_id": subscription_id,
                "user_id": user_id,
            }
        )
        return jsonify({"status": "cancelled"})
    except Exception as e:
        print("❌ Error cancelling subscription:", e)
        return jsonify({"error": str(e)}), 400


# -------------------------------------------------
# STRIPE WEBHOOK
# -------------------------------------------------
@payments_bp.route("/stripe_webook", methods=["POST"])
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
