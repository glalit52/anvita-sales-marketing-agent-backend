import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import get_settings
from app.core.errors import Unauthorized

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hmac_sha256_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def encode_jwt(claims: dict[str, Any], ttl: timedelta) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {**claims, "iat": now, "exp": now + ttl, "jti": uuid.uuid4().hex}
    return jwt.encode(payload, s.jwt_secret.get_secret_value(), algorithm=s.jwt_algorithm)


def decode_jwt(token: str, *, expected_type: str) -> dict[str, Any]:
    s = get_settings()
    try:
        claims = jwt.decode(token, s.jwt_secret.get_secret_value(), algorithms=[s.jwt_algorithm])
    except jwt.ExpiredSignatureError as e:
        raise Unauthorized("Token expired", code="token_expired") from e
    except jwt.PyJWTError as e:
        raise Unauthorized("Invalid token") from e
    if claims.get("typ") != expected_type:
        raise Unauthorized("Invalid token type")
    return claims


def create_access_token(user_id: uuid.UUID, org_id: uuid.UUID, role: str) -> str:
    ttl = timedelta(minutes=get_settings().access_token_ttl_minutes)
    return encode_jwt({"typ": "access", "sub": str(user_id), "org": str(org_id), "role": role}, ttl)


def create_webchat_token(org_id: uuid.UUID, conversation_id: uuid.UUID, contact_id: uuid.UUID) -> str:
    ttl = timedelta(days=get_settings().webchat_token_ttl_days)
    claims = {"typ": "webchat", "org": str(org_id), "conv": str(conversation_id), "sub": str(contact_id)}
    return encode_jwt(claims, ttl)
