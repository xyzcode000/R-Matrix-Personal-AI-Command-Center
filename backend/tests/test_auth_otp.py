"""Two-factor sign-in tests. No Telegram network request is made."""

import pytest

from app import control_plane, database, telegram
from app.config import settings

PASSWORD = "correct horse battery staple"
CHAT_ID = "424242"
SESSION = control_plane.SESSION_COOKIE


def _pair_telegram(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123:TEST")
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "auth_telegram_otp_enabled", True)
    with database.session() as connection:
        database.update_telegram_state(
            connection,
            {
                "paired_chat_id": CHAT_ID,
                "paired_user_id": CHAT_ID,
                "paired_username": "owner_telegram",
                "paired_at": database.utc_now(),
                "owner_username": "owner",
            },
        )


@pytest.fixture
def otp_flow(anonymous_client, monkeypatch):
    control_plane.create_owner("owner", PASSWORD)
    _pair_telegram(monkeypatch)
    sent: list[str] = []

    async def fake_send(connection, chat_id, code, ttl_minutes):
        sent.append(code)
        return True

    monkeypatch.setattr(telegram, "send_login_code", fake_send)
    csrf = anonymous_client.get("/api/auth/csrf").json()["csrf_token"]
    anonymous_client.headers.update({"X-CSRF-Token": csrf})
    return anonymous_client, sent


def _password_login(client):
    return client.post("/api/auth/login", json={"username": "owner", "password": PASSWORD})


def _wrong_code(correct: str) -> str:
    return "000000" if correct != "000000" else "111111"


def test_password_step_alone_never_issues_a_session(otp_flow):
    client, sent = otp_flow
    response = _password_login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["two_factor"] is True
    assert body["challenge_id"]
    assert body["delivery"] == "telegram"
    assert client.cookies.get(SESSION) is None
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/control/devices").status_code == 401
    assert len(sent) == 1
    assert sent[0].isdigit() and len(sent[0]) == 6


def test_wrong_code_is_rejected_then_the_correct_code_completes_sign_in(otp_flow):
    client, sent = otp_flow
    challenge_id = _password_login(client).json()["challenge_id"]

    rejected = client.post(
        "/api/auth/verify-otp",
        json={"challenge_id": challenge_id, "code": _wrong_code(sent[0])},
    )
    assert rejected.status_code == 401
    assert client.cookies.get(SESSION) is None
    assert sent[0] not in rejected.text

    accepted = client.post(
        "/api/auth/verify-otp",
        json={"challenge_id": challenge_id, "code": sent[0]},
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"username": "owner", "two_factor": True}
    client.headers.update({"X-CSRF-Token": client.cookies[control_plane.CSRF_COOKIE]})
    assert client.get("/api/auth/me").json()["username"] == "owner"


def test_a_code_cannot_be_reused(otp_flow):
    client, sent = otp_flow
    challenge_id = _password_login(client).json()["challenge_id"]
    assert client.post(
        "/api/auth/verify-otp",
        json={"challenge_id": challenge_id, "code": sent[0]},
    ).status_code == 200
    client.headers.update({"X-CSRF-Token": client.cookies[control_plane.CSRF_COOKIE]})

    replay = client.post(
        "/api/auth/verify-otp",
        json={"challenge_id": challenge_id, "code": sent[0]},
    )
    assert replay.status_code == 401


def test_expired_challenge_is_rejected(otp_flow):
    client, sent = otp_flow
    challenge_id = _password_login(client).json()["challenge_id"]

    with control_plane._connection() as connection:
        connection.execute(
            "UPDATE auth_challenges SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", challenge_id),
        )

    response = client.post(
        "/api/auth/verify-otp",
        json={"challenge_id": challenge_id, "code": sent[0]},
    )
    assert response.status_code == 401
    assert "no longer valid" in response.json()["detail"].casefold()
    assert client.cookies.get(SESSION) is None


def test_resend_respects_the_cooldown_and_rotates_the_code(otp_flow):
    client, sent = otp_flow
    challenge_id = _password_login(client).json()["challenge_id"]

    too_soon = client.post("/api/auth/resend-otp", json={"challenge_id": challenge_id})
    assert too_soon.status_code == 429
    assert len(sent) == 1

    with control_plane._connection() as connection:
        connection.execute(
            "UPDATE auth_challenges SET last_sent_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", challenge_id),
        )

    resent = client.post("/api/auth/resend-otp", json={"challenge_id": challenge_id})
    assert resent.status_code == 200
    assert len(sent) == 2

    stale = client.post(
        "/api/auth/verify-otp",
        json={"challenge_id": challenge_id, "code": sent[0]},
    )
    assert stale.status_code == 401
    assert client.post(
        "/api/auth/verify-otp",
        json={"challenge_id": challenge_id, "code": sent[1]},
    ).status_code == 200


