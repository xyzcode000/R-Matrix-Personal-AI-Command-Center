import hashlib
import sqlite3
from datetime import timedelta

import pytest

from app import control_plane


PASSWORD = "correct horse battery staple"


def sign_in(client):
    control_plane.create_owner("owner", PASSWORD)
    csrf = client.get("/api/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": csrf},
        json={"username": "owner", "password": PASSWORD},
    )
    assert response.status_code == 200
    return client.cookies["r_matrix_csrf"]


def test_remote_access_requires_a_session_and_csrf(anonymous_client):
    client = anonymous_client
    assert client.get("/api/control/devices").status_code == 401
    control_plane.create_owner("owner", PASSWORD)
    csrf = client.get("/api/auth/csrf").json()["csrf_token"]
    wrong_password = client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": csrf},
        json={"username": "owner", "password": "not-the-password"},
    )
    assert wrong_password.status_code == 401
    assert PASSWORD not in wrong_password.text
    assert "not-the-password" not in wrong_password.text
    denied = client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": "wrong-token"},
        json={"username": "owner", "password": PASSWORD},
    )
    assert denied.status_code == 403
    assert client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": csrf},
        json={"username": "owner", "password": PASSWORD},
    ).status_code == 200
    session_cookie = client.cookies.get("r_matrix_session")
    assert session_cookie
    assert session_cookie not in client.get("/api/auth/me").text
    assert client.get("/api/auth/me").json() == {"username": "owner"}
    csrf = client.cookies["r_matrix_csrf"]
    assert client.post(
        "/api/control/enrollment-codes",
        headers={"X-CSRF-Token": "stale"},
    ).status_code == 403
    assert client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": csrf},
    ).status_code == 200
    assert client.get("/api/control/devices").status_code == 401


def test_private_app_apis_require_owner_session(anonymous_client):
    client = anonymous_client
    assert client.get("/api/health").status_code == 200
    for path in (
        "/api/conversations",
        "/api/documents",
        "/api/memories",
        "/api/memory-proposals",
        "/api/tasks",
        "/api/agents",
        "/api/agent-executions",
        "/api/action-approvals",
        "/api/projects",
        "/api/providers",
        "/api/settings/profile",
        "/api/export",
        "/api/control/devices",
    ):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/chat", json={"message": "hello"}).status_code == 401
    assert client.post("/api/documents/search", json={"query": "private"}).status_code == 401
    client.cookies.set(control_plane.SESSION_COOKIE, "invalid.jwt.token")
    assert client.get("/api/conversations").status_code == 401
    assert client.get("/api/control/devices").status_code == 401


def test_api_auth_can_be_disabled_only_explicitly(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "require_api_auth", False)
    assert client.get("/api/health").json()["auth_required"] is False
    assert client.get("/api/conversations").status_code == 200


def test_jwt_validation_expiration_and_logout(client):
    login = client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": client.cookies["r_matrix_csrf"]},
        json={"username": "owner", "password": PASSWORD},
    )
    assert login.status_code == 200
    token = client.cookies[control_plane.SESSION_COOKIE]
    parts = token.split(".")
    assert len(parts) == 3
    claims = control_plane._decode_access_token(token)
    assert claims is not None
    assert claims["sub"]
    assert claims["exp"] > claims["iat"]
    cookie_header = login.headers["set-cookie"]
    assert "HttpOnly" in cookie_header
    assert "SameSite=strict" in cookie_header

    client.cookies.set(control_plane.SESSION_COOKIE, f"{parts[0]}.{parts[1]}.invalid")
    assert client.get("/api/conversations").status_code == 401
    assert client.get("/api/control/devices").status_code == 401

    with sqlite3.connect(control_plane.settings.control_database_path) as connection:
        user_id = connection.execute("SELECT id FROM owner_accounts").fetchone()[0]
    now = control_plane._now()
    expired = control_plane._create_access_token(
        user_id,
        now - timedelta(hours=2),
        now - timedelta(hours=1),
    )
    client.cookies.set(control_plane.SESSION_COOKIE, expired, path="/api")
    assert client.get("/api/conversations").status_code == 401
    assert client.get("/api/control/devices").status_code == 401

    # A valid session reaches the workspace, and logout invalidates it server-side.
    client.cookies.clear()
    csrf = client.get("/api/auth/csrf").json()["csrf_token"]
    assert client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": csrf},
        json={"username": "owner", "password": PASSWORD},
    ).status_code == 200
    assert client.get("/api/conversations").status_code == 200
    assert client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": client.cookies["r_matrix_csrf"]}
    ).status_code == 200
    assert client.get("/api/conversations").status_code == 401


