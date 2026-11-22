import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import settings

SESSION_COOKIE = "r_matrix_session"
CSRF_COOKIE = "r_matrix_csrf"
PASSWORD_ITERATIONS = 600_000
LEGACY_PASSWORD_ITERATIONS = 310_000
JWT_ISSUER = "r-matrix"
JWT_AUDIENCE = "r-matrix-dashboard"
ENROLLMENT_MINUTES = 10
JOB_TTL_MINUTES = 15
ALLOWED_OPERATIONS = {"health.check", "agent.status"}
OTP_CODE_DIGITS = 6
OTP_HASH_DOMAIN = "r-matrix-login-otp:v1:"
OTP_CHALLENGE_WINDOW_MINUTES = 15
OTP_CHALLENGE_LIMIT = 5
OTP_MAX_SENDS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _jwt_secret() -> bytes | None:
    secret = settings.auth_jwt_secret.encode("utf-8")
    return secret if len(secret) >= 32 else None


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _create_access_token(user_id: str, issued_at: datetime, expires_at: datetime) -> str:
    secret = _jwt_secret()
    if secret is None:
        raise RuntimeError("AUTH_JWT_SECRET must be configured with at least 32 bytes.")
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url_encode(
        json.dumps(
            {
                "aud": JWT_AUDIENCE,
                "exp": int(expires_at.timestamp()),
                "iat": int(issued_at.timestamp()),
                "iss": JWT_ISSUER,
                "jti": secrets.token_urlsafe(24),
                "sub": user_id,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}"
    signature = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(signature)}"


def _decode_access_token(token: str) -> dict[str, Any] | None:
    secret = _jwt_secret()
    if secret is None or len(token) > 4096:
        return None
    try:
        header_part, payload_part, signature_part = token.split(".")
        header = json.loads(_b64url_decode(header_part))
        claims = json.loads(_b64url_decode(payload_part))
        signature = _b64url_decode(signature_part)
        if not isinstance(header, dict) or header != {"alg": "HS256", "typ": "JWT"}:
            return None
        expected = hmac.new(
            secret,
            f"{header_part}.{payload_part}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected) or not isinstance(claims, dict):
            return None
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        now = int(_now().timestamp())
        if (
            type(issued_at) is not int
            or type(expires_at) is not int
            or expires_at <= now
            or issued_at > now + 60
            or expires_at <= issued_at
            or claims.get("iss") != JWT_ISSUER
            or claims.get("aud") != JWT_AUDIENCE
            or not isinstance(claims.get("sub"), str)
            or not claims["sub"]
            or not isinstance(claims.get("jti"), str)
            or not claims["jti"]
        ):
            return None
        return claims
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _connection() -> sqlite3.Connection:
    path: Path = settings.control_database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize() -> None:
    with _connection() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )"""
        )
        current = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
        if current < 1:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS owner_accounts (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    password_iterations INTEGER NOT NULL DEFAULT 310000,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_attempts (
                    username_hash TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL,
                    window_started_at TEXT NOT NULL,
                    locked_until TEXT
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES owner_accounts(id) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS enrollment_codes (
                    code_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES owner_accounts(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES owner_accounts(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    credential_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS control_jobs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES owner_accounts(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    operation TEXT NOT NULL CHECK(operation IN ('health.check', 'agent.status')),
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'expired')),
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_control_jobs_device_status
                    ON control_jobs(device_id, status, created_at);
                CREATE TABLE IF NOT EXISTS control_audit (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    device_id TEXT,
                    event TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
                (_timestamp(_now()),),
            )
        current = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
        if current < 2:
            owner_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(owner_accounts)").fetchall()
            }
            if "password_iterations" not in owner_columns:
                connection.execute(
                    f"ALTER TABLE owner_accounts ADD COLUMN password_iterations INTEGER NOT NULL DEFAULT {LEGACY_PASSWORD_ITERATIONS}"
                )
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (2, ?)",
                (_timestamp(_now()),),
            )


        current = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
        if current < 3:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_challenges (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES owner_accounts(id) ON DELETE CASCADE,
                    username TEXT NOT NULL,
                    otp_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'verified', 'cancelled', 'expired', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    sends INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_sent_at TEXT NOT NULL,
                    verified_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_auth_challenges_user_status
                    ON auth_challenges(user_id, status, created_at);
                """
            )
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (3, ?)",
                (_timestamp(_now()),),
            )


