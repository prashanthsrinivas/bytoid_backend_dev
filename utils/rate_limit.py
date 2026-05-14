import os
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from utils.app_configs import IS_DEV


def _get_rate_limit_key():
    """Per-user key for authenticated requests, per-IP for anonymous ones."""
    from flask import g
    return str(getattr(g, "user_id", None) or get_remote_address())


limiter = Limiter(
    key_func=_get_rate_limit_key,
    default_limits=[],
    headers_enabled=True,
)


def init_limiter(app):
    host = os.getenv("REDIS_HOST_DEV")
    if host:
        app.config["RATELIMIT_STORAGE_URI"] = f"rediss://{host}:6379"
        if IS_DEV:
            app.config["RATELIMIT_STORAGE_OPTIONS"] = {"ssl_cert_reqs": None}
        else:
            app.config["RATELIMIT_STORAGE_OPTIONS"] = {
                "ssl_ca_certs": "/home/ec2-user/bytoid_python/awsredis.pem",
                "ssl_cert_reqs": "required",
            }
    else:
        app.config["RATELIMIT_STORAGE_URI"] = "memory://"
    limiter.init_app(app)
