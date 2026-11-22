from datetime import datetime, timedelta, timezone
from uuid import uuid4

from agent.r_matrix_agent import execute_job, validate_server_url


def test_agent_accepts_only_safe_server_origins():
    assert validate_server_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert validate_server_url("https://control.example.com") == "https://control.example.com"
    for url in (
        "http://control.example.com",
        "https://user:password@control.example.com",
        "https://control.example.com/api",
        "https://control.example.com/?token=secret",
    ):
        try:
            validate_server_url(url)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Unsafe control-plane URL was accepted: {url}")


def test_agent_executes_only_explicit_read_only_operations():
    job = {
        "id": str(uuid4()),
        "operation": "agent.status",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }
    state, result = execute_job(job)
    assert state == "succeeded"
    assert set(result) == {"status", "platform", "python_version", "agent_version"}

    job["operation"] = "shell.exec"
    assert execute_job(job) == ("failed", {"status": "unsupported_operation"})


def test_agent_rejects_malformed_and_expired_jobs():
    assert execute_job(None) == ("failed", {"status": "invalid_job"})
    assert execute_job({"id": "bad", "operation": "health.check", "expires_at": "never"}) == (
        "failed",
        {"status": "invalid_job"},
    )
    expired = {
        "id": str(uuid4()),
        "operation": "health.check",
        "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    }
    assert execute_job(expired) == ("failed", {"status": "expired"})
