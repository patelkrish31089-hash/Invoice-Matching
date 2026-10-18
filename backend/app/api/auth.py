from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import append_audit_event
from app.config import settings
from app.db import get_db
from app.models import User
from app.schemas import LoginRequest
from app.security import (
    create_persisted_access_token,
    get_current_user,
    prune_expired_access_sessions,
    revoke_access_session,
    verify_password,
)
from app.api.common import user_payload

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/login")
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    # Normalize login lookup as well as input so legacy mixed-case addresses keep working.
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email or password is incorrect")
    prune_expired_access_sessions(db)
    token = create_persisted_access_token(db, user)
    append_audit_event(db, actor_id=user.id, action="auth.login", object_type="user", object_id=user.id, details={"method": "password"})
    db.commit()
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.access_token_expire_minutes * 60,
        "user": user_payload(user),
    }


@router.post("/refresh")
def refresh(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    current_session_hash = getattr(request.state, "auth_session_hash", None)
    if not current_session_hash or not revoke_access_session(db, current_session_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token")
    token = create_persisted_access_token(db, user)
    append_audit_event(db, actor_id=user.id, action="auth.session_refreshed", object_type="user", object_id=user.id, details={"previous_session_revoked": True})
    db.commit()
    return {"access_token": token, "token_type": "bearer", "expires_in": settings.access_token_expire_minutes * 60}


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    current_session_hash = getattr(request.state, "auth_session_hash", None)
    if not current_session_hash or not revoke_access_session(db, current_session_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token")
    append_audit_event(db, actor_id=user.id, action="auth.logout", object_type="user", object_id=user.id, details={"session_revoked": True})
    db.commit()
    return {"ok": True, "message": "This bearer session was revoked."}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return user_payload(user)
