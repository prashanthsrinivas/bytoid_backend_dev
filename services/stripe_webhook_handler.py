import json
from datetime import datetime, timedelta, timezone
from db.db_checkers import get_userid
from db.rds_db import connect_to_rds
from services.credit_system import CreditManager
from utils.stripe_config import dev_stipe as stripe
from utils.app_configs import IS_DEV
from utils.base_logger import get_logger

logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")


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

    @staticmethod
    def get_token_by_planidorpriceid(plancode=None, price_id=None):
        conn = connect_to_rds()
        cur = conn.cursor()

        try:
            if price_id:

                # --- 2️⃣ Get token amount from plans table ---
                cur.execute(
                    "SELECT monthly_token_limit FROM plans WHERE stripe_price_id = %s LIMIT 1",
                    (price_id,),
                )
                row = cur.fetchone()
                if row and row[0] is not None:
                    return row[0]
            if plancode:
                cur.execute(
                    "SELECT monthly_token_limit FROM plans WHERE plan_code = %s LIMIT 1",
                    (plancode,),
                )
                row = cur.fetchone()
                if row and row[0] is not None:
                    return row[0]

            # --- 3️⃣ Fallback: use metadata from invoice/checkout session ---
            # You can pass invoice or session as a parameter if needed
            logger.warning("Price %s not found in plans, fallback to metadata", price_id)
            return 0

        finally:
            cur.close()
            conn.close()

    def _get_subscription_tokens(self, subscription_id):
        """
        Return the number of tokens for a subscription.
        1️⃣ Look up the subscription's price_id
        2️⃣ Get monthly_token_limit from plans
        3️⃣ Fallback to metadata if needed
        """
        conn = connect_to_rds()
        cur = conn.cursor()

        try:
            # --- 1️⃣ Get price_id from subscriptions table ---
            cur.execute(
                "SELECT stripe_price_id FROM subscriptions WHERE stripe_subscription_id = %s LIMIT 1",
                (subscription_id,),
            )
            row = cur.fetchone()
            if not row:
                # print(f"⚠️ Subscription {subscription_id} not found")
                return 0

            price_id = row[0]

            # --- 2️⃣ Get token amount from plans table ---
            cur.execute(
                "SELECT monthly_token_limit FROM plans WHERE stripe_price_id = %s LIMIT 1",
                (price_id,),
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                return row[0]

            # --- 3️⃣ Fallback: use metadata from invoice/checkout session ---
            # You can pass invoice or session as a parameter if needed
            # print(f"⚠️ Price {price_id} not found in plans, fallback to metadata")
            return 0

        finally:
            cur.close()
            conn.close()

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
            # print("➡️ Handling event:", self.event_type)
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
        """
        Save Stripe payment, merging checkout.session.completed and invoice.paid events.
        Event-order aware: never downgrade succeeded payments, and always attach session ID.
        """
        STATUS_PRIORITY = {"pending": 1, "succeeded": 2, "failed": 3}

        user_id = data.get("user_id")
        if not user_id:
            # print("❌ Payment skipped (missing user_id)")
            return

        conn = connect_to_rds()
        if not conn:
            # print("❌ DB connection failed")
            return

        try:
            cur = conn.cursor()
            existing_payment_id = None
            old_status = None
            old_checkout_session_id = None

            invoice_id = data.get("invoice_id")
            payment_intent_id = data.get("payment_intent_id")
            subscription_id = data.get("subscription_id")
            amount_cents = data.get("amount_cents")

            # --- 1️⃣ Look for existing by invoice_id / payment_intent_id ---
            if invoice_id:
                cur.execute(
                    "SELECT id, status, stripe_checkout_session_id FROM payments WHERE stripe_invoice_id = %s LIMIT 1",
                    (invoice_id,),
                )
                row = cur.fetchone()
                if row:
                    existing_payment_id, old_status, old_checkout_session_id = row

            elif payment_intent_id:
                cur.execute(
                    "SELECT id, status, stripe_checkout_session_id FROM payments WHERE stripe_payment_intent_id = %s LIMIT 1",
                    (payment_intent_id,),
                )
                row = cur.fetchone()
                if row:
                    existing_payment_id, old_status, old_checkout_session_id = row

            # --- 2️⃣ Merge by subscription + amount + 10s window if not found ---
            if not existing_payment_id and subscription_id:
                cur.execute(
                    """
                    SELECT id, status, stripe_checkout_session_id, created_at
                    FROM payments
                    WHERE stripe_subscription_id = %s
                    AND amount_cents = %s
                    AND ABS(TIMESTAMPDIFF(SECOND, created_at, NOW())) <= 60
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (subscription_id, amount_cents),
                )
                row = cur.fetchone()
                if row:
                    existing_payment_id, old_status, old_checkout_session_id = (
                        row[0],
                        row[1],
                        row[2],
                    )
                    # print(f"🔀 Merging payment events into id={existing_payment_id}")

            # --- 3️⃣ Determine final status ---
            new_status = data.get("status", "pending")
            if old_status and STATUS_PRIORITY.get(old_status, 0) > STATUS_PRIORITY.get(
                new_status, 0
            ):
                # Do not downgrade
                new_status = old_status

            # --- 4️⃣ Update or Insert ---
            if existing_payment_id:
                # If old is succeeded and new is pending (late session), only update session ID
                if (
                    old_status == "succeeded"
                    and new_status == "succeeded"
                    and data.get("checkout_session_id")
                ):
                    cur.execute(
                        """
                        UPDATE payments
                        SET stripe_checkout_session_id=COALESCE(%s,stripe_checkout_session_id)
                        WHERE id=%s
                        """,
                        (data.get("checkout_session_id"), existing_payment_id),
                    )
                    # print(
                    #     f"💾 Updated checkout_session_id only for succeeded payment id={existing_payment_id}"
                    # )

                else:
                    # Normal merge/update
                    cur.execute(
                        """
                        UPDATE payments
                        SET
                            user_id=%s,
                            stripe_event_id=COALESCE(%s,stripe_event_id),
                            stripe_payment_intent_id=COALESCE(%s,stripe_payment_intent_id),
                            stripe_checkout_session_id=COALESCE(%s,stripe_checkout_session_id),
                            stripe_invoice_id=COALESCE(%s,stripe_invoice_id),
                            stripe_subscription_id=COALESCE(%s,stripe_subscription_id),
                            amount_cents=%s,
                            currency=%s,
                            payment_type=%s,
                            status=%s,
                            invoice_url=COALESCE(%s,invoice_url)
                        WHERE id=%s
                        """,
                        (
                            user_id,
                            data.get("stripe_event_id"),
                            payment_intent_id,
                            data.get("checkout_session_id"),
                            invoice_id,
                            subscription_id,
                            amount_cents,
                            data.get("currency"),
                            data.get("payment_type"),
                            new_status,
                            data.get("invoice_url"),
                            existing_payment_id,
                        ),
                    )
                    # print(f"💾 Payment updated (merged) id={existing_payment_id}")

            else:
                # Insert new payment
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
                        invoice_url,
                        created_at
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    """,
                    (
                        user_id,
                        data.get("stripe_event_id"),
                        payment_intent_id,
                        data.get("checkout_session_id"),
                        invoice_id,
                        subscription_id,
                        amount_cents,
                        data.get("currency"),
                        data.get("payment_type"),
                        new_status,
                        data.get("invoice_url"),
                    ),
                )
                # print(f"💾 Payment inserted (new) id={cur.lastrowid}")

            conn.commit()

        except Exception as e:
            logger.error("Error on save payment: %s", e, exc_info=IS_DEV)

        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()

    # -------------------------------------------------
    # SUBSCRIPTIONS
    # -------------------------------------------------
    def save_subscription(self, data):
        if not data.get("user_id") or not data.get("subscription_id"):
            # print("❌ Subscription skipped (missing data)")
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
                    stripe_price_id=new.stripe_price_id,
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
            # print("📦 Subscription saved")

        finally:
            cur.close()
            conn.close()

    def _save_subscription_from_stripe(self, sub):
        customer = stripe.Customer.retrieve(sub["customer"])
        user_id = customer.metadata.get("user_id") or get_userid(customer.email)

        price_id = sub["items"]["data"][0]["price"]["id"]
        # print("sub details", sub)

        self.save_subscription(
            {
                "user_id": user_id,
                "subscription_id": sub["id"],
                "customer_id": sub["customer"],
                "price_id": price_id,
                "status": sub["status"],
                "period_start": sub.get("current_period_start"),
                "period_end": sub.get("current_period_end"),
            }
        )

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
            # print(f"🔄 Subscription status updated → {db_status}")
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
        status = "pending"

        if is_one_time:
            # For one-time payments, check final status
            payment_status = session.get("payment_status")
            pi_id = session.get("payment_intent")

            if payment_status == "paid" and pi_id:
                status = "succeeded"
                pi = stripe.PaymentIntent.retrieve(pi_id)
                charge_id = pi.get("latest_charge")
                if charge_id:
                    charge = stripe.Charge.retrieve(charge_id)
                    receipt_url = charge.get("receipt_url")
            else:
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
                "subscription_id": session.get("subscription"),
                "invoice_id": None,
                "invoice_url": receipt_url,
            }
        )

        # -------------------------
        # CREDIT ALLOCATION
        # -------------------------
        if status == "succeeded":
            try:
                # 1️⃣ Primary source: metadata
                tokens = metadata.get("credits") or metadata.get("amount_cents")

                # 2️⃣ Fallback ONLY if metadata missing
                if not tokens:
                    price_id = None
                    if session.get("line_items"):
                        price_id = session["line_items"][0]["price"]["id"]

                    tokens = self.get_token_by_planidorpriceid(
                        plancode=metadata.get("plan_code"),
                        price_id=price_id,
                    )

                if not tokens or int(tokens) <= 0:
                    raise ValueError("Invalid credit amount")

                tokens = int(tokens)

                conn = connect_to_rds()
                credit_manager = CreditManager(db_conn=conn)

                source_type = metadata.get("type", "ONETIME").upper()

                credit_manager.add_credits(
                    user_id=user_id,
                    credits=tokens,
                    source_type=source_type,
                    expires_at=datetime.utcnow() + timedelta(days=60),
                    source_ref=session.get("id"),  # 👈 idempotency key
                )

                conn.close()
                #print(f"💰 Added {tokens} credits to user {user_id}")

            except Exception as e:
                logger.error("Failed to add credits: %s", e, exc_info=IS_DEV)

    def checkout_failed(self):
        logger.error("Checkout failed: %s", self.obj.get("id"))

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

        # Get customer info
        customer_id = invoice.get("customer")
        customer = stripe.Customer.retrieve(customer_id)
        user_id = customer.metadata.get("user_id") or get_userid(customer.email)

        # Resolve subscription_id safely
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
            subscription_id = (
                invoice.get("parent", {})
                .get("subscription_details", {})
                .get("subscription")
            )

        # Save the payment first
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
                "checkout_session_id": None,
            }
        )

        # -------------------------
        # CREDIT ALLOCATION (SUBSCRIPTION)
        # -------------------------
        if subscription_id:
            # Resolve user_id fallback: check payments table (populated by checkout_completed)
            if not user_id:
                conn_tmp = connect_to_rds()
                cur_tmp = conn_tmp.cursor()
                cur_tmp.execute(
                    "SELECT user_id FROM payments WHERE stripe_subscription_id=%s AND user_id IS NOT NULL LIMIT 1",
                    (subscription_id,),
                )
                row_tmp = cur_tmp.fetchone()
                if row_tmp:
                    user_id = row_tmp[0]
                cur_tmp.close()
                conn_tmp.close()

            if not user_id:
                logger.error("invoice_paid: cannot resolve user_id for subscription %s", subscription_id)
                return

            # Get price_id directly from invoice lines (avoids race condition with subscriptions table)
            line_item = invoice.get("lines", {}).get("data", [{}])[0]
            price_id = line_item.get("price", {}).get("id")

            tokens = 0
            if price_id:
                tokens = StripeWebhookHandler.get_token_by_planidorpriceid(price_id=price_id)

            # Fallback: try subscriptions table (may be populated by now)
            if not tokens:
                tokens = self._get_subscription_tokens(subscription_id=subscription_id)

            logger.info("invoice_paid: user=%s sub=%s tokens=%s", user_id, subscription_id, tokens)

            if tokens > 0:
                conn = connect_to_rds()
                credit_manager = CreditManager(db_conn=conn)

                credit_manager.add_credits(
                    user_id=user_id,
                    credits=tokens,
                    source_type="SUBSCRIPTION",
                    expires_at=datetime.utcnow() + timedelta(days=45),
                    source_ref=invoice.get("id"),  # use invoice id for idempotency
                )

                conn.close()
            else:
                logger.warning("invoice_paid: no tokens found for sub=%s price=%s", subscription_id, price_id)

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
            #print("⚠️ invoice_failed without subscription:", invoice.get("id"))
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

        #print("❌ Subscription payment failed:", subscription_id)

    # -------------------------------------------------
    # SUBSCRIPTION LIFECYCLE
    # -------------------------------------------------
    def subscription_created(self):
        sub = self.obj
        logger.info("Subscription created: %s", sub.get("id"))

        self._save_subscription_from_stripe(sub)

        logger.info("Subscription created & saved")

    def subscription_updated(self):
        sub = self.obj

        self._save_subscription_from_stripe(sub)

        logger.info("Subscription updated & saved")

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
            logger.info("Subscription canceled: %s", sub.get("id"))
        finally:
            cur.close()
            conn.close()

    # -------------------------------------------------
    # FALLBACK
    # -------------------------------------------------
    def unhandled_event(self):
        logger.warning("Unhandled Stripe event: %s", self.event_type)
