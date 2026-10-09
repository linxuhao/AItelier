"""Driver registry REST surface (design/multi-driver-coop.md §3.4, phase P0).

Reads (whoami, list, get, project members) need the same private-read
credential as the rest of the private State surface; writes need an admin
(`api.authz.require_admin`): an off-tunnel `is_admin` driver such as
`owner-cli`, or the owner's allowlisted Access email. While the feature is off
(`AITELIER_DRIVER_IDENTITY` unset or no pepper secret) every route except
`/me` answers 404 and nothing is created.

A token appears in exactly two responses - register and rotate - once.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api import authz
from core import drivers

router = APIRouter(prefix="/api/drivers", tags=["Drivers"])


def _registry():
    registry = authz.driver_registry()
    if registry is None:
        raise HTTPException(404, "driver identity is not enabled on this deployment")
    return registry


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except drivers.DriverConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except drivers.DriverError as exc:
        raise HTTPException(422, str(exc)) from exc


def _actor(request: Request) -> str:
    return authz.request_actor(request)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterDriver(_Strict):
    driver_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    host_label: str = Field(default="", max_length=200)
    capabilities: dict = Field(default_factory=dict)
    is_admin: bool = False


class Rotate(_Strict):
    expected_revision: int


class SetStatus(_Strict):
    status: str
    expected_revision: int
    reason: str = Field(min_length=1, max_length=2000)


class SetAdmin(_Strict):
    is_admin: bool
    expected_revision: int
    reason: str = Field(min_length=1, max_length=2000)


class SetMembership(_Strict):
    status: str
    expected_revision: int
    reason: str = Field(min_length=1, max_length=2000)


@router.get("/me", dependencies=[Depends(authz.require_reader)])
def whoami(request: Request):
    """Who the server thinks this credential is, plus memberships and capabilities."""
    identity = authz.request_identity(request)
    registry = authz.driver_registry()
    out = {"enabled": registry is not None,
           "kind": identity.kind if identity else None,
           "actor": identity.actor if identity else None,
           "driver_id": identity.driver_id if identity else None,
           "is_admin": bool(identity and identity.is_admin),
           "email": identity.email if identity else None}
    if registry is not None and identity is not None and identity.driver_id:
        out["driver"] = _call(registry.get, identity.driver_id)
    return out


@router.get("", dependencies=[Depends(authz.require_reader)])
def list_drivers():
    return {"drivers": _registry().list()}


@router.get("/projects/{project_id}", dependencies=[Depends(authz.require_reader)])
def project_drivers(project_id: str):
    return {"project_id": project_id, "members": _registry().project_members(project_id)}


@router.get("/{driver_id}", dependencies=[Depends(authz.require_reader)])
def get_driver(driver_id: str):
    registry = _registry()
    try:
        return registry.get(driver_id)
    except drivers.DriverError as exc:
        # A missing resource is the self-register client's pre-create branch.
        raise HTTPException(404, str(exc)) from exc


@router.get("/{driver_id}/audit", dependencies=[Depends(authz.require_admin)])
def driver_audit(driver_id: str, limit: int = 100):
    return {"driver_id": driver_id, "audit": _registry().audit(driver_id, limit)}


@router.post("", dependencies=[Depends(authz.require_admin)])
def register_driver(body: RegisterDriver, request: Request):
    return _call(_registry().register, body.driver_id, body.display_name, host_label=body.host_label,
                 capabilities=body.capabilities, is_admin=body.is_admin, actor=_actor(request))


@router.post("/{driver_id}/rotate", dependencies=[Depends(authz.require_admin)])
def rotate_driver_token(driver_id: str, body: Rotate, request: Request):
    return _call(_registry().rotate, driver_id, body.expected_revision, actor=_actor(request))


@router.post("/{driver_id}/status", dependencies=[Depends(authz.require_admin)])
def set_driver_status(driver_id: str, body: SetStatus, request: Request):
    return _call(_registry().set_status, driver_id, body.status, body.expected_revision, body.reason,
                 actor=_actor(request))


@router.post("/{driver_id}/admin", dependencies=[Depends(authz.require_admin)])
def set_driver_admin(driver_id: str, body: SetAdmin, request: Request):
    return _call(_registry().set_admin, driver_id, body.is_admin, body.expected_revision, body.reason,
                 actor=_actor(request))


@router.put("/{driver_id}/projects/{project_id}", dependencies=[Depends(authz.require_admin)])
def set_project_driver(driver_id: str, project_id: str, body: SetMembership, request: Request):
    registry = _registry()

    def project_exists(pid: str) -> bool:
        with registry.db.get_connection() as conn:
            try:
                return conn.execute("SELECT 1 FROM state_projects WHERE project_id=?",
                                    (pid,)).fetchone() is not None
            except sqlite3.Error:
                return False
    return _call(registry.set_membership, project_id, driver_id, body.status, body.expected_revision,
                 body.reason, actor=_actor(request), project_exists=project_exists)
