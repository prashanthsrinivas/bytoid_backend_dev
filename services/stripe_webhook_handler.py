# import json
# from datetime import datetime
# from db.db_checkers import get_userid
# from db.rds_db import connect_to_rds
# from utils.stripe_config import dev_stipe as stripe


# def ts_to_datetime(ts):
#     if not ts:
#         return None
#     return datetime.utcfromtimestamp(int(ts))


# class StripeWebhookHandler:
#     def __init__(self, event):
#         self.event = event
#         self.event_type = event.get("type", "unknown")
#         self.obj = event.get("data", {}).get("object", {})

#     # -------------------------------------------------
#     # ENTRY POINT
#     # -------------------------------------------------
#     def process(self):
#         handler_map = {
#             # Checkout
#             "checkout.session.completed": self.checkout_completed,
#             "checkout.session.async_payment_succeeded": self.checkout_async_succeeded,
#             "checkout.session.async_payment_failed": self.checkout_async_failed,
#             # PaymentIntent
#             "payment_intent.succeeded": self.payment_intent_succeeded,
#             "payment_intent.payment_failed": self.payment_intent_failed,
#             "payment_intent.canceled": self.payment_intent_canceled,
#             # Invoice / Subscription billing
#             "invoice.paid": self.invoice_paid,
#             # "invoice_payment.paid": self.invoice_paid,  # New Stripe event
#             "invoice.payment_failed": self.invoice_failed,
#             # Subscription lifecycle
#             "customer.subscription.created": self.subscription_created,
#             "customer.subscription.updated": self.subscription_updated,
#             "customer.subscription.deleted": self.subscription_deleted,
#             # Refund / Dispute / Payout
#             "refund.created": self.refund_created,
#             "dispute.created": self.dispute_created,
#             "payout.paid": self.payout_paid,
#         }

#         handler = handler_map.get(self.event_type)
#         if handler:
#             print("event type", self.event_type)
#             handler()
#         else:
#             self.unhandled_event()

#     # -------------------------------------------------
#     # DB HELPERS
#     # -------------------------------------------------
#     def save_payment(self, data):
#         user_id = data.get("user_id")
#         if not user_id:
#             print("❌ Cannot save payment, user_id missing")
#             return

#         connection = connect_to_rds()
#         if not connection:
#             return

#         cursor = connection.cursor()
#         try:
#             query = """
#             INSERT INTO payments
#             (
#                 user_id,
#                 stripe_event_id,
#                 stripe_payment_intent_id,
#                 stripe_checkout_session_id,
#                 stripe_invoice_id,
#                 amount_cents,
#                 currency,
#                 payment_type,
#                 status,
#                 invoice_url
#             )
#             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
#             AS new
#             ON DUPLICATE KEY UPDATE
#                 status = new.status,
#                 invoice_url = new.invoice_url,
#                 stripe_event_id = new.stripe_event_id,
#                 stripe_payment_intent_id = new.stripe_payment_intent_id;
#             """

#             cursor.execute(
#                 query,
#                 (
#                     user_id,
#                     data.get("stripe_event_id"),
#                     data.get("payment_intent_id"),
#                     data.get("checkout_session_id"),
#                     data.get("invoice_id"),
#                     data.get("amount_cents"),
#                     data.get("currency"),
#                     data.get("payment_type"),  # ENUM SAFE
#                     data.get("status"),  # ENUM SAFE
#                     data.get("invoice_url"),
#                 ),
#             )
#             connection.commit()

#         finally:
#             cursor.close()
#             connection.close()

#     def save_subscription(self, data):
#         # Ensure user_id is not None
#         user_id = data.get("user_id")
#         if not user_id:
#             email = data.get("email")
#             if email:
#                 user_id = get_userid(email)
#             else:
#                 print("❌ Cannot save subscription, user_id missing")
#                 return

#         connection = connect_to_rds()
#         if not connection:
#             return