def health_check() -> None:
    with _connection() as connection:
        connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()


def create_owner(username: str, password: str) -> bool:
    normalized = username.strip()
    if not normalized or len(normalized) > 80 or not 12 <= len(password) <= 1024:
        raise ValueError("Use a username of 1-80 characters and a password of at least 12 and at most 1024 characters.")
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    ).hex()
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM owner_accounts LIMIT 1").fetchone():
            return False
        try:
            connection.execute(
                """INSERT INTO owner_accounts
                   (id, username, password_salt, password_hash, password_iterations, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid4()),
                    normalized,
                    salt.hex(),
                    password_hash,
                    PASSWORD_ITERATIONS,
                    _timestamp(_now()),
                ),
            )
        except sqlite3.IntegrityError:
            return False
    return True


def _verify_password(
    password: str,
    salt_hex: str,
    expected_hash: str,
    iterations: int,
) -> bool:
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations
    ).hex()
    return hmac.compare_digest(actual, expected_hash)


def verify_credentials(username: str, password: str) -> dict[str, str] | None:
    """Checks the owner username and password without creating a session.

    Lockout windows, the dummy hash for unknown accounts and the PBKDF2
    iteration upgrade are unchanged, so password-only deployments keep their
    existing behaviour. A session is issued separately, after any second
    factor has been satisfied.
    """
    username_hash = _digest(username.strip().casefold())
    if _jwt_secret() is None:
        return None
    with _connection() as connection:
        now = _now()
        attempt = connection.execute(
            "SELECT attempts, window_started_at, locked_until FROM login_attempts WHERE username_hash = ?",
            (username_hash,),
        ).fetchone()
        if attempt and attempt["locked_until"] and attempt["locked_until"] > _timestamp(now):
            return None
        account = connection.execute(
            """SELECT id, username, password_salt, password_hash, password_iterations FROM owner_accounts
               WHERE id = (
                   SELECT id FROM owner_accounts ORDER BY created_at, rowid LIMIT 1
               ) AND username = ?""",
            (username.strip(),),
        ).fetchone()
        if account:
            valid = _verify_password(
                password,
                account["password_salt"],
                account["password_hash"],
                account["password_iterations"],
            )
        else:
            _verify_password(password, "00" * 16, "00" * 32, PASSWORD_ITERATIONS)
            valid = False
        if not valid:
            window_started = (
                attempt["window_started_at"]
                if attempt and _timestamp(now - timedelta(minutes=15)) < attempt["window_started_at"]
                else _timestamp(now)
            )
            attempts = attempt["attempts"] + 1 if attempt and window_started == attempt["window_started_at"] else 1
            locked_until = _timestamp(now + timedelta(minutes=15)) if attempts >= 5 else None
            connection.execute(
                """INSERT INTO login_attempts (username_hash, attempts, window_started_at, locked_until)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(username_hash) DO UPDATE SET
                     attempts = excluded.attempts,
                     window_started_at = excluded.window_started_at,
                     locked_until = excluded.locked_until""",
                (username_hash, attempts, window_started, locked_until),
            )
            return None
        if account["password_iterations"] < PASSWORD_ITERATIONS:
            salt = secrets.token_bytes(16)
            password_hash = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt,
                PASSWORD_ITERATIONS,
            ).hex()
            connection.execute(
                """UPDATE owner_accounts
                   SET password_salt = ?, password_hash = ?, password_iterations = ?
                   WHERE id = ?""",
                (salt.hex(), password_hash, PASSWORD_ITERATIONS, account["id"]),
            )
        connection.execute("DELETE FROM login_attempts WHERE username_hash = ?", (username_hash,))
        return {"id": account["id"], "username": account["username"]}


def start_session(user_id: str) -> dict[str, str] | None:
    """Issues a normal owner session for an already verified owner account."""
    with _connection() as connection:
        account = connection.execute(
            """SELECT id, username FROM owner_accounts
               WHERE id = ? AND id = (
                   SELECT id FROM owner_accounts ORDER BY created_at, rowid LIMIT 1
               )""",
            (user_id,),
        ).fetchone()
        if account is None:
            return None
        now = _now()
        expires_at = now + timedelta(hours=settings.auth_session_hours)
        session_token = _create_access_token(account["id"], now, expires_at)
        csrf_token = secrets.token_urlsafe(32)
        connection.execute(
            "DELETE FROM auth_sessions WHERE expires_at <= ?",
            (_timestamp(now),),
        )
        connection.execute(
            """INSERT INTO auth_sessions (token_hash, user_id, csrf_hash, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                _digest(session_token),
                account["id"],
                _digest(csrf_token),
                _timestamp(expires_at),
                _timestamp(now),
            ),
        )
        return {
            "id": account["id"],
            "username": account["username"],
            "token": session_token,
            "csrf_token": csrf_token,
        }


