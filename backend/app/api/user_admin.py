from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import append_audit_event
from app.db import get_db
from app.models import Role, User, Vendor
from app.schemas import UserCreateRequest, UserRoleUpdateRequest
from app.security import hash_password, require_roles

router = APIRouter(prefix="/admin/users", tags=["administration"])


def _validated_vendor_binding(
    db: Session,
    roles: list[str],
    requested_vendor_id: str | None,
    *,
    existing_vendor_id: str | None = None,
) -> str | None:
    if len(roles) != len(set(roles)):
        raise HTTPException(status_code=422, detail="Duplicate role names are not allowed")
    if "vendor" not in roles:
        if requested_vendor_id is not None:
            raise HTTPException(status_code=422, detail="vendor_id is only valid for a vendor-only account")
        return None
    if len(set(roles)) != 1:
        raise HTTPException(status_code=422, detail="Vendor accounts must have only the vendor role")
    vendor_id = requested_vendor_id or existing_vendor_id
    if not vendor_id:
        raise HTTPException(status_code=422, detail="A vendor_id is required for a vendor account")
    vendor = db.get(Vendor, vendor_id)
    if not vendor or not vendor.is_active:
        raise HTTPException(status_code=422, detail="The vendor account does not exist or is inactive")
    return vendor.id


@router.get("")
def list_users(db: Session = Depends(get_db), _admin: User = Depends(require_roles("admin"))):
    users = db.scalars(select(User).order_by(User.email)).all()
    return {"items": [{"id": item.id, "email": item.email, "display_name": item.display_name, "active": item.is_active, "roles": sorted(role.name for role in item.roles), "vendor_id": item.vendor_id, "created_at": item.created_at.isoformat()} for item in users]}


@router.post("", status_code=201)
def create_user(payload: UserCreateRequest, db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    password = payload.password
    if not (re.search(r"[a-z]", password) and re.search(r"[A-Z]", password) and re.search(r"\d", password) and re.search(r"[^A-Za-z0-9]", password)):
        raise HTTPException(status_code=422, detail="Password must include lower/upper case, a number, and a symbol")
    vendor_id = _validated_vendor_binding(db, payload.roles, payload.vendor_id)
    roles = list(db.scalars(select(Role).where(Role.name.in_(payload.roles))).all())
    if len(roles) != len(set(payload.roles)):
        raise HTTPException(status_code=422, detail="One or more role names are not configured")
    user = User(
        email=payload.email.strip().lower(), display_name=payload.display_name.strip(),
        password_hash=hash_password(password), roles=roles, vendor_id=vendor_id,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A user with that email already exists")
    append_audit_event(
        db, actor_id=admin.id, action="admin.user_created", object_type="user", object_id=user.id,
        details={"email": user.email, "roles": sorted(payload.roles), "vendor_id": vendor_id},
    )
    db.commit()
    return {"id": user.id, "email": user.email, "display_name": user.display_name, "active": user.is_active, "roles": sorted(role.name for role in user.roles), "vendor_id": user.vendor_id}


@router.put("/{user_id}/roles")
def update_user_roles(user_id: str, payload: UserRoleUpdateRequest, db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id and "admin" not in payload.roles:
        raise HTTPException(status_code=409, detail="You cannot remove your own administrator role")
    vendor_id = _validated_vendor_binding(
        db, payload.roles, payload.vendor_id, existing_vendor_id=user.vendor_id,
    )
    roles = list(db.scalars(select(Role).where(Role.name.in_(payload.roles))).all())
    if len(roles) != len(set(payload.roles)):
        raise HTTPException(status_code=422, detail="One or more role names are not configured")
    previous = sorted(role.name for role in user.roles)
    previous_vendor_id = user.vendor_id
    user.roles = roles
    user.vendor_id = vendor_id
    append_audit_event(
        db, actor_id=admin.id, action="admin.user_roles_updated", object_type="user", object_id=user.id,
        details={
            "previous_roles": previous, "new_roles": sorted(payload.roles),
            "previous_vendor_id": previous_vendor_id, "vendor_id": vendor_id,
        },
    )
    db.commit()
    return {"id": user.id, "roles": sorted(role.name for role in user.roles), "vendor_id": user.vendor_id}


@router.patch("/{user_id}/active")
def set_user_active(user_id: str, active: bool, db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id and not active:
        raise HTTPException(status_code=409, detail="You cannot deactivate your own active administrator account")
    prior = user.is_active
    user.is_active = active
    append_audit_event(db, actor_id=admin.id, action="admin.user_active_state_changed", object_type="user", object_id=user.id, details={"previous": prior, "active": active})
    db.commit()
    return {"id": user.id, "active": user.is_active}
