import pytest
from unittest.mock import MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from supervisor import AegisSupervisor


def test_supervisor_calls_controller_run():
    """11. Supervisor calls AegisController.run()."""
    mock_controller = MagicMock()
    mock_controller.run.return_value = {"anomaly": False, "results": []}

    supervisor = AegisSupervisor(controller=mock_controller, poll_interval=1)
    result = supervisor.run_cycle()

    mock_controller.run.assert_called_once()
    assert result["anomaly"] is False


def test_supervisor_continues_after_controller_exception():
    """12. Supervisor continues after a controller exception without crashing."""
    mock_controller = MagicMock()
    # First call raises an exception, second call succeeds
    mock_controller.run.side_effect = [
        RuntimeError("CloudWatch connection timeout"),
        {"anomaly": False, "results": []}
    ]

    supervisor = AegisSupervisor(controller=mock_controller, poll_interval=1)

    # First cycle handles exception cleanly
    cycle1 = supervisor.run_cycle()
    assert cycle1.get("anomaly") is False
    assert "CloudWatch connection timeout" in cycle1.get("error", "")

    # Second cycle proceeds normally
    cycle2 = supervisor.run_cycle()
    assert cycle2.get("anomaly") is False
    assert mock_controller.run.call_count == 2


def test_supervisor_stop():
    """Verify graceful stop sets running to False."""
    supervisor = AegisSupervisor(poll_interval=1)
    supervisor.running = True
    supervisor.stop()
    assert supervisor.running is False
    assert supervisor._stop_event.is_set()