def test_repeated_wrong_codes_lock_the_challenge(otp_flow):
    client, sent = otp_flow
    challenge_id = _password_login(client).json()["challenge_id"]
    wrong = _wrong_code(sent[0])

    for _ in range(settings.auth_otp_max_attempts):
        response = client.post(
            "/api/auth/verify-otp",
            json={"challenge_id": challenge_id, "code": wrong},
        )
        assert response.status_code == 401

    blocked = client.post(
        "/api/auth/verify-otp",
        json={"challenge_id": challenge_id, "code": sent[0]},
    )
    assert blocked.status_code == 401
    assert client.cookies.get(SESSION) is None


def test_telegram_delivery_failure_does_not_authenticate(otp_flow, monkeypatch):
    client, _ = otp_flow

    async def failing_send(connection, chat_id, code, ttl_minutes):
        return False

    monkeypatch.setattr(telegram, "send_login_code", failing_send)
    response = _password_login(client)

    assert response.status_code == 502
    assert client.cookies.get(SESSION) is None
    with control_plane._connection() as connection:
        statuses = {
            row["status"] for row in connection.execute("SELECT status FROM auth_challenges")
        }
    assert "pending" not in statuses


def _mandatory_two_factor_client(anonymous_client, monkeypatch, *, token: str | None):
    control_plane.create_owner("owner", PASSWORD)
    monkeypatch.setattr(settings, "auth_telegram_otp_enabled", True)
    monkeypatch.setattr(settings, "telegram_enabled", True)
    if token is not None:
        monkeypatch.setattr(settings, "telegram_bot_token", token)
    csrf = anonymous_client.get("/api/auth/csrf").json()["csrf_token"]
    anonymous_client.headers.update({"X-CSRF-Token": csrf})
    return anonymous_client


def test_missing_pairing_blocks_sign_in(anonymous_client, monkeypatch):
    client = _mandatory_two_factor_client(anonymous_client, monkeypatch, token="123:TEST")
    response = client.post(
        "/api/auth/login", json={"username": "owner", "password": PASSWORD}
    )

    assert response.status_code == 409
    assert "paired Telegram chat" in response.json()["detail"]
    assert client.cookies.get(SESSION) is None
    assert client.get("/api/control/devices").status_code == 401
    assert "123:TEST" not in response.text


def test_missing_bot_token_blocks_sign_in(anonymous_client, monkeypatch):
    client = _mandatory_two_factor_client(anonymous_client, monkeypatch, token=None)
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    response = client.post(
        "/api/auth/login", json={"username": "owner", "password": PASSWORD}
    )

    assert response.status_code == 503
    assert client.cookies.get(SESSION) is None
    assert client.get("/api/control/devices").status_code == 401


def test_disabled_telegram_integration_blocks_sign_in(anonymous_client, monkeypatch):
    client = _mandatory_two_factor_client(anonymous_client, monkeypatch, token="123:TEST")
    monkeypatch.setattr(settings, "telegram_enabled", False)
    assert client.post(
        "/api/auth/login", json={"username": "owner", "password": PASSWORD}
    ).status_code == 503
    assert client.cookies.get(SESSION) is None


def test_disabled_two_factor_falls_back_to_password(otp_flow, monkeypatch):
    client, sent = otp_flow
    monkeypatch.setattr(settings, "auth_telegram_otp_enabled", False)

    response = _password_login(client)
    assert response.status_code == 200
    assert response.json()["two_factor"] is False
    assert client.cookies.get(SESSION)
    assert sent == []


def test_otp_endpoints_require_a_matching_csrf_token(otp_flow):
    client, sent = otp_flow
    challenge_id = _password_login(client).json()["challenge_id"]

    client.headers.update({"X-CSRF-Token": "stale-value"})
    assert client.post(
        "/api/auth/verify-otp",
        json={"challenge_id": challenge_id, "code": sent[0]},
    ).status_code == 403
    assert client.post(
        "/api/auth/resend-otp", json={"challenge_id": challenge_id}
    ).status_code == 403
    assert client.post(
        "/api/auth/cancel-otp", json={"challenge_id": challenge_id}
    ).status_code == 403


def test_challenge_creation_is_rate_limited(otp_flow):
    client, _ = otp_flow
    for _ in range(control_plane.OTP_CHALLENGE_LIMIT):
        assert _password_login(client).status_code == 200
    assert _password_login(client).status_code == 429


def test_only_a_hash_of_the_code_is_stored(otp_flow):
    client, sent = otp_flow
    _password_login(client)

    with control_plane._connection() as connection:
        row = connection.execute("SELECT otp_hash, status FROM auth_challenges").fetchone()
    assert row["status"] == "pending"
    assert row["otp_hash"] != sent[0]
    assert sent[0] not in row["otp_hash"]
    assert len(row["otp_hash"]) == 64
