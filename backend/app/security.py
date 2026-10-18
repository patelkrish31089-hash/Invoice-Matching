from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import AuthSession, User, utcnow

logger = logging.getLogger("ledgerlens.auth")


def _log_auth_rejection(request: Request, reason: str) -> None:
    # Correlate auth failures without ever recording bearer tokens or passwords.
    logger.warning(
        "auth_rejected request_id=%s path=%s reason=%s",
        getattr(request.state, "request_id", "unknown"), request.url.path, reason,
    )


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = 310_000) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iteration_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iteration_text))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def token_fingerprint(jti: str) -> str:
    """Return a non-reversible database key for a random JWT ID."""
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def create_access_token(
    user_id: str,
    *,
    secret: str | None = None,
    expires_minutes: int | None = None,
    jti: str | None = None,
) -> str:
    secret = secret or settings.secret_key
    now = int(time.time())
    lifetime = settings.access_token_expire_minutes if expires_minutes is None else expires_minutes
    claims = {
        "sub": user_id,
        "iat": now,
        "exp": now + int(lifetime * 60),
        "iss": "ledgerlens-local",
        "type": "access",
        # A random per-token identifier allows server-side revocation without storing
        # the bearer token itself. Only its SHA-256 fingerprint is persisted.
        "jti": jti or secrets.token_urlsafe(24),
    }
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(claims, separators=(',', ':')).encode())}"
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


def create_persisted_access_token(db: Session, user: User) -> str:
    """Create a signed token and persist only its revocable session fingerprint."""
    token = create_access_token(user.id)
    claims = decode_access_token(token)
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti:
        raise ValueError("New access token has no session identifier")
    db.add(AuthSession(
        token_hash=token_fingerprint(jti),
        user_id=user.id,
        expires_at_epoch=int(claims["exp"]),
    ))
    return token


def revoke_access_session(db: Session, token_hash: str) -> bool:
    """Revoke one active access session; never log or expose its token fingerprint."""
    session = db.get(AuthSession, token_hash)
    if session is None or session.revoked_at is not None:
        return False
    session.revoked_at = utcnow()
    return True


def prune_expired_access_sessions(db: Session, *, now_epoch: int | None = None) -> int:
    """Prune expired revocation metadata without touching any currently valid session."""
    cutoff = int(time.time()) if now_epoch is None else int(now_epoch)
    result = db.execute(delete(AuthSession).where(AuthSession.expires_at_epoch <= cutoff))
    return max(0, int(result.rowcount or 0))


def decode_access_token(token: str, *, secret: str | None = None) -> dict[str, Any]:
    secret = secret or settings.secret_key
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed bearer token")
    signing_input = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _unb64(parts[2])):
        raise ValueError("Invalid bearer token signature")
    header = json.loads(_unb64(parts[0]))
    claims = json.loads(_unb64(parts[1]))
    if header.get("alg") != "HS256" or claims.get("type") != "access" or claims.get("iss") != "ledgerlens-local":
        raise ValueError("Unsupported bearer token")
    jti = claims.get("jti")
    if not isinstance(jti, str) or len(jti) < 24:
        raise ValueError("Bearer token has no valid session identifier")
    if int(claims.get("exp", 0)) <= int(time.time()):
        raise ValueError("Bearer token expired")
    return claims


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    unauthorized_headers = {"WWW-Authenticate": "Bearer"}
    if scheme.lower() != "bearer" or not token.strip():
        _log_auth_rejection(request, "missing_authorization")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required", headers=unauthorized_headers)
    try:
        claims = decode_access_token(token.strip())
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        expired = str(exc) == "Bearer token expired"
        _log_auth_rejection(request, "expired_token" if expired else "invalid_token")
        detail = "Bearer token expired" if expired else "Invalid bearer token"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers=unauthorized_headers) from None
    subject = claims.get("sub")
    jti = claims.get("jti")
    if not isinstance(subject, str) or not subject or not isinstance(jti, str):
        _log_auth_rejection(request, "invalid_claims")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token", headers=unauthorized_headers)
    session_hash = token_fingerprint(jti)
    auth_session = db.get(AuthSession, session_hash)
    if (
        auth_session is None
        or auth_session.user_id != subject
        or auth_session.revoked_at is not None
        or auth_session.expires_at_epoch != int(claims["exp"])
        or auth_session.expires_at_epoch <= int(time.time())
    ):
        _log_auth_rejection(request, "inactive_session")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token", headers=unauthorized_headers)
    user = db.get(User, subject)
    if not user:
        _log_auth_rejection(request, "unknown_user")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token", headers=unauthorized_headers)
    if not user.is_active:
        _log_auth_rejection(request, "inactive_user")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User account is inactive", headers=unauthorized_headers)
    request.state.auth_session_hash = session_hash
    request.state.auth_token_exp = int(claims["exp"])
    return user


def require_roles(*allowed_roles: str) -> Callable:
    def dependency(user: User = Depends(get_current_user)) -> User:
        role_names = {role.name for role in user.roles}
        if not role_names.intersection(allowed_roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your role cannot perform this action")
        return user
    return dependency