def authenticate(username: str, password: str) -> dict[str, str] | None:
    """Password-only convenience wrapper kept for backward compatibility."""
    account = verify_credentials(username, password)
    if account is None:
        return None
    return start_session(account["id"])


def get_session_user(token: str) -> dict[str, str] | None:
    claims = _decode_access_token(token)
    if claims is None:
        return None
    with _connection() as connection:
        row = connection.execute(
            """SELECT a.id, a.username FROM auth_sessions s
               JOIN owner_accounts a ON a.id = s.user_id
               WHERE s.token_hash = ? AND s.user_id = ? AND s.expires_at > ?
                 AND a.id = (
                     SELECT id FROM owner_accounts ORDER BY created_at, rowid LIMIT 1
                 )""",
            (_digest(token), claims["sub"], _timestamp(_now())),
        ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"], "session_token": token}


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def compare_csrf(cookie_token: str, header_token: str) -> bool:
    return bool(cookie_token and header_token and hmac.compare_digest(cookie_token, header_token))


def validate_csrf(session_token: str, cookie_token: str, header_token: str) -> bool:
    if not compare_csrf(cookie_token, header_token):
        return False
    with _connection() as connection:
        row = connection.execute(
            """SELECT 1 FROM auth_sessions
               WHERE token_hash = ? AND csrf_hash = ? AND expires_at > ?""",
            (_digest(session_token), _digest(cookie_token), _timestamp(_now())),
        ).fetchone()
    return row is not None


def revoke_session(token: str) -> None:
    with _connection() as connection:
        connection.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_digest(token),))


def create_enrollment_code(user_id: str) -> dict[str, str]:
    code = secrets.token_urlsafe(32)
    now = _now()
    with _connection() as connection:
        connection.execute(
            """INSERT INTO enrollment_codes (code_hash, user_id, expires_at, created_at)
               VALUES (?, ?, ?, ?)""",
            (_digest(code), user_id, _timestamp(now + timedelta(minutes=ENROLLMENT_MINUTES)), _timestamp(now)),
        )
        connection.execute(
            "INSERT INTO control_audit (id, user_id, event, created_at) VALUES (?, ?, ?, ?)",
            (str(uuid4()), user_id, "device.enrollment_code_created", _timestamp(now)),
        )
    return {"code": code, "expires_at": _timestamp(now + timedelta(minutes=ENROLLMENT_MINUTES))}


