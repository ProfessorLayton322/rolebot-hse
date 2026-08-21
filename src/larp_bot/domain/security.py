import base64
import hashlib
import hmac

from .models import Platform


def participant_key(secret: str, platform: Platform, user_id: int, event_id: str) -> str:
    """Return a URL-safe opaque deterministic per-user, per-event identifier."""
    message = f"{platform.value}:{user_id}:{event_id}".encode()
    digest = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def canonical_request(timestamp: str, request_id: str, method: str, path: str, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join((timestamp, request_id, method.upper(), path, body_hash)).encode()


def sign_request(secret: str, timestamp: str, request_id: str, method: str, path: str, body: bytes) -> str:
    return hmac.new(
        secret.encode(), canonical_request(timestamp, request_id, method, path, body), hashlib.sha256
    ).hexdigest()


def constant_time_valid_signature(expected: str, supplied: str) -> bool:
    return bool(supplied) and hmac.compare_digest(expected, supplied)
