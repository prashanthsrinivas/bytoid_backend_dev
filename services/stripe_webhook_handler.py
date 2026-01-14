import json
from datetime import datetime, timezone
from db.db_checkers import get_userid
from db.rds_db import connect_to_rds
from utils.stripe_config import dev_stipe as stripe


# -------------------------------------------------
# HELPERS
# -------------------------------------------------


def ts_to_datetime(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


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
    # DB LOOKUP (CRITICAL)
    # -------------------------------------------------
    def _get_existing_checkout_session(
        self, payment_intent_id=None, invoice_id=None, subscription_id=None
    ):
        """
        NEVER lose checkout_session_id.
        Try to resolve from any known identifier.
        """
        conn = connect_to_rds()
        if not conn:
            return None

        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT stripe_checkout_session_id
                FROM payments
                WHERE
                    (%s IS NOT NULL AND stripe_payment_intent_id = %s)
                 OR (%s IS NOT NULL AND stripe_invoice_id = %s)
                 OR (%s IS NOT NULL AND stripe_subscription_id = %s)
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    payment_intent_id,
                    payment_intent_id,
                    invoice_id,
                    invoice_id,
                    subscription_id,
                    subscription_id,
                ),
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
        subscription_id = data.get("subscription_id")
        checkout_session_id = data.get("checkout_session_id")
        payment_type = data.get("payment_type")

        # 🔐 Guarantee checkout_session_id
        if not checkout_session_id:
            checkout_session_id = self._get_existing_checkout_session(
                payment_intent_id, invoice_id, subscription_id
            )

        conn = connect_to_rds()
        if not conn:
            return

        cur = conn.cursor()
        try:
            # 1️⃣ If it's a subscription, check for the latest payment for same subscription in last 10 seconds
            existing_payment_id = None
            if subscription_id:
                cur.execute(
                    """
                    SELECT id, created_at
                    FROM payments
                    WHERE stripe_subscription_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (subscription_id,),
                )
                row = cur.fetchone()
                if row:
                    existing_id, created_at = row
                    # If last payment is within 10 seconds, consider it same transaction (merge session + invoice)
                    time_diff = (datetime.utcnow() - created_at).total_seconds()
                    if time_diff <= 10:
                        existing_payment_id = existing_id

            # 2️⃣ If not subscription or no recent payment, check for unique identifiers
            if not existing_payment_id:
                cur.execute(
                    """
                    SELECT id
                    FROM payments
                    WHERE (stripe_payment_intent_id IS NOT NULL AND stripe_payment_intent_id = %s)
                    OR (stripe_checkout_session_id IS NOT NULL AND stripe_checkout_session_id = %s)
                    OR (stripe_invoice_id IS NOT NULL AND stripe_invoice_id = %s)
                    LIMIT 1
                    """,
                    (payment_intent_id, checkout_session_id, invoice_id),
                )
                row = cur.fetchone()
                if row:
                    existing_payment_id = row[0]

            # 3️⃣ If found, update
            if existing_payment_id:
                cur.execute(
                    """
                    UPDATE payments
                    SET
                        user_id = %s,
                        stripe_event_id = %s,
                        stripe_payment_intent_id = COALESCE(%s, stripe_payment_intent_id),
                        stripe_checkout_session_id = COALESCE(%s, stripe_checkout_session_id),
                        stripe_invoice_id = COALESCE(%s, stripe_invoice_id),
                        stripe_subscription_id = COALESCE(%s, stripe_subscription_id),
                        amount_cents = %s,
                        currency = %s,
                        payment_type = %s,
                        status = %s,
                        invoice_url = COALESCE(%s, invoice_url)
                    WHERE id = %s
                    """,
                    (
                        user_id,
                        data.get("stripe_event_id"),
                        payment_intent_id,
                        checkout_session_id,
                        invoice_id,
                        subscription_id,
                        data.get("amount_cents"),
                        data.get("currency"),
                        payment_type,
                        data.get("status"),
                        data.get("invoice_url"),
                        existing_payment_id,
                    ),
                )
                print(f"💾 Payment merged with existing (id={existing_payment_id})")

            # 4️⃣ Otherwise, insert new
            else:
                cur.execute(
                    """
                    INSERT INTO payments
                    (
                        user_id,
                        stripe_event_id,
                        stripe_payment_intent_id,
                        stripe_checkout_session_id,
                        stripe_invoice_id,
                        stripe_subscription_id,
                        amount_cents,
                        currency,
                        payment_type,
                        status,
                        invoice_url
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        user_id,
                        data.get("stripe_event_id"),
                        payment_intent_id,
                        checkout_session_id,
                        invoice_id,
                        subscription_id,
                        data.get("amount_cents"),
                        data.get("currency"),
                        payment_type,
                        data.get("status"),
                        data.get("invoice_url"),
                    ),
                )
                print("💾 Payment inserted as new")

            conn.commit()

        finally:
            cur.close()
            conn.close()

    # -------------------------------------------------
    # SUBSCRIPTIONS
    # -------------------------------------------------
    def save_subscription(self, data):
        if not data.get("user_id") or not data.get("subscription_id"):
            print("❌ Subscription skipped (missing data)")
            return

        conn = connect_to_rds()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO subscriptions
                (
                    user_id,
                    stripe_subscription_id,
                    stripe_customer_id,
                    stripe_price_id,
                    status,
                    current_period_start,
                    current_period_end
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                AS new
                ON DUPLICATE KEY UPDATE
                    status = new.status,
                    current_period_start = new.current_period_start,
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

    def update_subscription_status(self, subscription):
        stripe_status = subscription.get("status")

        status_map = {
            "active": "active",
            "past_due": "past_due",
            "unpaid": "past_due",
            "incomplete": "incomplete",
            "canceled": "canceled",
        }

        db_status = status_map.get(stripe_status, "incomplete")

        conn = connect_to_rds()
        if not conn:
            return

        cur = conn.cursor()
        try:
            cur.execute(
                """
                UPDATE subscriptions
                SET
                    status = %s,
                    current_period_start = %s,
                    current_period_end = %s,
                    stripe_price_id = %s,
                    updated_at = NOW()
                WHERE stripe_subscription_id = %s
                """,
                (
                    db_status,
                    ts_to_datetime(subscription.get("current_period_start")),
                    ts_to_datetime(subscription.get("current_period_end")),
                    subscription.get("items", {})
                    .get("data", [{}])[0]
                    .get("price", {})
                    .get("id"),
                    subscription.get("id"),
                ),
            )
            conn.commit()
            print(f"🔄 Subscription status updated → {db_status}")
        finally:
            cur.close()
            conn.close()

    # -------------------------------------------------
    # CHECKOUT
    # -------------------------------------------------
    def checkout_completed(self):
        session = self.obj

        metadata = session.get("metadata", {})
        user_id = metadata.get("user_id")

        if not user_id:
            email = session.get("customer_details", {}).get("email")
            user_id = get_userid(email)

        mode = session.get("mode")  # "payment" or "subscription"

        is_subscription = mode == "subscription"
        is_one_time = mode == "payment"

        # -------------------------
        # Determine payment type
        # -------------------------
        payment_type = "subscription" if is_subscription else "one_time"

        # -------------------------
        # Determine payment status
        # -------------------------
        receipt_url = None
        if is_one_time:
            # checkout.session.completed is FINAL for one-time
            if session.get("payment_status") == "paid":
                status = "succeeded"
                pi_id = session.get("payment_intent")
                if pi_id:
                    pi = stripe.PaymentIntent.retrieve(pi_id)
                    # print("pi", pi)

                    charge_id = pi.get("latest_charge")
                    if charge_id:
                        charge = stripe.Charge.retrieve(charge_id)
                        # print("charge", charge)
                        receipt_url = charge.get("receipt_url")
            else:
                # async methods (UPI, netbanking)
                status = "pending"

        else:
            # subscriptions complete on invoice.paid
            status = "pending"

        # -------------------------
        # Save payment
        # -------------------------
        self.save_payment(
            {
                "user_id": user_id,
                "stripe_event_id": self.event.get("id"),
                "payment_type": payment_type,
                "status": status,
                "amount_cents": session.get("amount_total"),
                "currency": session.get("currency"),
                "payment_intent_id": session.get("payment_intent"),
                "checkout_session_id": session.get("id"),
                # subscription-only
                "subscription_id": session.get("subscription"),
                # invoice-only (will be set later for subs)
                "invoice_id": receipt_url,
            }
        )

        # # -------------------------
        # # Business logic
        # # -------------------------
        # if is_one_time and status == "paid":
        #     self.credit_topup(user_id, session.get("amount_total"))

        # if is_subscription:
        #     print("📌 Subscription created, waiting for invoice.paid")

    def checkout_failed(self):
        print("❌ Checkout failed:", self.obj.get("id"))

    # -------------------------------------------------
    # PAYMENT INTENT (ONE-TIME ONLY)
    # -------------------------------------------------
    def payment_intent_succeeded(self):
        pi = self.obj
        if pi.get("metadata", {}).get("type") == "subscription":
            return

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
    # INVOICE (FINAL AUTHORITY FOR SUBSCRIPTIONS)
    # -------------------------------------------------
    def invoice_paid(self):
        invoice = self.obj
        # print("invoice obj", invoice)

        # Get customer info
        customer = stripe.Customer.retrieve(invoice.get("customer"))
        user_id = customer.metadata.get("user_id") or get_userid(customer.email)

        # Attempt to get subscription_id from multiple sources
        subscription_id = invoice.get("subscription")

        if not subscription_id:
            # 1️⃣ Try first line item parent details
            lines = invoice.get("lines", {}).get("data", [])
            for line in lines:
                parent = line.get("parent", {})
                subscription_details = parent.get(
                    "subscription_item_details"
                ) or parent.get("subscription_details")
                if subscription_details and subscription_details.get("subscription"):
                    subscription_id = subscription_details["subscription"]
                    break

        if not subscription_id:
            # 2️⃣ Fallback to invoice.parent.subscription_details
            subscription_id = (
                invoice.get("parent", {})
                .get("subscription_details", {})
                .get("subscription")
            )

        # Save the payment
        self.save_payment(
            {
                "user_id": user_id,
                "stripe_event_id": self.event.get("id"),
                "payment_type": "subscription" if subscription_id else "one_time",
                "status": "succeeded",
                "amount_cents": invoice.get("amount_paid"),
                "currency": invoice.get("currency"),
                "payment_intent_id": invoice.get("payment_intent"),
                "invoice_id": invoice.get("id"),
                "invoice_url": invoice.get("hosted_invoice_url"),
                "subscription_id": subscription_id,
                "checkout_session_id": self._get_existing_checkout_session(
                    invoice.get("payment_intent"), invoice.get("id"), subscription_id
                ),
            }
        )

        # Save subscription if we have subscription_id
        if subscription_id:
            # Use first line item for price/period
            line = lines[0] if lines else {}
            price_id = line.get("price", {}).get("id") or line.get("pricing", {}).get(
                "price_details", {}
            ).get("price")
            period = line.get("period", {})

            self.save_subscription(
                {
                    "user_id": user_id,
                    "subscription_id": subscription_id,
                    "customer_id": invoice.get("customer"),
                    "price_id": price_id,
                    "status": "active",
                    "period_start": period.get("start") or invoice.get("period_start"),
                    "period_end": period.get("end") or invoice.get("period_end"),
                }
            )

    def invoice_failed(self):
        invoice = self.obj

        # Get customer
        customer_id = invoice.get("customer")
        customer = stripe.Customer.retrieve(customer_id)
        user_id = customer.metadata.get("user_id") or get_userid(customer.email)

        # Try to resolve subscription_id
        subscription_id = invoice.get("subscription")

        if not subscription_id:
            lines = invoice.get("lines", {}).get("data", [])
            for line in lines:
                parent = line.get("parent", {})
                sub_details = parent.get("subscription_item_details") or parent.get(
                    "subscription_details"
                )
                if sub_details and sub_details.get("subscription"):
                    subscription_id = sub_details["subscription"]
                    break

        if not subscription_id:
            print("⚠️ invoice_failed without subscription:", invoice.get("id"))
            return

        # Fetch subscription from Stripe to get real status
        sub = stripe.Subscription.retrieve(subscription_id)

        # Update subscription status
        self.update_subscription_status(sub)

        # Save failed payment record (optional but recommended)
        self.save_payment(
            {
                "user_id": user_id,
                "stripe_event_id": self.event.get("id"),
                "payment_type": "subscription",
                "status": "failed",
                "amount_cents": invoice.get("amount_due"),
                "currency": invoice.get("currency"),
                "payment_intent_id": invoice.get("payment_intent"),
                "invoice_id": invoice.get("id"),
                "invoice_url": invoice.get("hosted_invoice_url"),
                "subscription_id": subscription_id,
            }
        )

        print("❌ Subscription payment failed:", subscription_id)

    # -------------------------------------------------
    # SUBSCRIPTION LIFECYCLE
    # -------------------------------------------------
    def subscription_created(self):
        print("🔔 Subscription created:", self.obj.get("id"))

    def subscription_updated(self):
        # sub = self.obj
        # self.update_subscription_status(sub)

        # ❌ No email logic
        print("🔄 Subscription updated:", self.obj.get("id"))

    def subscription_deleted(self):
        sub = self.obj

        conn = connect_to_rds()
        if not conn:
            return

        cur = conn.cursor()
        try:
            cur.execute(
                """
                UPDATE subscriptions
                SET status = 'canceled', updated_at = NOW()
                WHERE stripe_subscription_id = %s
                """,
                (sub.get("id"),),
            )
            conn.commit()
            print("🛑 Subscription canceled:", sub.get("id"))
        finally:
            cur.close()
            conn.close()

    # -------------------------------------------------
    # FALLBACK
    # -------------------------------------------------
    def unhandled_event(self):
        print("ℹ️ Unhandled Stripe event:", self.event_type)
