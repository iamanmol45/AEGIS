"""
AEGIS load generator.

Generates real HTTP traffic against the live ALB -- unlike chaos.py's
scenarios (which inject a fake metric value directly), this exercises the
actual request path end-to-end, so CPU/latency/error-rate anomalies show
up as real CloudWatch metrics and can trigger the full autonomous
detection/RCA/recovery loop, not just a simulated one.

Two traffic shapes, matching the architecture doc's requirement for
realistic (not "one-size-fits-all") workload patterns:
  - SteadyUser: constant, low-rate traffic -- typical daily baseline.
  - BurstyUser: fast, tightly-spaced requests -- peak/spike conditions.

Usage:
    pip install -r requirements.txt
    locust -f locustfile.py --host http://<alb-dns-name>

Then open http://localhost:8089 and pick a user count/spawn rate, or run
headless with a scripted shape:
    locust -f locustfile.py --host http://<alb-dns-name> --headless \
        --users 50 --spawn-rate 5 --run-time 5m
"""

from locust import HttpUser, task, between, LoadTestShape


class SteadyUser(HttpUser):
    """Typical daily traffic pattern: a handful of requests, spaced out."""
    weight = 3
    wait_time = between(1, 3)

    @task(3)
    def health(self):
        self.client.get("/health")

    @task(2)
    def scan(self):
        self.client.get("/scan")

    @task(1)
    def status(self):
        self.client.get("/status")


class BurstyUser(HttpUser):
    """Peak-load traffic pattern: minimal wait between requests, meant to
    actually push CPU/latency high enough to trip real CloudWatch alarms
    when run with enough concurrent users."""
    weight = 1
    wait_time = between(0.05, 0.3)

    @task(4)
    def health(self):
        self.client.get("/health")

    @task(3)
    def scan(self):
        self.client.get("/scan")

    @task(1)
    def policy(self):
        self.client.get("/policy")


class RampingBurstShape(LoadTestShape):
    """
    Optional scripted shape (used only if you run Locust with
    `--class-picker` or set LoadTestShape as the active shape): steady
    baseline, then a sharp ramp to a burst, then back down. Demonstrates
    the "daily activity cycle then a spike" pattern the architecture doc
    calls for, in a single scripted run instead of manual user-count
    changes in the web UI.

    Stages: (duration_seconds, target_user_count, spawn_rate)
    """
    stages = [
        {"duration": 60, "users": 5, "spawn_rate": 1},
        {"duration": 120, "users": 5, "spawn_rate": 1},
        {"duration": 150, "users": 60, "spawn_rate": 10},
        {"duration": 240, "users": 60, "spawn_rate": 10},
        {"duration": 270, "users": 5, "spawn_rate": 5},
    ]

    def tick(self):
        run_time = self.get_run_time()

        for stage in self.stages:
            if run_time < stage["duration"]:
                return (stage["users"], stage["spawn_rate"])

        return None