#         cursor = connection.cursor()
#         try:
#             query = """
#             INSERT INTO subscriptions
#             (user_id, stripe_subscription_id, stripe_customer_id,
#             stripe_price_id, status,
#             current_period_start, current_period_end)
#             VALUES (%s,%s,%s,%s,%s,%s,%s) AS new
#             ON DUPLICATE KEY UPDATE
#                 status = new.status,
#                 current_period_end = new.current_period_end;
#             """

#             cursor.execute(
#                 query,
#                 (
#                     user_id,
#                     data.get("subscription_id"),
#                     data.get("customer_id"),
#                     data.get("price_id"),
#                     data.get("status"),
#                     ts_to_datetime(data.get("period_start")),
#                     ts_to_datetime(data.get("period_end")),
#                 ),
#             )
#             connection.commit()
#         finally:
#             cursor.close()
#             connection.close()

#     # -------------------------------------------------
#     # CHECKOUT
#     # -------------------------------------------------
#     def checkout_completed(self):
#         print("✅ Checkout completed:", self.obj.get("id"))

#     def checkout_async_succeeded(self):
#         print("✅ Checkout async succeeded:", self.obj.get("id"))

#     def checkout_async_failed(self):
#         print("❌ Checkout async failed:", self.obj.get("id"))

#     # -------------------------------------------------
#     # PAYMENT INTENT
#     # -------------------------------------------------
#     def payment_intent_succeeded(self):
#         payment_type = self.obj.get("metadata", {}).get("type")
#         email = self.obj.get("customer_email")
#         user_id = self.obj.get("metadata", {}).get("user_id") or get_userid(email)

#         if payment_type == "topup":
#             self.save_payment(
#                 {
#                     "user_id": user_id,
#                     "payment_type": "one_time",
#                     "status": "succeeded",
#                     "amount_cents": self.obj.get("amount"),
#                     "currency": self.obj.get("currency"),
#                     "payment_intent_id": self.obj.get("id"),
#                 }
#             )
#         else:
#             print("Skipping PaymentIntent for subscription; wait for invoice.paid")

#     def payment_intent_failed(self):
#         print(f"❌ PaymentIntent failed: {self.obj.get('id')}")
#         email = self.obj.get("customer_email")
#         user_id = self.obj.get("metadata", {}).get("user_id") or get_userid(email)

#         self.save_payment(
#             {
#                 "user_id": user_id,
#                 "payment_type": "one_time",
#                 "status": "failed",
#                 "amount_cents": self.obj.get("amount"),
#                 "currency": self.obj.get("currency"),
#                 "payment_intent_id": self.obj.get("id"),
#             }
#         )

#     def payment_intent_canceled(self):
#         print(f"⚠️ PaymentIntent canceled: {self.obj.get('id')}")

#     # -------------------------------------------------
#     # INVOICE / SUBSCRIPTION
#     # -------------------------------------------------
#     def invoice_paid(self):
#         invoice = self.obj
#         event = self.event

#         invoice_id = invoice.get("id")
#         print(f"📄 Invoice paid: {invoice_id}")

#         # -------------------------------------------------
#         # 1️⃣ Resolve user_id (robust)
#         # -------------------------------------------------
#         user_id = invoice.get("metadata", {}).get("user_id")

#         customer_id = invoice.get("customer")
#         customer_email = None

#         if not user_id and customer_id:
#             customer = stripe.Customer.retrieve(customer_id)
#             customer_email = getattr(customer, "email", None)
#             user_id = getattr(customer, "metadata", {}).get("user_id")

#         if not user_id and customer_email:
#             user_id = get_userid(customer_email)

#         if not user_id:
#             print(f"❌ Could not determine user_id for invoice {invoice_id}")
#             return

#         print(f"✅ user_id resolved: {user_id}")

#         # -------------------------------------------------
#         # 2️⃣ Save PAYMENT (invoice = payment record)
#         # -------------------------------------------------
#         amount = invoice.get("amount_paid") or 0
#         currency = invoice.get("currency")
#         invoice_url = invoice.get("hosted_invoice_url")
#         payment_intent_id = invoice.get("payment_intent")

#         print(
#             f"💰 Saving payment | amount={amount} {currency}, "
#             f"payment_intent={payment_intent_id}"
#         )