def enroll_device(code: str, name: str) -> dict[str, str] | None:
    now = _timestamp(_now())
    token = secrets.token_urlsafe(48)
    device_id = str(uuid4())
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT user_id FROM enrollment_codes WHERE code_hash = ? AND expires_at > ?",
            (_digest(code), now),
        ).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM enrollment_codes WHERE code_hash = ?", (_digest(code),))
        connection.execute(
            """INSERT INTO devices (id, user_id, name, credential_hash, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (device_id, row["user_id"], name.strip(), _digest(token), now),
        )
        connection.execute(
            "INSERT INTO control_audit (id, user_id, device_id, event, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), row["user_id"], device_id, "device.enrolled", now),
        )
    return {"device_id": device_id, "device_token": token}


def list_devices(user_id: str) -> list[dict[str, Any]]:
    with _connection() as connection:
        rows = connection.execute(
            """SELECT id, name, created_at, last_seen_at, revoked_at
               FROM devices WHERE user_id = ? ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def revoke_device(user_id: str, device_id: str) -> bool:
    now = _timestamp(_now())
    with _connection() as connection:
        cursor = connection.execute(
            "UPDATE devices SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (now, device_id, user_id),
        )
        if cursor.rowcount:
            connection.execute(
                "UPDATE control_jobs SET status = 'expired' WHERE device_id = ? AND status IN ('queued', 'running')",
                (device_id,),
            )
            connection.execute(
                "INSERT INTO control_audit (id, user_id, device_id, event, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid4()), user_id, device_id, "device.revoked", now),
            )
    return bool(cursor.rowcount)


def get_agent_device(token: str) -> dict[str, str] | None:
    with _connection() as connection:
        row = connection.execute(
            """SELECT id, user_id, name FROM devices
               WHERE credential_hash = ? AND revoked_at IS NULL""",
            (_digest(token),),
        ).fetchone()
    return dict(row) if row else None


def create_job(user_id: str, device_id: str, operation: str) -> dict[str, str] | None:
    if operation not in ALLOWED_OPERATIONS:
        raise ValueError("The requested operation is not allowed.")
    now = _now()
    job_id = str(uuid4())
    expires_at = _timestamp(now + timedelta(minutes=JOB_TTL_MINUTES))
    with _connection() as connection:
        device = connection.execute(
            "SELECT id FROM devices WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (device_id, user_id),
        ).fetchone()
        if not device:
            return None
        connection.execute(
            """INSERT INTO control_jobs
               (id, user_id, device_id, operation, status, created_at, expires_at)
               VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
            (job_id, user_id, device_id, operation, _timestamp(now), expires_at),
        )
        connection.execute(
            "INSERT INTO control_audit (id, user_id, device_id, event, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), user_id, device_id, f"job.queued.{operation}", _timestamp(now)),
        )
    return {"id": job_id, "device_id": device_id, "operation": operation, "status": "queued", "expires_at": expires_at}


def claim_job(device_id: str) -> dict[str, str] | None:
    now = _timestamp(_now())
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE control_jobs SET status = 'expired' WHERE device_id = ? AND status IN ('queued', 'running') AND expires_at <= ?",
            (device_id, now),
        )
        connection.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (now, device_id))
        job = connection.execute(
            """SELECT id, operation, expires_at FROM control_jobs
               WHERE device_id = ? AND status = 'queued' AND expires_at > ?
               ORDER BY created_at LIMIT 1""",
            (device_id, now),
        ).fetchone()
        if job is None:
            return None
        connection.execute(
            "UPDATE control_jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
            (now, job["id"]),
        )
    return dict(job)


def complete_job(device_id: str, job_id: str, state: str, result: dict[str, Any]) -> bool:
    if state not in {"succeeded", "failed"}:
        raise ValueError("Unsupported job result state.")
    if len(json.dumps(result, separators=(",", ":")).encode("utf-8")) > 4096:
        raise ValueError("The result exceeds the allowed size.")
    allowed_fields = {
        "health.check": {"status"},
        "agent.status": {"status", "platform", "python_version", "agent_version"},
    }
    with _connection() as connection:
        job = connection.execute(
            """SELECT operation, user_id FROM control_jobs
               WHERE id = ? AND device_id = ? AND status = 'running' AND expires_at > ?""",
            (job_id, device_id, _timestamp(_now())),
        ).fetchone()
        if job is None:
            return False
        if set(result) - allowed_fields[job["operation"]]:
            raise ValueError("The result contains fields not allowed for this operation.")
        if any(not isinstance(value, str) or len(value) > 100 for value in result.values()):
            raise ValueError("Result values must be short strings.")
        if "status" not in result:
            raise ValueError("The result must include a status.")
        now = _timestamp(_now())
        connection.execute(
            "UPDATE control_jobs SET status = ?, result_json = ?, finished_at = ? WHERE id = ?",
            (state, json.dumps(result, separators=(",", ":")), now, job_id),
        )
        connection.execute(
            "INSERT INTO control_audit (id, user_id, device_id, event, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), job["user_id"], device_id, f"job.{state}.{job['operation']}", now),
        )
    return True


def list_jobs(user_id: str) -> list[dict[str, Any]]:
    with _connection() as connection:
        rows = connection.execute(
            """SELECT id, device_id, operation, status, result_json, created_at, expires_at, finished_at
               FROM control_jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 100""",
            (user_id,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "device_id": row["device_id"],
            "operation": row["operation"],
            "status": row["status"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "finished_at": row["finished_at"],
        }
        for row in rows
    ]


def _otp_hash(challenge_id: str, code: str) -> str:
    return hashlib.sha256(f"{OTP_HASH_DOMAIN}{challenge_id}:{code}".encode("utf-8")).hexdigest()


def _new_otp_code() -> str:
    return f"{secrets.randbelow(10 ** OTP_CODE_DIGITS):0{OTP_CODE_DIGITS}d}"


def create_otp_challenge(user_id: str, username: str) -> dict[str, Any] | None:
    """Creates a single-use challenge and returns the plaintext code exactly once.

    Only a salted SHA-256 hash of the code is stored. ``None`` means the owner
    has requested too many challenges recently.
    """
    now = _now()
    challenge_id = str(uuid4())
    code = _new_otp_code()
    expires_at = _timestamp(now + timedelta(minutes=settings.auth_otp_ttl_minutes))
    resend_at = _timestamp(now + timedelta(seconds=settings.auth_otp_resend_seconds))
    with _connection() as connection:
        recent = connection.execute(
            "SELECT COUNT(*) FROM auth_challenges WHERE user_id = ? AND created_at > ?",
            (user_id, _timestamp(now - timedelta(minutes=OTP_CHALLENGE_WINDOW_MINUTES))),
        ).fetchone()[0]
        if recent >= OTP_CHALLENGE_LIMIT:
            return None
        connection.execute(
            "DELETE FROM auth_challenges WHERE created_at < ?",
            (_timestamp(now - timedelta(days=1)),),
        )
        connection.execute(
            "UPDATE auth_challenges SET status = 'cancelled' WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        )
        connection.execute(
            """INSERT INTO auth_challenges
               (id, user_id, username, otp_hash, status, attempts, sends, created_at, expires_at, last_sent_at)
               VALUES (?, ?, ?, ?, 'pending', 0, 1, ?, ?, ?)""",
            (
                challenge_id,
                user_id,
                username,
                _otp_hash(challenge_id, code),
                _timestamp(now),
                expires_at,
                _timestamp(now),
            ),
        )
    return {"id": challenge_id, "code": code, "expires_at": expires_at, "resend_at": resend_at}


def challenge_is_pending(challenge_id: str) -> bool:
    with _connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM auth_challenges WHERE id = ? AND status = 'pending' AND expires_at > ?",
            (challenge_id, _timestamp(_now())),
        ).fetchone()
    return row is not None


def resend_otp_challenge(challenge_id: str) -> dict[str, Any]:
    """Rotates the code for a pending challenge, enforcing cooldown and a send cap."""
    now = _now()
    with _connection() as connection:
        row = connection.execute(
            "SELECT id, status, sends, last_sent_at, expires_at FROM auth_challenges WHERE id = ?",
            (challenge_id,),
        ).fetchone()
        if row is None:
            return {"status": "unknown"}
        if row["status"] != "pending" or row["expires_at"] <= _timestamp(now):
            return {"status": "expired"}
        if int(row["sends"]) >= OTP_MAX_SENDS:
            return {"status": "limited"}
        try:
            last_sent = datetime.fromisoformat(row["last_sent_at"])
        except ValueError:
            last_sent = now - timedelta(hours=1)
        wait_seconds = (last_sent + timedelta(seconds=settings.auth_otp_resend_seconds) - now).total_seconds()
        if wait_seconds > 0:
            return {"status": "cooldown", "retry_after": int(wait_seconds) + 1}
        code = _new_otp_code()
        expires_at = _timestamp(now + timedelta(minutes=settings.auth_otp_ttl_minutes))
        connection.execute(
            """UPDATE auth_challenges
               SET otp_hash = ?, sends = sends + 1, last_sent_at = ?, expires_at = ?
               WHERE id = ?""",
            (_otp_hash(challenge_id, code), _timestamp(now), expires_at, challenge_id),
        )
    return {
        "status": "sent",
        "code": code,
        "expires_at": expires_at,
        "resend_at": _timestamp(now + timedelta(seconds=settings.auth_otp_resend_seconds)),
    }


def verify_otp_challenge(challenge_id: str, code: str) -> dict[str, Any]:
    """Verifies a code and, only on success, returns a freshly issued owner session."""
    normalized = "".join(character for character in (code or "") if character.isdigit())
    now = _now()
    with _connection() as connection:
        row = connection.execute(
            """SELECT id, user_id, status, otp_hash, attempts, expires_at
               FROM auth_challenges WHERE id = ?""",
            (challenge_id,),
        ).fetchone()
        if row is None:
            return {"status": "unknown"}
        if row["status"] in {"verified", "cancelled"}:
            return {"status": "used"}
        if row["status"] == "failed":
            return {"status": "locked"}
        if row["status"] == "expired" or row["expires_at"] <= _timestamp(now):
            connection.execute(
                "UPDATE auth_challenges SET status = 'expired' WHERE id = ? AND status = 'pending'",
                (challenge_id,),
            )
            return {"status": "expired"}
        if len(normalized) != OTP_CODE_DIGITS or not hmac.compare_digest(
            _otp_hash(challenge_id, normalized), row["otp_hash"]
        ):
            attempts = int(row["attempts"]) + 1
            status = "failed" if attempts >= settings.auth_otp_max_attempts else "pending"
            connection.execute(
                "UPDATE auth_challenges SET attempts = ?, status = ? WHERE id = ?",
                (attempts, status, challenge_id),
            )
            return {
                "status": "invalid",
                "attempts_left": max(0, settings.auth_otp_max_attempts - attempts),
            }
        connection.execute(
            "UPDATE auth_challenges SET status = 'verified', verified_at = ? WHERE id = ?",
            (_timestamp(now), challenge_id),
        )
        connection.execute(
            """UPDATE auth_challenges SET status = 'cancelled'
               WHERE user_id = ? AND id != ? AND status = 'pending'""",
            (row["user_id"], challenge_id),
        )
    session = start_session(row["user_id"])
    if session is None:
        return {"status": "unknown"}
    return {"status": "verified", "session": session}


def cancel_otp_challenge(challenge_id: str) -> bool:
    with _connection() as connection:
        cursor = connection.execute(
            "UPDATE auth_challenges SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (challenge_id,),
        )
    return bool(cursor.rowcount)
