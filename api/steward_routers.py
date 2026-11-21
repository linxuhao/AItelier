"""The owner's decisions on the steward's proposals, and the owner's memory requests
(plugin steps 13-14, design-steward 3.4 and 5).

Approve / decline and the memory requests are the OWNER's: an `owner:` session that
is no driver (core.state_steward.StateSteward.is_owner_session), on top of the
writer verdict. A steward token never reaches them: they are not on its write
whitelist (api/authz._steward_denial). The impact read is the owner's or the
steward's. Every answer is a private record: none is served to an anonymous caller.

Scheduled reminders (step 16, design-steward 7.4): the owner creates, edits,
deletes and previews them here; the list is the owner's or the steward's. The
steward itself writes them through its State commands.

Group reminders (step 25 C11, design-steward 7.9.4) are schedules with target family_group;
the family lists (step 25 C4, 7.9.5) are the owner's here too: view, add, check, remove and
clear the done items (owner session only).

Chat channels (step 19, design-steward 7.2): the owner issues a binding code, unbinds,
picks the default channel and asks for a new WeChat login QR here (owner session only);
the binding list and the messages' sources are the owner's (the steward reads them
through its State queries).
"""
import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api import authz
from api.dsh_driver_routers import manager
from api.state_graph_routers import get_service
from core.state_graph import StateConflict, StateGraphError, StateNotFound
from core.state_steward import (ARCHIVE_KINDS, ProposalConflict, ScheduleConflict, ScheduleInvalid,
                                impact_is_empty, preview_schedule, stop_impact)

router = APIRouter(prefix="/api/steward", tags=["Steward"])


class Approve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    confirm_interrupt: bool = False


class Decline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=2000)


class MemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=40)
    payload: dict = Field(default_factory=dict)


def _owner(service):
    if not service.steward.is_owner_session():
        raise HTTPException(403, "owner_session_required")


def _conflict(exc: ProposalConflict):
    # The web card shows the CURRENT row a 409 carries (web/src/lib/api.ts).
    return JSONResponse({"detail": exc.row}, status_code=409)


@router.post("/proposals/{proposal_id}/approve", dependencies=[Depends(authz.require_writer)])
async def approve(proposal_id: str, body: Approve, service=Depends(get_service), runtime=Depends(manager)):
    """Approve exactly the version the owner saw; execute its frozen payload once."""
    _owner(service)
    steward = service.steward
    try:
        row = await asyncio.to_thread(steward.get_proposal, proposal_id)
        impact = None
        if row["kind"] == "stop_driver" and row["status"] == "open" and row["version"] == body.expected_version:
            # Checked again now: refused (row stays open) unless the owner confirmed.
            impact = await asyncio.to_thread(_impact_now, service, row["payload"]["project_id"])
            if not impact_is_empty(impact) and not body.confirm_interrupt:
                return JSONResponse({"detail": {"code": "confirm_interrupt_required", "impact": impact,
                                                "proposal": row}}, status_code=422)
        row = await asyncio.to_thread(steward.begin_approval, proposal_id, body.expected_version)
    except ProposalConflict as exc:
        return _conflict(exc)
    except StateNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StateGraphError as exc:
        raise HTTPException(403 if str(exc) == "owner_session_required" else 422, str(exc)) from exc
    if row["kind"] == "create_project":
        return await asyncio.to_thread(steward.execute_create_project, proposal_id)
    if row["kind"] in ARCHIVE_KINDS:
        return await asyncio.to_thread(steward.execute_archive, proposal_id)
    if row["kind"] == "stop_driver":
        await asyncio.to_thread(steward.record_confirmation, proposal_id, body.confirm_interrupt, impact)
    target = row["payload"]["project_id"]
    try:
        if row["kind"] == "start_driver":
            # The entry POST /api/dsh-drivers/{project_id}/start uses; the model is the role's route.
            driver = await runtime.start(target, provider="aitelier", model=None, actor=service.actor)
        else:
            driver = await runtime.stop(target)
    except Exception as exc:  # noqa: BLE001 - any failure is the proposal's result
        return await asyncio.to_thread(steward.fail, proposal_id, f"{type(exc).__name__}: {exc}")
    return await asyncio.to_thread(steward.finish, proposal_id, "done", {"driver": driver})