def test_mutations_require_csrf_and_owner_is_single_account(anonymous_client):
    client = anonymous_client
    assert control_plane.create_owner("owner", PASSWORD)
    assert not control_plane.create_owner("second-owner", PASSWORD)
    csrf = client.get("/api/auth/csrf").json()["csrf_token"]
    login = client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": csrf},
        json={"username": "owner", "password": PASSWORD},
    )
    assert login.status_code == 200
    assert client.post("/api/tasks", json={"title": "Private task"}).status_code == 403
    assert client.post(
        "/api/tasks",
        headers={"X-CSRF-Token": client.cookies["r_matrix_csrf"]},
        json={"title": "Private task"},
    ).status_code == 201
    second_owner_login = client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": client.cookies["r_matrix_csrf"]},
        json={"username": "second-owner", "password": PASSWORD},
    )
    assert second_owner_login.status_code == 401


def test_existing_non_owner_session_cannot_access_single_owner_data(anonymous_client):
    client = anonymous_client
    assert control_plane.create_owner("owner", PASSWORD)
    now = control_plane._now()
    expires_at = now + timedelta(hours=1)
    token = control_plane._create_access_token("legacy-second-user", now, expires_at)
    with sqlite3.connect(control_plane.settings.control_database_path) as connection:
        connection.execute(
            """INSERT INTO owner_accounts
               (id, username, password_salt, password_hash, password_iterations, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "legacy-second-user",
                "legacy-user",
                "00" * 16,
                "00" * 32,
                control_plane.PASSWORD_ITERATIONS,
                "2999-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """INSERT INTO auth_sessions
               (token_hash, user_id, csrf_hash, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                control_plane._digest(token),
                "legacy-second-user",
                control_plane._digest("csrf"),
                expires_at.isoformat(),
                now.isoformat(),
            ),
        )
    client.cookies.set(control_plane.SESSION_COOKIE, token, path="/api")
    assert client.get("/api/conversations").status_code == 401
    assert client.get("/api/control/devices").status_code == 401


def test_login_requires_configured_jwt_secret(anonymous_client, monkeypatch):
    from app.config import settings

    control_plane.create_owner("owner", PASSWORD)
    csrf = anonymous_client.get("/api/auth/csrf").json()["csrf_token"]
    monkeypatch.setattr(settings, "auth_jwt_secret", "")
    response = anonymous_client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": csrf},
        json={"username": "owner", "password": PASSWORD},
    )
    assert response.status_code == 503
    assert "AUTH_JWT_SECRET" in response.json()["detail"]


def test_legacy_password_hash_is_upgraded_after_successful_login(client):
    salt = b"legacy-test-salt"
    legacy_hash = hashlib.pbkdf2_hmac(
        "sha256",
        PASSWORD.encode("utf-8"),
        salt,
        control_plane.LEGACY_PASSWORD_ITERATIONS,
    ).hex()
    with sqlite3.connect(control_plane.settings.control_database_path) as connection:
        connection.execute(
            "UPDATE owner_accounts SET password_salt = ?, password_hash = ?, password_iterations = ?",
            (
                salt.hex(),
                legacy_hash,
                control_plane.LEGACY_PASSWORD_ITERATIONS,
            ),
        )
    response = client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": client.cookies["r_matrix_csrf"]},
        json={"username": "owner", "password": PASSWORD},
    )
    assert response.status_code == 200
    with sqlite3.connect(control_plane.settings.control_database_path) as connection:
        iterations = connection.execute(
            "SELECT password_iterations FROM owner_accounts"
        ).fetchone()[0]
    assert iterations == control_plane.PASSWORD_ITERATIONS


