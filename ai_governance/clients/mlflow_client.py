"""MLflow experiment tracking client (local file backend by default).

Env vars (both optional):
    MLFLOW_TRACKING_URI     — defaults to a local file:// store under
                              ./mlruns so MLflow works without a separate
                              tracking server.  Set this to point at a real
                              MLflow server when you have one.
    MLFLOW_EXPERIMENT_NAME  — defaults to "ai_governance".

Call init_mlflow() once before using any mlflow.* APIs.  Subsequent calls
are no-ops thanks to the _initialized guard.  The experiment is also
created on first access so /mlflow/runs never errors with
"experiment does not exist".
"""

import os
import threading

_lock = threading.Lock()
_initialized = False


def _default_tracking_uri() -> str:
    """Return a writable file:// URI under the project root so the OSS UI
    works without a separately-deployed MLflow server."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    mlruns = os.path.join(here, "mlruns")
    os.makedirs(mlruns, exist_ok=True)
    return f"file://{mlruns}"


def get_experiment_name() -> str:
    return os.getenv("MLFLOW_EXPERIMENT_NAME", "ai_governance")


def init_mlflow() -> None:
    """Configure MLflow tracking URI and active experiment (idempotent).

    Creates the experiment if it does not exist so search_runs() never
    errors out for a fresh install."""
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return

        import mlflow

        mlflow.set_tracking_uri(
            os.getenv("MLFLOW_TRACKING_URI", _default_tracking_uri())
        )

        exp_name = get_experiment_name()
        existing = mlflow.get_experiment_by_name(exp_name)
        if existing is None:
            mlflow.create_experiment(exp_name)
        mlflow.set_experiment(exp_name)

        _initialized = True


def get_mlflow():
    """Return the mlflow module with tracking configured."""
    init_mlflow()
    import mlflow

    return mlflow
