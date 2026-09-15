"""Admin-only user management: create, disable, reset password, delete."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.deps import require_admin
from src.database import MediaRequest, User, UserRole, get_db
from src.security import audit, client_ip, validate_password, validate_username

router = APIRouter(tags=["users"])


class CreateUserRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)
    role: str = "user"


class UpdateUserRequest(BaseModel):
    is_active: Optional[bool] = None
    role: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    password: str = Field(max_length=128)


def _user_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "is_active": bool(u.is_active),
        "locked": bool(u.locked_until and u.locked_until > datetime.utcnow()),
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def _validate_role(role: str) -> None:
    if role not in (UserRole.admin, UserRole.user):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")


@router.get("/users")
def list_users(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    return [_user_dict(u) for u in db.query(User).order_by(User.created_at).all()]


@router.post("/users")
def create_user(
    body: CreateUserRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    err = validate_username(body.username)
    if err:
        raise HTTPException(status_code=400, detail=err)
    _validate_role(body.role)
    err = validate_password(body.password, body.username, is_admin=body.role == UserRole.admin)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if db.query(User).filter_by(username=body.username).first():
        raise HTTPException(status_code=409, detail="Username already exists")

    user = User(
        username=body.username,
        password_hash=bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode(),
        role=body.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    audit(db, "user.created", username=admin.username,
          detail=f"{body.username} ({body.role})", ip=client_ip(request))
    return _user_dict(user)


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    body: UpdateUserRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot modify your own account here")

    changes = []
    if body.role is not None and body.role != user.role:
        _validate_role(body.role)
        user.role = body.role
        user.session_version += 1  # force re-login with new privileges
        changes.append(f"role={body.role}")
    if body.is_active is not None and body.is_active != bool(user.is_active):
        user.is_active = body.is_active
        if not body.is_active:
            user.session_version += 1  # kill their sessions immediately
        changes.append(f"active={body.is_active}")
    db.commit()
    if changes:
        audit(db, "user.updated", username=admin.username,
              detail=f"{user.username}: {', '.join(changes)}", ip=client_ip(request))
    return _user_dict(user)


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    body: ResetPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Use the Account page to change your own password")

    err = validate_password(body.password, user.username, is_admin=user.role == UserRole.admin)
    if err:
        raise HTTPException(status_code=400, detail=err)

    user.password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    user.password_changed_at = None  # allow env re-sync semantics only for admin seed
    user.session_version += 1
    user.failed_logins = 0
    user.locked_until = None
    db.commit()
    audit(db, "user.password_reset", username=admin.username,
          detail=user.username, ip=client_ip(request))
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    if user.username == admin.username and user.role == UserRole.admin:
        raise HTTPException(status_code=400, detail="Cannot delete the seeded admin account")

    # Keep their requests for history, but detach ownership
    db.query(MediaRequest).filter_by(user_id=user.id).update({"user_id": -1})
    db.delete(user)
    db.commit()
    audit(db, "user.deleted", username=admin.username, detail=user.username, ip=client_ip(request))
    return {"ok": True}
