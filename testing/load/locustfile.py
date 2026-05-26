"""Locust scenarios for backend_load / backend_stress / backend_performance.

Selected at runtime via the LOCUST_SCENARIO env var:
    steady       → SteadyStateUser (fixed concurrency)
    stress       → RampStressUser  (LoadTestShape ramp-to-failure)
    performance  → PerformanceProbeUser (low concurrency, p95/p99 focus)

Endpoints exercised are all read-only and safe to hammer:
    GET /tests/categories
    GET /tests/summary
    GET /azure/test-results
    GET /  (root, may be 404 — counted as smoke)
"""

import os

from locust import HttpUser, LoadTestShape, between, constant_pacing, task

SCENARIO = (os.getenv("LOCUST_SCENARIO") or "steady").lower()


def _safe_get(client, path, name=None):
    """Mark non-5xx responses as success; treat connection errors as failures."""
    with client.get(path, name=name or path, catch_response=True) as resp:
        if resp.status_code and resp.status_code < 500:
            resp.success()
        else:
            resp.failure(f"status={resp.status_code}")


class SteadyStateUser(HttpUser):
    """Fixed-concurrency load. Used by tasks.tests.run_backend_load."""

    wait_time = between(0.5, 1.5)

    @task(3)
    def get_summary(self):
        _safe_get(self.client, "/tests/summary")

    @task(2)
    def get_categories(self):
        _safe_get(self.client, "/tests/categories")

    @task(1)
    def get_azure_test_results(self):
        _safe_get(self.client, "/azure/test-results")

    @task(1)
    def root(self):
        _safe_get(self.client, "/", name="/ (smoke)")


class RampStressUser(HttpUser):
    """User class used during the ramp-to-failure stress run."""

    wait_time = between(0.1, 0.4)

    @task
    def hammer_summary(self):
        _safe_get(self.client, "/tests/summary")

    @task
    def hammer_categories(self):
        _safe_get(self.client, "/tests/categories")


class PerformanceProbeUser(HttpUser):
    """Low-concurrency timed probe to capture p95/p99 per endpoint."""

    wait_time = constant_pacing(1.0)

    @task
    def probe_summary(self):
        _safe_get(self.client, "/tests/summary")

    @task
    def probe_categories(self):
        _safe_get(self.client, "/tests/categories")

    @task
    def probe_azure_test_results(self):
        _safe_get(self.client, "/azure/test-results")


class StressShape(LoadTestShape):
    """Active only when LOCUST_SCENARIO=stress.

    Ramps users in 5 stages, holding each level briefly. Locust still honours
    the global -u cap, so the shape multiplies a fraction of `total_users`.
    """

    stages = [
        {"duration": 20, "users_frac": 0.1, "spawn_rate": 2},
        {"duration": 40, "users_frac": 0.3, "spawn_rate": 5},
        {"duration": 60, "users_frac": 0.5, "spawn_rate": 10},
        {"duration": 90, "users_frac": 0.8, "spawn_rate": 15},
        {"duration": 120, "users_frac": 1.0, "spawn_rate": 20},
    ]

    def tick(self):
        if SCENARIO != "stress":
            return None
        run_time = self.get_run_time()
        # Use the runner's configured user count as the cap.
        runner = getattr(self, "runner", None)
        cap = getattr(runner, "target_user_count", None) or 100
        for stage in self.stages:
            if run_time < stage["duration"]:
                users = max(1, int(cap * stage["users_frac"]))
                return users, stage["spawn_rate"]
        return None


# Restrict the active user class to the requested scenario by setting
# `abstract = True` on the other two. Locust treats abstract User classes as
# non-instantiable.
def _activate_scenario():
    SteadyStateUser.abstract = SCENARIO != "steady"
    RampStressUser.abstract = SCENARIO != "stress"
    PerformanceProbeUser.abstract = SCENARIO != "performance"


_activate_scenario()
