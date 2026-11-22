import ipaddress
from urllib.parse import urlsplit


class UnsafeURL(ValueError):
    """A URL is not safe for outbound webpage retrieval."""


def validate_web_url(value: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURL("The source URL is invalid.") from exc

    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or "\\" in parsed.netloc
        or any(character.isspace() for character in value)
    ):
        raise UnsafeURL("Only credential-free HTTP and HTTPS source URLs are allowed.")

    hostname = hostname.rstrip(".").casefold()
    if (
        not hostname
        or "%" in hostname
        or hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".example"))
    ):
        raise UnsafeURL("Local and reserved hostnames are not allowed.")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if "." not in hostname:
            raise UnsafeURL("Single-label hostnames are not allowed.")
    else:
        _require_public_address(address)

    expected_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != expected_port:
        raise UnsafeURL("Non-standard web ports are not allowed.")

    return parsed.scheme, hostname, expected_port


def require_public_ip(value: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise UnsafeURL("The hostname resolved to an invalid IP address.") from exc
    _require_public_address(address)


def _require_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if not address.is_global:
        raise UnsafeURL("The source host must resolve to a public IP address.")
