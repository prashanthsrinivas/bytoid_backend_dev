import os
import stripe

# -------------------------------------------------
# STRIPE ENV CONFIG
# -------------------------------------------------
STRIPE_SECRET_KEY = os.getenv(
    "STRIPE_SECRET_KEY",
    "rk_test_51SnhSV6AY73BJLxH1O57fpiXhOCSar0QrRs9WcD9asarReTpP344d4cKa6EmtocUsL6zwWnTbg1B8ta5lz2NBWL600ZVYCUSMl",
)

STRIPE_WEBHOOK_SECRET = os.getenv(
    "STRIPE_WEBHOOK_SECRET", "whsec_yH8SDet1nSgbAGeP2AsTuSGnBQxABNiO"
)
STRIPE_SUCCESS_URL = os.getenv(
    "STRIPE_SUCCESS_URL",
    "https://dev.bytoid.ai/billing/success",
)

STRIPE_CANCEL_URL = os.getenv(
    "STRIPE_CANCEL_URL",
    "https://dev.bytoid.ai/billing/cancelled",
)

# -------------------------------------------------
# INITIALIZE STRIPE
# -------------------------------------------------
stripe.api_key = STRIPE_SECRET_KEY

dev_stipe = stripe


# -------------------------------------------------
# COMMON HELPERS
# -------------------------------------------------
def verify_webhook(payload, sig_header):
    """
    Verify Stripe webhook signature and return event
    """
    return stripe.Webhook.construct_event(
        payload=payload,
        sig_header=sig_header,
        secret=STRIPE_WEBHOOK_SECRET,
    )


# def create_checkout_session(
#     *,
#     mode,
#     line_items,
#     email,
#     metadata=None,
# ):
#     return stripe.checkout.Session.create(
#         mode=mode,
#         line_items=line_items,
#         customer_email=email,
#         metadata=metadata or {},
#         success_url=STRIPE_SUCCESS_URL,
#         cancel_url=STRIPE_CANCEL_URL,
#     )


def create_checkout_session(
    *,
    mode,
    line_items,
    email,
    metadata=None,
):
    return stripe.checkout.Session.create(
        mode=mode,
        line_items=line_items,
        customer_email=email,
        metadata=metadata or {},
        success_url=STRIPE_SUCCESS_URL,
        cancel_url=STRIPE_CANCEL_URL,
        # ✅ Automatic tax
        automatic_tax={"enabled": True},
        # ✅ Collect billing address
        billing_address_collection="required",
    )


# =====================================================
# STRIPE HELPERS
# =====================================================


def create_stripe_product_and_price(plan, is_subscription=True, is_topup=False):
    """
    Create a Stripe product and price.
    - Subscriptions: recurring price (interval must be month/year/week/day)
    - Top-ups: one-time price
    """
    print("Creating a new plan in Stripe...")

    # Create product
    product = stripe.Product.create(
        name=plan["name"],
        description=plan.get("description", ""),
        metadata={"plan_code": plan["plan_code"]},
    )

    # Create price
    if is_subscription:
        # Ensure billing_interval is valid for Stripe recurring
        if plan["billing_interval"] not in ("day", "week", "month", "year"):
            raise ValueError(
                f"Invalid billing_interval for subscription: {plan['billing_interval']}"
            )

        price = stripe.Price.create(
            product=product.id,
            unit_amount=plan["amount_cents"],
            currency=plan["currency"].lower(),
            recurring={"interval": plan["billing_interval"]},
            metadata={"plan_code": plan["plan_code"]},
        )
    elif is_topup:
        # Top-up = one-time payment
        price = stripe.Price.create(
            product=product.id,
            unit_amount=plan["amount_cents"],
            currency=plan["currency"].lower(),
            metadata={"plan_code": plan["plan_code"]},
        )
    else:
        raise ValueError("Plan must be either a subscription or a top-up")

    print("Stripe product created:", product.id)
    print("Stripe price created:", price.id)

    return product.id, price.id


def create_new_price_and_disable_old(
    plan, old_price_id, is_subscription=True, is_topup=False
):
    """
    Creates a new Stripe price and disables the old one.
    Handles subscriptions and top-ups.
    """
    if is_subscription:
        if plan["billing_interval"] not in ("day", "week", "month", "year"):
            raise ValueError(
                f"Invalid billing_interval for subscription: {plan['billing_interval']}"
            )
        new_price = stripe.Price.create(
            product=plan["stripe_product_id"],
            unit_amount=plan["amount_cents"],
            currency=plan["currency"].lower(),
            recurring={"interval": plan["billing_interval"]},
            metadata={"plan_code": plan["plan_code"]},
        )
    elif is_topup:
        # Top-up = one-time, no recurring
        new_price = stripe.Price.create(
            product=plan["stripe_product_id"],
            unit_amount=plan["amount_cents"],
            currency=plan["currency"].lower(),
            metadata={"plan_code": plan["plan_code"]},
        )
    else:
        raise ValueError("Plan must be either a subscription or a top-up")

    # Disable old price
    stripe.Price.modify(old_price_id, active=False)
    return new_price.id
