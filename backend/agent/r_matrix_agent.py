import argparse
from getpass import getpass
import json
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx

SERVICE_NAME = "R Matrix Personal AI Agent"
AGENT_VERSION = "0.1.0"
POLL_SECONDS = 10


def validate_server_url(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Use a valid HTTPS server URL.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Non-local agent connections require HTTPS.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("The server URL must be an origin without credentials, path, query, or fragment.")
    return f"{parsed.scheme}://{parsed.netloc}"


def require_windows_credential_store() -> None:
    if os.name != "nt":
        raise RuntimeError("The companion agent is supported only on Windows.")
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError("Install the agent requirements to enable Windows Credential Manager.") from exc
    backend_name = type(keyring.get_keyring()).__module__.casefold()
    if "windows" not in backend_name:
        raise RuntimeError("A Windows Credential Manager keyring is required; refusing insecure token storage.")


def config_path() -> Path:
    root = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return root / "RMatrixAgent" / "config.json"


def save_device_config(server_url: str, device_id: str) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps({"server_url": server_url, "device_id": device_id}),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def load_device_config() -> dict[str, str]:
    path = config_path()
    config = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(config, dict)
        or set(config) != {"server_url", "device_id"}
        or not isinstance(config["server_url"], str)
        or not isinstance(config["device_id"], str)
    ):
        raise ValueError("Agent configuration is invalid.")
    return config


def enroll(server_url: str, name: str, code: str) -> None:
    require_windows_credential_store()
    import keyring

    with httpx.Client(timeout=15) as client:
        response = client.post(
            f"{server_url}/api/control/enroll",
            json={"name": name, "code": code},
        )
        response.raise_for_status()
        enrollment = response.json()
    if (
        not isinstance(enrollment, dict)
        or not isinstance(enrollment.get("device_token"), str)
        or not isinstance(enrollment.get("device_id"), str)
    ):
        raise ValueError("The control plane returned an invalid enrollment response.")
    try:
        keyring.set_password(SERVICE_NAME, server_url, enrollment["device_token"])
    except keyring.errors.KeyringError as exc:
        raise RuntimeError("Windows Credential Manager could not save the device credential.") from exc
    save_device_config(server_url, enrollment["device_id"])
    print(f"Enrolled device {enrollment['device_id']}. Its credential is stored in Windows Credential Manager.")


def run_agent() -> None:
    require_windows_credential_store()
    import keyring

    config = load_device_config()
    server_url = validate_server_url(config["server_url"])
    try:
        token = keyring.get_password(SERVICE_NAME, server_url)
    except keyring.errors.KeyringError as exc:
        raise RuntimeError("Windows Credential Manager could not read the device credential.") from exc
    if not isinstance(token, str) or not token:
        raise RuntimeError("No device credential is present in Windows Credential Manager; enroll this device again.")
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=20, headers=headers) as client:
        print("R Matrix agent is connected. Press Ctrl+C to stop.")
        while True:
            try:
                response = client.get(f"{server_url}/api/control/agent/jobs/next")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or set(payload) != {"job"}:
                    raise ValueError("Malformed job response.")
                job = payload["job"]
                if job is not None:
                    if not isinstance(job, dict) or set(job) != {"id", "operation", "expires_at"}:
                        raise ValueError("Malformed job payload.")
                    state, result = execute_job(job)
                    if result["status"] in {"invalid_job", "expired"}:
                        raise ValueError("Invalid or expired job.")
                    result_response = client.post(
                        f"{server_url}/api/control/agent/jobs/{job['id']}/result",
                        json={"status": state, "result": result},
                    )
                    result_response.raise_for_status()
                time.sleep(POLL_SECONDS)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    print("Device credential is invalid or revoked. Stop the agent and enroll it again if authorized.")
                    return
                print(f"Control-plane request failed with HTTP {exc.response.status_code}; retrying.")
                time.sleep(POLL_SECONDS)
            except httpx.RequestError:
                print("Control plane is unreachable; retrying.")
                time.sleep(POLL_SECONDS)
            except (KeyError, TypeError, ValueError):
                print("Control plane returned a malformed job response; retrying.")
                time.sleep(POLL_SECONDS)


def execute_job(job: object) -> tuple[str, dict[str, str]]:
    if not isinstance(job, dict):
        return "failed", {"status": "invalid_job"}
    if set(job) != {"id", "operation", "expires_at"}:
        return "failed", {"status": "invalid_job"}
    try:
        UUID(str(job["id"]))
        expires_at = datetime.fromisoformat(str(job["expires_at"]))
    except (ValueError, TypeError):
        return "failed", {"status": "invalid_job"}
    if expires_at.tzinfo is None:
        return "failed", {"status": "invalid_job"}
    if expires_at <= datetime.now(timezone.utc):
        return "failed", {"status": "expired"}
    operation = job.get("operation")
    if operation == "health.check":
        return "succeeded", {"status": "ok"}
    if operation == "agent.status":
        return "succeeded", {
            "status": "ok",
            "platform": platform.platform()[:100],
            "python_version": platform.python_version()[:100],
            "agent_version": AGENT_VERSION,
        }
    return "failed", {"status": "unsupported_operation"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Outbound-only R Matrix Windows companion agent.")
    parser.add_argument("--enroll", action="store_true", help="Enroll using a single-use code.")
    parser.add_argument("--server", help="Control-plane origin, for example https://matrix.example.com")
    parser.add_argument("--name", default=platform.node() or "Windows device")
    args = parser.parse_args()
    if args.enroll:
        if not args.server:
            parser.error("--server is required with --enroll")
        try:
            server_url = validate_server_url(args.server)
            code = getpass("Single-use enrollment code: ").strip()
            enroll(server_url, args.name, code)
        except (ValueError, RuntimeError, httpx.HTTPError, OSError):
            raise SystemExit("Enrollment failed. Check the server, code, HTTPS connection, and Windows Credential Manager.")
        return
    try:
        run_agent()
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError):
        raise SystemExit("Agent could not start. Check its local configuration and Windows Credential Manager.")
    except KeyboardInterrupt:
        print("Agent stopped.")


if __name__ == "__main__":
    main()
