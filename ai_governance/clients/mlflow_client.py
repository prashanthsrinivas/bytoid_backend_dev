"""MLflow experiment tracking client.

Required env vars (both optional — sensible defaults provided):
    MLFLOW_TRACKING_URI     — e.g. http://mlflow.internal:5000
    MLFLOW_EXPERIMENT_NAME  — defaults to "ai_governance"

Call init_mlflow() once before using any mlflow.* APIs.  Subsequent calls
are no-ops thanks to the _initialized guard.
"""

import os
import threading

_lock = threading.Lock()
_initialized = False


def init_mlflow() -> None:
    """Configure MLflow tracking URI and active experiment (idempotent)."""
    global _initialized
    if _initialized:
        return
    with _lock:
        if not _initialized:
            import mlflow

            mlflow.set_tracking_uri(
                os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
            )
            mlflow.set_experiment(
                os.getenv("MLFLOW_EXPERIMENT_NAME", "ai_governance")
            )
            _initialized = True


def get_mlflow():
    """Return the mlflow module with tracking configured."""
    init_mlflow()
    import mlflow

    return mlflow
