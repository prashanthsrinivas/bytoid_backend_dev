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
    )


# =====================================================
# STRIPE HELPERS
# =====================================================


def create_stripe_product_and_price(plan):
    print("making a new plan")
    product = stripe.Product.create(
        name=plan["name"],
        description=plan.get("description", ""),
        metadata={"plan_code": plan["plan_code"]},
    )

    price = stripe.Price.create(
        product=product.id,
        unit_amount=plan["amount_cents"],
        currency=plan["currency"].lower(),
        recurring={"interval": plan["billing_interval"]},
        metadata={"plan_code": plan["plan_code"]},
    )
    print("successfully made the product", product.id)
    print("successfully made the priced", price.id)

    return product.id, price.id


def create_new_price_and_disable_old(plan, old_price_id):
    new_price = stripe.Price.create(
        product=plan["stripe_product_id"],
        unit_amount=plan["amount_cents"],
        currency=plan["currency"].lower(),
        recurring={"interval": plan["billing_interval"]},
        metadata={"plan_code": plan["plan_code"]},
    )

    stripe.Price.modify(old_price_id, active=False)
    return new_price.id
