import multiprocessing

bind = "0.0.0.0:3000"
workers = multiprocessing.cpu_count() * 2 + 1
worker_class = "gthread"
threads = 4
timeout = 120
keepalive = 5


def on_starting(server):
    """Self-heal the uploads bucket CORS once, in the master, before workers fork.

    The browser SPA PUTs presigned uploads straight to S3; without bucket CORS
    the preflight is rejected and uploads fail with "Failed to fetch". Doing it
    here (not at module import) means one idempotent call per deploy instead of
    one per worker. Never raises — failures are logged and boot continues.
    """
    try:
        from utils.s3_utils import ensure_bucket_cors

        ensure_bucket_cors()
    except Exception as e:  # pragma: no cover - boot must not depend on this
        server.log.warning("on_starting: ensure_bucket_cors failed: %s", e)
