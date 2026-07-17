from unittest.mock import MagicMock, patch

import run


def test_invalid_interval_uses_safe_default() -> None:
    assert run._interval_minutes("invalid") == 60
    assert run._interval_minutes("0") == 1
    assert run._interval_minutes("15") == 15


def test_failure_backoff_is_bounded() -> None:
    assert run._failure_backoff_seconds("invalid") == 300
    assert run._failure_backoff_seconds("1") == 30
    assert run._failure_backoff_seconds("999999") == 3600


def test_runner_exits_when_web_process_dies() -> None:
    web = MagicMock()
    web.poll.side_effect = [None, None, 17, 17]
    web.returncode = 17
    worker = MagicMock()
    worker.poll.return_value = None

    with (
        patch.object(run.Path, "mkdir"),
        patch.object(run, "heartbeat"),
        patch.object(run.subprocess, "Popen", side_effect=[web, worker]),
        patch.object(run.time, "sleep"),
        patch.object(run.signal, "signal"),
    ):
        result = run.main()

    assert result == 17
    worker.terminate.assert_called_once()