def _impact_now(service, project_id):
    with service.store.transaction() as conn:
        return stop_impact(conn, project_id)


@router.post("/proposals/{proposal_id}/decline", dependencies=[Depends(authz.require_writer)])
async def decline(proposal_id: str, body: Decline, service=Depends(get_service)):
    _owner(service)
    try:
        return await asyncio.to_thread(service.steward.decline, proposal_id, body.expected_version, body.reason)
    except ProposalConflict as exc:
        return _conflict(exc)
    except StateNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/proposals/{proposal_id}/impact", dependencies=[Depends(authz.require_reader)])
def impact(proposal_id: str, service=Depends(get_service)):
    """What stopping the proposal's target driver would interrupt now (the owner or the steward)."""
    if not (service.steward.is_owner_session() or service.steward.is_steward()):
        raise HTTPException(403, "owner_or_steward_required")
    try:
        return service.steward.impact(proposal_id)
    except StateNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/memory-requests", dependencies=[Depends(authz.require_writer)])
def create_memory_request(body: MemoryRequest, service=Depends(get_service)):
    _owner(service)
    try:
        return service.steward.create_memory_request(body.kind, body.payload)
    except StateGraphError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/memory-requests/{request_id}", dependencies=[Depends(authz.require_reader)])
def read_memory_request(request_id: str, service=Depends(get_service)):
    _owner(service)
    try:
        return service.steward.read_memory_request(request_id)
    except StateNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


# --- scheduled reminders (step 16) ---------------------------------------------------------

class ScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    instruction: str = Field(min_length=1, max_length=2000)
    schedule: dict
    tz: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    # Step 25 C11 (design-steward 7.9.4): 「发到：我 / 家庭群」; channel names the group when both
    # channels have one.
    target: Literal["owner", "family_group"] = "owner"
    channel: Literal["slack", "wechat"] | None = None


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    instruction: str | None = Field(default=None, min_length=1, max_length=2000)
    schedule: dict | None = None
    tz: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    target: Literal["owner", "family_group"] | None = None
    channel: Literal["slack", "wechat"] | None = None


class SchedulePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule: dict
    tz: str = Field(min_length=1, max_length=64)


def _schedule_call(call, *args, **kwargs):
    """One owner write; a refusal keeps its code (and a conflict the current row)."""
    try:
        return call(*args, **kwargs)
    except ScheduleConflict as exc:
        return JSONResponse({"detail": {"code": "version_conflict", "schedule": exc.row}}, status_code=409)
    except ScheduleInvalid as exc:
        return JSONResponse({"detail": {"code": exc.code, "reason": exc.reason}}, status_code=422)
    except StateNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StateGraphError as exc:
        raise HTTPException(403 if str(exc) == "owner_session_required" else 422, str(exc)) from exc


@router.get("/schedules", dependencies=[Depends(authz.require_reader)])
def list_schedules(service=Depends(get_service)):
    """Every live schedule (the owner or the steward)."""
    if not (service.steward.is_owner_session() or service.steward.is_steward()):
        raise HTTPException(403, "owner_or_steward_required")
    return service.steward.list_schedules()


@router.post("/schedules", dependencies=[Depends(authz.require_writer)])
def create_schedule(body: ScheduleCreate, service=Depends(get_service)):
    _owner(service)
    return _schedule_call(service.steward.create_schedule, body.title, body.instruction, body.schedule,
                          tz=body.tz, enabled=body.enabled, as_owner=True, target=body.target, channel=body.channel)


@router.post("/schedules/preview", dependencies=[Depends(authz.require_writer)])
def preview(body: SchedulePreview, service=Depends(get_service)):
    """The next runs the schedule would have if saved now, in its zone."""
    _owner(service)
    return _schedule_call(preview_schedule, body.schedule, body.tz)