#         self.save_payment(
#             {
#                 "user_id": user_id,
#                 "stripe_event_id": event.get("id"),
#                 "payment_type": "subscription",
#                 "status": "succeeded",  # ✅ ENUM SAFE
#                 "amount_cents": amount,
#                 "currency": currency,
#                 "payment_intent_id": payment_intent_id,
#                 "invoice_id": invoice_id,
#                 "invoice_url": invoice_url,
#             }
#         )

#         # -------------------------------------------------
#         # 3️⃣ Save SUBSCRIPTION (from invoice lines)
#         # -------------------------------------------------
#         lines = invoice.get("lines", {}).get("data", [])

#         for line in lines:
#             subscription_id = line.get("subscription") or line.get("parent", {}).get(
#                 "subscription_item_details", {}
#             ).get("subscription")

#             pricing = line.get("pricing", {}).get("price_details", {})
#             price_id = pricing.get("price")

#             period = line.get("period", {})
#             period_start = period.get("start")
#             period_end = period.get("end")

#             if not subscription_id or not price_id:
#                 continue

#             self.save_subscription(
#                 {
#                     "user_id": user_id,
#                     "subscription_id": subscription_id,
#                     "customer_id": customer_id,
#                     "price_id": price_id,
#                     "status": "active",
#                     "period_start": period_start,
#                     "period_end": period_end,
#                 }
#             )

#             print(
#                 f"✅ Subscription saved: user={user_id}, "
#                 f"sub={subscription_id}, price={price_id}"
#             )

#     def invoice_failed(self):
#         print(f"❌ Invoice payment failed: {self.obj.get('id')}")

#     # -------------------------------------------------
#     # SUBSCRIPTIONS
#     # -------------------------------------------------
#     def subscription_created(self):
#         print(f"🔔 Subscription created: {self.obj.get('id')}")

#     def subscription_updated(self):
#         print(f"🔔 Subscription updated: {self.obj.get('id')}")

#     def subscription_deleted(self):
#         print(f"🔔 Subscription deleted: {self.obj.get('id')}")

#         user_id = self.obj.get("metadata", {}).get("user_id")
#         if not user_id:
#             customer_id = self.obj.get("customer")
#             if customer_id:
#                 customer = stripe.Customer.retrieve(customer_id)
#                 user_id = customer.metadata.get("user_id") or get_userid(customer.email)

#         self.save_subscription(
#             {
#                 "user_id": user_id,
#                 "subscription_id": self.obj.get("id"),
#                 "customer_id": self.obj.get("customer"),
#                 "price_id": self.obj.get("items", {})
#                 .get("data", [{}])[0]
#                 .get("price", {})
#                 .get("id"),
#                 "status": "canceled",
#                 "period_start": self.obj.get("current_period_start"),
#                 "period_end": self.obj.get("current_period_end"),
#             }
#         )

#     # -------------------------------------------------
#     # REFUNDS / DISPUTES / PAYOUTS
#     # -------------------------------------------------
#     def refund_created(self):
#         print(f"↩️ Refund created: {self.obj.get('id')}")

#     def dispute_created(self):
#         print(f"⚖️ Dispute created: {self.obj.get('id')}")

#     def payout_paid(self):
#         print(f"🏦 Payout paid: {self.obj.get('id')}")

#     # -------------------------------------------------
#     # FALLBACK
#     # -------------------------------------------------
#     def unhandled_event(self):
#         print(f"ℹ️ Unhandled event: {self.event_type}")
import json
from datetime import datetime
from db.db_checkers import get_userid
from db.rds_db import connect_to_rds
from utils.stripe_config import dev_stipe as stripe


# -------------------------------------------------
# HELPERS
# -------------------------------------------------
def ts_to_datetime(ts):
    if not ts:
        return None
    return datetime.utcfromtimestamp(int(ts))


