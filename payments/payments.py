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
# @payments_bp.route("/payments/subscribe", methods=["POST"])
# def subscribe():
#     body = request.json or {}

#     user_id = body.get("user_id")
#     email = body.get("email")
#     plan_code = body.get("plan_code")
#     price_id = body.get("price_id")

#     if not user_id or not price_id:
#         return jsonify({"error": "user_id & price_id required"}), 400

#     # -----------------------------
#     # 1️⃣ Check if user already has active subscription
#     # -----------------------------
#     conn = connect_to_rds()
#     if not conn:
#         return jsonify({"error": "DB connection failed"}), 500

#     try:
#         cur = conn.cursor()
#         cur.execute(
#             """
#             SELECT stripe_subscription_id, status
#             FROM subscriptions
#             WHERE user_id = %s
#             ORDER BY created_at DESC
#             LIMIT 1
#             """,
#             (user_id,),
#         )
#         row = cur.fetchone()
#         if row:
#             subscription_id, status = row
#             if status == "active":
#                 return (
#                     jsonify(
#                         {
#                             "error": "User already has an active subscription",
#                             "subscription_id": subscription_id,
#                             "status": status,
#                         }
#                     ),
#                     400,
#                 )
#     finally:
#         cur.close()
#         conn.close()

#     # -----------------------------
#     # 2️⃣ Create new subscription checkout session
#     # -----------------------------
#     session = create_checkout_session(
#         mode="subscription",
#         line_items=[{"price": price_id, "quantity": 1}],
#         email=email,
#         metadata={"user_id": user_id, "plan_code": plan_code, "type": "subscription"},
#     )

#     return jsonify({"url": session.url, "checkout_session_id": session.id})


# @payments_bp.route("/payments/subscribe", methods=["POST"])
# def subscribe():
#     body = request.json or {}

#     user_id = body.get("user_id")
#     email = body.get("email")
#     plan_code = body.get("plan_code")
#     price_id = body.get("price_id")  # New plan Stripe price ID

#     if not user_id or not price_id:
#         return jsonify({"error": "user_id & price_id required"}), 400

#     conn = connect_to_rds()
#     if not conn:
#         return jsonify({"error": "DB connection failed"}), 500

#     try:
#         cur = conn.cursor()

#         # -----------------------------
#         # 1️⃣ Check active subscription
#         # -----------------------------
#         cur.execute(
#             """
#             SELECT stripe_subscription_id, status
#             FROM subscriptions
#             WHERE user_id = %s
#             ORDER BY created_at DESC
#             LIMIT 1
#             """,
#             (user_id,),
#         )
#         row = cur.fetchone()

#         # Get new plan amount
#         cur.execute(
#             "SELECT amount_cents FROM plans WHERE stripe_price_id = %s",
#             (price_id,),
#         )
#         plan_row = cur.fetchone()
#         if not plan_row:
#             return jsonify({"error": "New plan not found"}), 400

#         new_price_cents = plan_row[0]

#         if row:
#             subscription_id, status = row
#             if status == "active":
#                 # -----------------------------
#                 # 2️⃣ Fetch current subscription amount
#                 # -----------------------------
#                 subscription = stripe.Subscription.retrieve(subscription_id)
#                 # current_price_id = subscription.items.data[0].price.id
#                 current_price_id = subscription["items"]["data"][0]["price"]["id"]

#                 cur.execute(
#                     "SELECT amount_cents FROM plans WHERE stripe_price_id = %s",
#                     (current_price_id,),
#                 )
#                 current_plan_row = cur.fetchone()
#                 if not current_plan_row:
#                     return jsonify({"error": "Current plan not found"}), 400

#                 current_price_cents = current_plan_row[0]

#                 # -----------------------------
#                 # 3️⃣ Ensure new plan is higher
#                 # -----------------------------
#                 if new_price_cents <= current_price_cents:
#                     return (
#                         jsonify(
#                             {
#                                 "error": "New plan amount must be greater than current plan",
#                                 "current_price_cents": current_price_cents,
#                                 "new_price_cents": new_price_cents,
#                             }
#                         ),
#                         400,
#                     )

#                 # -----------------------------
#                 # 4️⃣ Upgrade subscription
#                 # -----------------------------
#                 updated_subscription = stripe.Subscription.modify(
#                     subscription_id,
#                     cancel_at_period_end=False,
#                     proration_behavior="create_prorations",
#                     items=[
#                         {
#                             "id": subscription["items"]["data"][0]["id"],  # <- fixed
#                             "price": price_id,
#                         }
#                     ],
#                     metadata={"plan_code": plan_code, "user_id": user_id},
#                 )

#                 return jsonify(
#                     {
#                         "message": "Subscription upgraded successfully",
#                         "subscription_id": updated_subscription.id,
#                     }
#                 )

#         # -----------------------------
#         # 5️⃣ No active subscription → create new
#         # -----------------------------
#         session = create_checkout_session(
#             mode="subscription",
#             line_items=[{"price": price_id, "quantity": 1}],
#             email=email,
#             metadata={
#                 "user_id": user_id,
#                 "plan_code": plan_code,
#                 "type": "subscription",
#             },
#         )

#         return jsonify({"url": session.url, "checkout_session_id": session.id})

#     finally:
#         cur.close()
#         conn.close()


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

    try:
        # -----------------------------
        # 1️⃣ Fetch new plan amount
        # -----------------------------
        cur.execute(
            "SELECT amount_cents FROM plans WHERE stripe_price_id = %s",
            (price_id,),
        )
        plan_row = cur.fetchone()
        if not plan_row:
            return jsonify({"error": "Plan not found"}), 400

        new_price_cents = plan_row[0]

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
            print("current price", current_price_cents)
            print("new price cents", new_price_cents)

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
            # stripe.Subscription.modify(
            #     subscription_id,
            #     cancel_at_period_end=False,
            #     proration_behavior="create_prorations",
            #     items=[
            #         {
            #             "id": subscription["items"]["data"][0]["id"],
            #             "price": price_id,
            #         }
            #     ],
            #     metadata={
            #         "user_id": user_id,
            #         "plan_code": plan_code,
            #     },
            # )
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
def topup():
    body = request.json or {}

    user_id = body.get("user_id")
    amount_cents = int(body.get("amount_cents"))  # MUST be int
    currency = body.get("currency", "USD").lower()
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
        # Cancel in Stripe — webhook will handle DB update
        stripe.Subscription.delete(subscription_id)

        log_payment_event(
            {
                "action": "cancel_subscription",
                "subscription_id": subscription_id,
                "user_id": user_id,
            }
        )
        return jsonify({"status": "cancellation_requested"})
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


# -------------------------------------------------
# GET USER SUBSCRIPTIONS
# -------------------------------------------------
@payments_bp.route("/payments/subscriptions/<user_id>", methods=["GET"])
def get_user_subscriptions(user_id):
    conn = connect_to_rds()
    if not conn:
        return jsonify({"error": "DB connection failed"}), 500

    cur = conn.cursor(dictionary=True)
    try:
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
        return jsonify({"subscriptions": subs})
    finally:
        cur.close()
        conn.close()


# -------------------------------------------------
# GET USER PAYMENTS
# -------------------------------------------------
@payments_bp.route("/payments/<user_id>", methods=["GET"])
def get_user_payments(user_id):
    conn = connect_to_rds()
    if not conn:
        return jsonify({"error": "DB connection failed"}), 500

    cur = conn.cursor(dictionary=True)
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
        payments = cur.fetchall()
        return jsonify({"payments": payments})
    finally:
        cur.close()
        conn.close()
