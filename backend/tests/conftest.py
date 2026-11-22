import base64
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Prevent tests from reading or using a locally configured Gemini credential.
os.environ["GEMINI_API_KEY"] = ""
# Never let the test suite start real Telegram long polling with the owner's bot token.
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["SMTP_HOST"] = ""
os.environ["SMTP_USERNAME"] = ""
os.environ["SMTP_PASSWORD"] = ""
os.environ["SMTP_FROM"] = ""
# Deterministic non-production key so document encryption runs in tests without
# ever reading the owner's real DOCUMENT_ENCRYPTION_KEY.
os.environ["DOCUMENT_ENCRYPTION_KEY"] = base64.b64encode(b"r-matrix-test-key-32-bytes-long!").decode()

from app import config
from app import control_plane
from app.main import app

TEST_OWNER_PASSWORD = "correct horse battery staple"
TEST_JWT_SECRET = "test-only-signing-secret-with-32-bytes-minimum"


def _configure_test_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(config.settings, "max_upload_bytes", 1024)
    monkeypatch.setattr(config.settings, "auth_jwt_secret", TEST_JWT_SECRET)
    # Password-only fixtures: mandatory Telegram OTP is exercised by dedicated tests.
    monkeypatch.setattr(config.settings, "auth_telegram_otp_enabled", False)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_test_settings(tmp_path, monkeypatch)
    with TestClient(app) as test_client:
        assert control_plane.create_owner("owner", TEST_OWNER_PASSWORD)
        csrf = test_client.get("/api/auth/csrf").json()["csrf_token"]
        response = test_client.post(
            "/api/auth/login",
            headers={"X-CSRF-Token": csrf},
            json={"username": "owner", "password": TEST_OWNER_PASSWORD},
        )
        assert response.status_code == 200
        test_client.headers.update({"X-CSRF-Token": test_client.cookies["r_matrix_csrf"]})
        yield test_client


@pytest.fixture
def anonymous_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_test_settings(tmp_path, monkeypatch)
    with TestClient(app) as test_client:
        yield test_client