def test_existing_control_database_migrates_without_removing_owner(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    with sqlite3.connect(settings.control_database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations (version, applied_at) VALUES (1, '2025-01-01T00:00:00+00:00');
            CREATE TABLE owner_accounts (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO owner_accounts
                (id, username, password_salt, password_hash, created_at)
                VALUES ('existing-owner', 'owner', 'salt', 'hash', '2025-01-01T00:00:00+00:00');
            """
        )
    control_plane.initialize()
    with sqlite3.connect(settings.control_database_path) as connection:
        owner = connection.execute(
            "SELECT id, password_iterations FROM owner_accounts"
        ).fetchone()
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
    assert owner == ("existing-owner", control_plane.LEGACY_PASSWORD_ITERATIONS)
    assert version == 3
    with sqlite3.connect(settings.control_database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "auth_challenges" in tables


def test_single_use_enrollment_and_device_credential_redaction(client):
    csrf = sign_in(client)
    issued = client.post(
        "/api/control/enrollment-codes",
        headers={"X-CSRF-Token": csrf},
    )
    assert issued.status_code == 201
    code = issued.json()["code"]
    enrolled = client.post(
        "/api/control/enroll",
        json={"code": code, "name": "Office PC"},
    )
    assert enrolled.status_code == 201
    device = enrolled.json()
    assert device["device_token"] not in client.get("/api/control/devices").text
    assert client.post(
        "/api/control/enroll",
        json={"code": code, "name": "Replay"},
    ).status_code == 400
    with sqlite3.connect(control_plane.settings.control_database_path) as connection:
        rows = connection.execute(
            "SELECT credential_hash FROM devices WHERE id = ?", (device["device_id"],)
        ).fetchone()
        audit = connection.execute("SELECT event FROM control_audit").fetchall()
    assert rows[0] != device["device_token"]
    assert code not in str(audit)
    assert device["device_token"] not in str(audit)


def test_allowlisted_job_round_trip_and_revocation(client):
    csrf = sign_in(client)
    code = client.post(
        "/api/control/enrollment-codes",
        headers={"X-CSRF-Token": csrf},
    ).json()["code"]
    device = client.post("/api/control/enroll", json={"code": code, "name": "Agent"}).json()
    created = client.post(
        "/api/control/jobs",
        headers={"X-CSRF-Token": csrf},
        json={"device_id": device["device_id"], "operation": "agent.status"},
    )
    assert created.status_code == 201
    assert created.json()["status"] == "queued"
    assert client.post(
        "/api/control/jobs",
        headers={"X-CSRF-Token": csrf},
        json={"device_id": device["device_id"], "operation": "shell.exec"},
    ).status_code == 422

    headers = {"Authorization": f"Bearer {device['device_token']}"}
    job = client.get("/api/control/agent/jobs/next", headers=headers).json()["job"]
    assert job["operation"] == "agent.status"
    invalid_result = client.post(
        f"/api/control/agent/jobs/{job['id']}/result",
        headers=headers,
        json={"status": "succeeded", "result": {"status": "ok", "secret": "must-not-be-accepted"}},
    )
    assert invalid_result.status_code == 422
    completed = client.post(
        f"/api/control/agent/jobs/{job['id']}/result",
        headers=headers,
        json={
            "status": "succeeded",
            "result": {
                "status": "ok",
                "platform": "Windows",
                "python_version": "3.14",
                "agent_version": "0.1.0",
            },
        },
    )
    assert completed.status_code == 200
    assert client.get("/api/control/jobs").json()[0]["status"] == "succeeded"
    assert client.post(
        f"/api/control/devices/{device['device_id']}/revoke",
        headers={"X-CSRF-Token": csrf},
    ).status_code == 200
    assert client.get("/api/control/agent/jobs/next", headers=headers).status_code == 401


def test_control_plane_only_mode_hides_local_chat_and_document_api(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "control_plane_only", True)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/csrf").status_code == 200
    assert client.get("/api/conversations").status_code == 404
    assert client.post("/api/chat", json={"message": "Search the web for current news"}).status_code == 404
    assert client.post("/api/documents/search", json={"query": "private"}).status_code == 404


def test_bootstrap_password_policy_and_duplicate_owner(anonymous_client):
    client = anonymous_client
    assert control_plane.create_owner("owner", PASSWORD)
    assert not control_plane.create_owner("OWNER", PASSWORD)
    assert not control_plane.create_owner("another-owner", PASSWORD)
    with pytest.raises(ValueError, match="at least 12"):
        control_plane.create_owner("second", "short")
    assert client.get("/api/health").status_code == 200


def test_expired_enrollment_codes_and_jobs_cannot_be_consumed(client):
    csrf = sign_in(client)
    code = client.post(
        "/api/control/enrollment-codes",
        headers={"X-CSRF-Token": csrf},
    ).json()["code"]
    with sqlite3.connect(control_plane.settings.control_database_path) as connection:
        connection.execute(
            "UPDATE enrollment_codes SET expires_at = '2000-01-01T00:00:00+00:00' WHERE code_hash = ?",
            (control_plane._digest(code),),
        )
    assert client.post(
        "/api/control/enroll",
        json={"code": code, "name": "Expired"},
    ).status_code == 400

    fresh_code = client.post(
        "/api/control/enrollment-codes",
        headers={"X-CSRF-Token": csrf},
    ).json()["code"]
    device = client.post(
        "/api/control/enroll",
        json={"code": fresh_code, "name": "Agent"},
    ).json()
    job = client.post(
        "/api/control/jobs",
        headers={"X-CSRF-Token": csrf},
        json={"device_id": device["device_id"], "operation": "health.check"},
    ).json()
    with sqlite3.connect(control_plane.settings.control_database_path) as connection:
        connection.execute(
            "UPDATE control_jobs SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job["id"],),
        )
    poll = client.get(
        "/api/control/agent/jobs/next",
        headers={"Authorization": f"Bearer {device['device_token']}"},
    )
    assert poll.json()["job"] is None
    assert client.get("/api/control/jobs").json()[0]["status"] == "expired"
