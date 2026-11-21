from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.services import ai_service, db_service
from app.services.state_service import build_state


router = APIRouter(tags=["queue"])


class AttachmentPayload(BaseModel):
    name: str = Field(default="Attached file", max_length=120)
    type: str = Field(default="unknown", max_length=80)
    size: int = 0


class CreateQueueEntryRequest(BaseModel):
    name: str = Field(default="Student", max_length=80)
    course: str = Field(default="General", max_length=80)
    need: str = Field(default="Office hours help", max_length=240)
    message: str = Field(default="", max_length=1200)
    slot_id: str | None = Field(default=None, alias="slot_id")
    slotId: str | None = None
    attachment: Optional[AttachmentPayload] = None
    file: Optional[AttachmentPayload] = None

    class Config:
        allow_population_by_field_name = True


def _state(
    student_id: Optional[str] = None,
    slot_id: Optional[str] = None,
    include_staff_details: bool = False,
):
    snapshot = db_service.get_app_snapshot()
    state = build_state(
        slots=snapshot["slots"],
        availability=snapshot["availability"],
        queue_entries=snapshot["queue"],
        current_by_slot=snapshot["currentBySlot"],
        served_by_slot=snapshot["servedBySlot"],
        student_id=student_id,
        requested_slot_id=slot_id,
        tas_active=snapshot["tasActive"],
        avg_help_minutes=snapshot["averageHelpMinutes"],
        forecast=snapshot["forecast"],
    )
    if not include_staff_details:
        state["staff"] = {
            "currentStudent": None,
            "waitingEntries": [],
            "servedCount": state["staff"]["servedCount"],
        }
    return state


def _session_token_for(entry_id: str) -> str:
    return f"queue-session-{entry_id}"


@router.get("/api/sections/{section_id}/state")
def section_state(
    section_id: str,
    queue_token: Optional[str] = Header(default=None, alias="X-Queue-Token"),
    x_user_token: Optional[str] = Header(default=None, alias="X-User-Token"),
):
    context = db_service.section_context(section_id)
    if not context:
        raise HTTPException(status_code=404, detail="section not found")
    user = db_service.user_for_token(x_user_token)
    is_ta = db_service.is_section_ta(user, section_id)
    state = _state(student_id=queue_token, slot_id=section_id, include_staff_details=is_ta)
    return db_service.serialize(
        {
            "section": db_service.sanitize_share_codes(context["section"], is_ta),
            "course": db_service.sanitize_share_codes(context["course"], is_ta),
            "state": state,
        }
    )


@router.post("/api/queue", status_code=status.HTTP_201_CREATED)
def create_queue_entry(
    payload: CreateQueueEntryRequest,
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
):
    slot_id = payload.slot_id or payload.slotId
    section = db_service.find_slot(slot_id)
    if not section:
        raise HTTPException(status_code=400, detail="Choose an available office-hour section before joining.")

    user = db_service.user_for_token(x_user_token)
    file_payload = payload.attachment or payload.file
    file = None
    if file_payload:
        file = {
            "name": file_payload.name.strip()[:120] or "Attached file",
            "type": file_payload.type.strip()[:80] or "unknown",
            "size": int(file_payload.size or 0),
        }

    message = payload.message.strip()[:1200]
    ai = ai_service.analyze_question(payload.course, payload.need, message, file)
    entry = {
        "id": str(uuid4()),
        "slotId": slot_id,
        "studentUserId": user["id"] if user else None,
        "name": payload.name.strip()[:80] or (user.get("name") if user else "Student"),
        "course": section.get("courseCode") or payload.course.strip()[:80] or "General",
        "need": payload.need.strip()[:120] or "Office hours help",
        "message": message,
        "file": file,
        "ai": ai,
        "status": "waiting",
        "joinedAt": datetime.now(timezone.utc),
    }
    entry = db_service.insert_queue_entry(entry)
    session_token = _session_token_for(entry["id"])
    return {
        "entry": db_service.serialize(entry),
        "queueToken": entry["id"],
        "sessionToken": session_token,
        "state": db_service.serialize(_state(student_id=entry["id"], slot_id=slot_id)),
    }


@router.get("/api/queue/me")
def queue_me(
    slot_id: Optional[str] = Query(default=None),
    queue_token: Optional[str] = Header(default=None, alias="X-Queue-Token"),
):
    if not queue_token:
        return {"status": "not_joined", "position": None, "personal_wait_minutes": None, "entry_id": None}
    state = _state(student_id=queue_token, slot_id=slot_id)
    return {
        "status": state["queue"]["status"],
        "position": state["queue"]["position"],
        "personal_wait_minutes": state["queue"]["personalWaitMinutes"],
        "entry_id": queue_token,
        "slot_id": state["selectedSlotId"],
    }


@router.delete("/api/queue/me", status_code=status.HTTP_200_OK)
def delete_queue_me(
    slot_id: Optional[str] = Query(default=None),
    queue_token: Optional[str] = Header(default=None, alias="X-Queue-Token"),
):
    if not queue_token:
        return {"removed": False, "state": db_service.serialize(_state(slot_id=slot_id))}
    removed = db_service.delete_queue_entry(queue_token)
    return {"removed": removed, "state": db_service.serialize(_state(slot_id=slot_id))}


@router.delete("/api/queue/{entry_id}", status_code=status.HTTP_200_OK)
def delete_queue_entry(
    entry_id: str,
    slot_id: Optional[str] = Query(default=None),
    queue_token: Optional[str] = Header(default=None, alias="X-Queue-Token"),
    x_user_token: Optional[str] = Header(default=None, alias="X-User-Token"),
):
    entry = db_service.find_queue_entry(entry_id)
    user = db_service.user_for_token(x_user_token)
    target_slot_id = (entry or {}).get("slotId") or slot_id
    is_ta = db_service.is_section_ta(user, target_slot_id)
    if entry:
        is_own_token = bool(queue_token and queue_token == entry_id)
        is_own_user = bool(user and entry.get("studentUserId") and user["id"] == entry.get("studentUserId"))
        if not (is_own_token or is_own_user or is_ta):
            raise HTTPException(status_code=403, detail="Not authorized to remove this queue entry.")
    elif not (queue_token == entry_id or is_ta):
        raise HTTPException(status_code=403, detail="Not authorized to remove this queue entry.")
    removed = db_service.delete_queue_entry(entry_id)
    return {"removed": removed, "state": db_service.serialize(_state(slot_id=slot_id, include_staff_details=is_ta))}
