import os
import time
import signal
import logging
import threading
from typing import Optional
import boto3
from controller import AegisController

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [AEGIS-SUPERVISOR] %(message)s"
)
logger = logging.getLogger("aegis.supervisor")


class AegisSupervisor:
    """
    AEGIS Continuous Background Supervisor.
    Orchestrates periodic execution of AegisController.run() with graceful shutdown,
    robust exception handling, and non-overlapping cycles.
    """

    def __init__(
        self,
        controller: Optional[AegisController] = None,
        poll_interval: Optional[int] = None,
    ):
        self.controller = controller or AegisController()
        env_interval = os.getenv("AEGIS_POLL_INTERVAL", "60")
        try:
            self.poll_interval = poll_interval if poll_interval is not None else int(env_interval)
        except ValueError:
            self.poll_interval = 60

        self.running = False
        self._stop_event = threading.Event()
        self._cycle_lock = threading.Lock()

        try:
            self._cloudwatch = boto3.client(
                "cloudwatch",
                region_name=os.getenv("AWS_REGION", "ap-south-1"),
            )
        except Exception as e:
            logger.warning(f"CloudWatch client unavailable, heartbeat disabled: {e}")
            self._cloudwatch = None

    def _emit_heartbeat(self):
        """Publish a heartbeat so AegisSupervisorHeartbeatAlarm can detect a
        stalled detection loop (missing data is treated as a breach)."""
        if self._cloudwatch is None:
            return
        try:
            self._cloudwatch.put_metric_data(
                Namespace="AEGIS/Supervisor",
                MetricData=[
                    {
                        "MetricName": "HeartbeatCount",
                        "Value": 1,
                        "Unit": "Count",
                    }
                ],
            )
        except Exception as e:
            logger.warning(f"Failed to emit heartbeat metric: {e}")

    def handle_signal(self, signum, frame):
        """Handle termination signals gracefully."""
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        logger.info(f"Received signal {sig_name}. Initiating graceful shutdown...")
        self.stop()

    def run_cycle(self):
        """Execute a single controller cycle safely."""
        with self._cycle_lock:
            logger.info("AEGIS cycle started")
            try:
                result = self.controller.run()
                logger.info(f"AEGIS cycle completed: anomaly={result.get('anomaly', False)}")
                return result
            except Exception as e:
                logger.error(f"AEGIS cycle error: {e}", exc_info=True)
                return {"anomaly": False, "error": str(e)}
            finally:
                # Emitted even on failure -- a crashing cycle still proves
                # the loop is alive; a stalled/dead thread is what the
                # watchdog alarm needs to catch.
                self._emit_heartbeat()

    def start(self):
        """Start the continuous background supervisor polling loop."""
        self.running = True
        self._stop_event.clear()

        # Register signal handlers if in main thread
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, self.handle_signal)
                signal.signal(signal.SIGTERM, self.handle_signal)
            except (ValueError, AttributeError):
                # Signals might not be registerable in certain environments
                pass

        logger.info(f"AEGIS supervisor started (polling interval: {self.poll_interval}s)")

        try:
            while self.running and not self._stop_event.is_set():
                self.run_cycle()

                # Sleep in increments or wait on stop_event for prompt termination
                if self.running and not self._stop_event.is_set():
                    logger.info(f"AEGIS sleeping for {self.poll_interval} seconds")
                    self._stop_event.wait(timeout=self.poll_interval)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received.")
        finally:
            self.running = False
            logger.info("AEGIS supervisor stopped cleanly")

    def stop(self):
        """Signal the supervisor to stop cleanly."""
        self.running = False
        self._stop_event.set()


if __name__ == "__main__":
    supervisor = AegisSupervisor()
    supervisor.start()