@router.put("/schedules/{schedule_id}", dependencies=[Depends(authz.require_writer)])
def update_schedule(schedule_id: str, body: ScheduleUpdate, service=Depends(get_service)):
    _owner(service)
    return _schedule_call(service.steward.update_schedule, schedule_id, body.expected_version, title=body.title,
                          instruction=body.instruction, schedule=body.schedule, tz=body.tz, enabled=body.enabled,
                          as_owner=True, target=body.target, channel=body.channel)


@router.delete("/schedules/{schedule_id}", dependencies=[Depends(authz.require_writer)])
def delete_schedule(schedule_id: str, expected_version: int, service=Depends(get_service)):
    _owner(service)
    out = _schedule_call(service.steward.delete_schedule, schedule_id, expected_version, as_owner=True)
    return {"deleted": True} if isinstance(out, dict) else out


# --- chat channels (step 19) ---------------------------------------------------------------

Channel = Literal["slack", "wechat"]


class DefaultChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Channel | None


@router.get("/channels", dependencies=[Depends(authz.require_reader)])
def list_channels(service=Depends(get_service)):
    """Both channels, bound or not, and the default one; each with the status the
    steward's host last reported ({connected, detail, at}, or None) and, while the WeChat
    login needs a scan, its QR as an SVG (`qr_svg`, else None)."""
    _owner(service)
    return service.channels.list_owner_channels(with_status=True)


@router.put("/channels/default", dependencies=[Depends(authz.require_writer)])
def set_default_channel(body: DefaultChannel, service=Depends(get_service)):
    _owner(service)
    return service.channels.set_default(body.channel)


@router.post("/channels/{channel}/code", dependencies=[Depends(authz.require_writer)])
def issue_code(channel: Channel, service=Depends(get_service)):
    """A 6-digit code, valid 10 minutes, once: the owner sends it to the bot from the
    account to bind. The plaintext is in this answer only (the backend keeps a digest
    keyed with the driver pepper; without that secret no code is issued: 503)."""
    _owner(service)
    try:
        return service.channels.issue_code(channel)
    except StateGraphError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/channels/{channel}/qr", dependencies=[Depends(authz.require_writer)])
def request_login_qr(channel: Literal["wechat"], service=Depends(get_service)):
    """A new WeChat login QR: one pending request the steward's host runs in code (it asks
    the bridge); the QR then appears in GET /channels like any other, never in this answer.
    A request still pending is answered again rather than doubled. Each channel's newest
    request (its status: pending, done with the bridge's answer, or expired when no host
    ran it within 2 minutes) is in GET /channels as `login_request`."""
    _owner(service)
    try:
        return service.channels.request_login(channel)
    except StateGraphError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/channels/{channel}", dependencies=[Depends(authz.require_writer)])
def unbind_channel(channel: Channel, service=Depends(get_service)):
    _owner(service)
    return service.channels.unbind(channel)


@router.get("/message-sources", dependencies=[Depends(authz.require_reader)])
def message_sources(ids: str = "", service=Depends(get_service)):
    """Which channel each of these messages came from or went to; an absent id came
    from (or stayed on) the dashboard."""
    _owner(service)
    message_ids = [i for i in ids.split(",") if i]
    try:
        return service.channels.message_sources(message_ids)
    except StateGraphError as exc:
        raise HTTPException(422, str(exc)) from exc


# --- the family group (step 23, design-steward 7.7) -----------------------------------------
# The owner's session only, like the channel routes: issue a group code, unbind (deleting the
# group's records), clear the records, name a member the bridge reported, write the family
# note, delete family memory, and - only on the pick-a-group path - pick a pending group.


class GroupCode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=40)


class MemberName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=20)


class FamilyNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=1000)


class PendingPick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Channel
    room_ref: str = Field(min_length=1, max_length=200)


def _group_call(call, *args):
    try:
        return call(*args)
    except StateNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except StateGraphError as exc:
        raise HTTPException(503 if str(exc).startswith("channel_secret_missing") else 422, str(exc)) from exc


@router.get("/groups", dependencies=[Depends(authz.require_reader)])
def list_groups(channel: Channel | None = None, service=Depends(get_service)):
    """The bound groups (names, members, counts); with `channel`, that group's records
    (withheld replies with their original and reason), family note and family memory."""
    _owner(service)
    return _group_call(service.groups.owner_view, channel)