# -------------------------------------------------
# STRIPE WEBHOOK HANDLER
# -------------------------------------------------
class StripeWebhookHandler:
    def __init__(self, event):
        self.event = event
        self.event_type = event.get("type", "unknown")
        self.obj = event.get("data", {}).get("object", {})

    # -------------------------------------------------
    # ENTRY POINT
    # -------------------------------------------------
    def process(self):
        handler_map = {
            "checkout.session.completed": self.checkout_completed,
            "checkout.session.async_payment_succeeded": self.checkout_completed,
            "checkout.session.async_payment_failed": self.checkout_failed,
            "payment_intent.succeeded": self.payment_intent_succeeded,
            "payment_intent.payment_failed": self.payment_intent_failed,
            "payment_intent.canceled": self.payment_intent_failed,
            "invoice.paid": self.invoice_paid,
            "invoice.payment_failed": self.invoice_failed,
            "customer.subscription.created": self.subscription_created,
            "customer.subscription.updated": self.subscription_updated,
            "customer.subscription.deleted": self.subscription_deleted,
        }

        handler = handler_map.get(self.event_type)
        if handler:
            print("➡️ Handling event:", self.event_type)
            handler()
        else:
            self.unhandled_event()

    # -------------------------------------------------
    # DB LOOKUPS (CRITICAL)
    # -------------------------------------------------
    def _get_existing_checkout_session(self, payment_intent_id, invoice_id):
        """Never lose checkout_session_id"""
        conn = connect_to_rds()
        if not conn:
            return None

        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT stripe_checkout_session_id
                FROM payments
                WHERE stripe_payment_intent_id = %s
                   OR stripe_invoice_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (payment_intent_id, invoice_id),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None
        finally:
            cur.close()
            conn.close()

    # -------------------------------------------------
    # PAYMENT SAVE (HARD GUARANTEES)
    # -------------------------------------------------
    def save_payment(self, data):
        user_id = data.get("user_id")
        if not user_id:
            print("❌ Payment skipped (missing user_id)")
            return

        payment_intent_id = data.get("payment_intent_id")
        invoice_id = data.get("invoice_id")

        # 🔐 GUARANTEE checkout_session_id
        checkout_session_id = data.get("checkout_session_id")
        if not checkout_session_id:
            checkout_session_id = self._get_existing_checkout_session(
                payment_intent_id, invoice_id
            )

        conn = connect_to_rds()
        if not conn:
            return

        cur = conn.cursor()
        try:
            query = """
            INSERT INTO payments
            (
                user_id,
                stripe_event_id,
                stripe_payment_intent_id,
                stripe_checkout_session_id,
                stripe_invoice_id,
                amount_cents,
                currency,
                payment_type,
                status,
                invoice_url
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            AS new
            ON DUPLICATE KEY UPDATE
                status = new.status,
                invoice_url = COALESCE(new.invoice_url, invoice_url),
                stripe_event_id = new.stripe_event_id,
                stripe_payment_intent_id = COALESCE(new.stripe_payment_intent_id, stripe_payment_intent_id),
                stripe_checkout_session_id = COALESCE(stripe_checkout_session_id, new.stripe_checkout_session_id);
            """

            cur.execute(
                query,
                (
                    user_id,
                    data.get("stripe_event_id"),
                    payment_intent_id,
                    checkout_session_id,
                    invoice_id,
                    data.get("amount_cents"),
                    data.get("currency"),
                    data.get("payment_type"),
                    data.get("status"),
                    data.get("invoice_url"),
                ),
            )
            conn.commit()
            print("💾 Payment saved")

        finally:
            cur.close()
            conn.close()

    # -------------------------------------------------
    # SUBSCRIPTIONS
    # -------------------------------------------------
    def save_subscription(self, data):
        if not data.get("user_id"):
            print("❌ Subscription skipped (missing user_id)")
            return

        conn = connect_to_rds()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO subscriptions
                (user_id, stripe_subscription_id, stripe_customer_id,
                 stripe_price_id, status,
                 current_period_start, current_period_end)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                AS new
                ON DUPLICATE KEY UPDATE
                    status = new.status,
                    current_period_end = new.current_period_end;
                """,
                (
                    data["user_id"],
                    data["subscription_id"],
                    data["customer_id"],
                    data["price_id"],
                    data["status"],
                    ts_to_datetime(data["period_start"]),
                    ts_to_datetime(data["period_end"]),
                ),
            )
            conn.commit()
            print("📦 Subscription saved")
        finally:
            cur.close()
            conn.close()

    # -------------------------------------------------
    # CHECKOUT
    # -------------------------------------------------
    def checkout_completed(self):
        session = self.obj
        user_id = session.get("metadata", {}).get("user_id")
        email = session.get("customer_details", {}).get("email")
        user_id = user_id or get_userid(email)

        self.save_payment(
            {
                "user_id": user_id,
                "stripe_event_id": self.event.get("id"),
                "payment_type": (
                    "subscription"
                    if session.get("mode") == "subscription"
                    else "one_time"
                ),
                "status": "pending",
                "amount_cents": session.get("amount_total"),
                "currency": session.get("currency"),
                "payment_intent_id": session.get("payment_intent"),
                "checkout_session_id": session.get("id"),
            }
        )

    def checkout_failed(self):
        print("❌ Checkout failed:", self.obj.get("id"))

    # -------------------------------------------------
    # PAYMENT INTENT
    # -------------------------------------------------
    def payment_intent_succeeded(self):
        pi = self.obj
        email = pi.get("receipt_email")
        user_id = pi.get("metadata", {}).get("user_id") or get_userid(email)

        self.save_payment(
            {
                "user_id": user_id,
                "stripe_event_id": self.event.get("id"),
                "payment_type": "one_time",
                "status": "succeeded",
                "amount_cents": pi.get("amount"),
                "currency": pi.get("currency"),
                "payment_intent_id": pi.get("id"),
            }
        )

    def payment_intent_failed(self):
        pi = self.obj
        email = pi.get("receipt_email")
        user_id = pi.get("metadata", {}).get("user_id") or get_userid(email)

        self.save_payment(
            {
                "user_id": user_id,
                "stripe_event_id": self.event.get("id"),
                "payment_type": "one_time",
                "status": "failed",
                "amount_cents": pi.get("amount"),
                "currency": pi.get("currency"),
                "payment_intent_id": pi.get("id"),
            }
        )

    # -------------------------------------------------
    # INVOICE
    # -------------------------------------------------
    def invoice_paid(self):
        invoice = self.obj
        customer_id = invoice.get("customer")

        customer = stripe.Customer.retrieve(customer_id)
        user_id = customer.metadata.get("user_id") or get_userid(customer.email)

        self.save_payment(
            {
                "user_id": user_id,
                "stripe_event_id": self.event.get("id"),
                "payment_type": "subscription",
                "status": "succeeded",
                "amount_cents": invoice.get("amount_paid"),
                "currency": invoice.get("currency"),
                "payment_intent_id": invoice.get("payment_intent"),
                "invoice_id": invoice.get("id"),
                "invoice_url": invoice.get("hosted_invoice_url"),
            }
        )

        for line in invoice.get("lines", {}).get("data", []):
            if not line.get("subscription"):
                continue

            self.save_subscription(
                {
                    "user_id": user_id,
                    "subscription_id": line.get("subscription"),
                    "customer_id": customer_id,
                    "price_id": line.get("price", {}).get("id"),
                    "status": "active",
                    "period_start": line["period"]["start"],
                    "period_end": line["period"]["end"],
                }
            )

    def invoice_failed(self):
        print("❌ Invoice payment failed:", self.obj.get("id"))

    # -------------------------------------------------
    # SUBSCRIPTION LIFECYCLE
    # -------------------------------------------------
    def subscription_created(self):
        print("🔔 Subscription created:", self.obj.get("id"))

    def subscription_updated(self):
        print("🔔 Subscription updated:", self.obj.get("id"))

    def subscription_deleted(self):
        print("🛑 Subscription deleted:", self.obj.get("id"))

    # -------------------------------------------------
    # FALLBACK
    # -------------------------------------------------
    def unhandled_event(self):
        print("ℹ️ Unhandled Stripe event:", self.event_type)
