import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def signing_key(secret: str) -> Ed25519PrivateKey:
    if not secret:
        raise ValueError("QQ_APP_SECRET 尚未配置")
    raw = secret.encode("utf-8")
    seed = (raw * ((32 + len(raw) - 1) // len(raw)))[:32]
    return Ed25519PrivateKey.from_private_bytes(seed)


def verify_callback(secret: str, timestamp: str, signature: str, body: bytes) -> bool:
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
        signing_key(secret).public_key().verify(bytes.fromhex(signature), timestamp.encode() + body)
        return True
    except (ValueError, InvalidSignature, TypeError):
        return False


def challenge(secret: str, data: dict) -> dict:
    token, event_ts = data["plain_token"], data["event_ts"]
    if not isinstance(token, str) or not isinstance(event_ts, str) or len(token) > 1024:
        raise ValueError("无效的回调挑战")
    return {"plain_token": token, "signature": signing_key(secret).sign((event_ts + token).encode()).hex()}