@router.post("/groups/{channel}/code", dependencies=[Depends(authz.require_writer)])
def issue_group_code(channel: Channel, body: GroupCode | None = None, service=Depends(get_service)):
    """A group code G-XXXX-XXXX, valid 10 minutes, once: the owner posts '@<the bot> code' in
    the group from the bound account."""
    _owner(service)
    return _group_call(service.groups.issue_group_code, channel, body.label if body else None)


@router.delete("/groups/{channel}", dependencies=[Depends(authz.require_writer)])
def unbind_group(channel: Channel, service=Depends(get_service)):
    _owner(service)
    return _group_call(service.groups.unbind_owner_group, channel)


@router.post("/groups/{channel}/clear", dependencies=[Depends(authz.require_writer)])
def clear_group(channel: Channel, service=Depends(get_service)):
    _owner(service)
    return _group_call(service.groups.clear_group_messages, channel)


@router.put("/groups/{channel}/members/{sender_ref}", dependencies=[Depends(authz.require_writer)])
def name_member(channel: Channel, sender_ref: str, body: MemberName, service=Depends(get_service)):
    _owner(service)
    return _group_call(service.groups.name_group_member, channel, sender_ref, body.label)


@router.put("/groups/{channel}/family-note", dependencies=[Depends(authz.require_writer)])
def set_family_note(channel: Channel, body: FamilyNote, service=Depends(get_service)):
    _owner(service)
    return _group_call(service.groups.set_family_note, channel, body.text)


@router.delete("/groups/{channel}/family-memory", dependencies=[Depends(authz.require_writer)])
def delete_family_memory(channel: Channel, entry_id: str | None = None, service=Depends(get_service)):
    _owner(service)
    return _group_call(service.groups.delete_family_memory, channel, entry_id)


@router.get("/groups-pending", dependencies=[Depends(authz.require_reader)])
def pending_groups(service=Depends(get_service)):
    _owner(service)
    return _group_call(service.groups.list_pending_groups)


@router.post("/groups-pending/pick", dependencies=[Depends(authz.require_writer)])
def pick_pending_group(body: PendingPick, service=Depends(get_service)):
    _owner(service)
    return _group_call(service.groups.pick_pending_group, body.channel, body.room_ref)


# --- the family lists (step 25 C4, design-steward 7.9.5) -----------------------------------
# The owner's session only. Adding passes the family group's exit check (422 withheld: <kind>).

ListName = Literal["shopping", "todo"]


class ListAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[str] = Field(min_length=1, max_length=10)


class ListCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[str] = Field(min_length=1, max_length=50)
    done: bool = True


@router.get("/lists", dependencies=[Depends(authz.require_reader)])
def family_lists(service=Depends(get_service)):
    """Both family lists: each item with who added it, when, and who checked it."""
    _owner(service)
    return _group_call(lambda: service.lists.list_items(as_owner=True))


@router.post("/lists/{list_name}/items", dependencies=[Depends(authz.require_writer)])
def add_list_items(list_name: ListName, body: ListAdd, service=Depends(get_service)):
    _owner(service)
    return _group_call(lambda: service.lists.add_items(list_name, body.items, as_owner=True))


@router.post("/lists/check", dependencies=[Depends(authz.require_writer)])
def check_list_items(body: ListCheck, service=Depends(get_service)):
    _owner(service)
    return _group_call(lambda: service.lists.check_items(body.item_ids, body.done, as_owner=True))


@router.delete("/lists/items/{item_id}", dependencies=[Depends(authz.require_writer)])
def remove_list_item(item_id: str, service=Depends(get_service)):
    _owner(service)
    return _group_call(lambda: service.lists.remove_items([item_id], as_owner=True))


@router.post("/lists/clear-done", dependencies=[Depends(authz.require_writer)])
def clear_done_list_items(list_name: ListName | None = None, service=Depends(get_service)):
    _owner(service)
    return _group_call(lambda: service.lists.clear_done(list_name))
