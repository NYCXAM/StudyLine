from collections import defaultdict
from datetime import datetime, timezone
from time import monotonic
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.services import ai_service, db_service
from app.services.state_service import build_state


router = APIRouter(tags=["queue"])

QUEUE_RATE_WINDOW_SECONDS = 60.0
QUEUE_RATE_MAX_REQUESTS = 5
_queue_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _check_queue_rate_limit(actor_key: str) -> None:
    now = monotonic()
    bucket = [ts for ts in _queue_rate_buckets[actor_key] if now - ts < QUEUE_RATE_WINDOW_SECONDS]
    if len(bucket) >= QUEUE_RATE_MAX_REQUESTS:
        _queue_rate_buckets[actor_key] = bucket
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many queue join attempts. Please wait a minute before trying again.",
        )
    bucket.append(now)
    _queue_rate_buckets[actor_key] = bucket


def _refresh_entry_ai(
    entry_id: str,
    course: Optional[str],
    need: Optional[str],
    message: Optional[str],
    file: Optional[dict],
) -> None:
    ai = ai_service.analyze_question(course, need, message, file)
    db_service.update_queue_entry_ai(entry_id, ai)


def _resolve_student_entry_id(
    queue_token: Optional[str],
    user: Optional[dict],
    slot_id: Optional[str] = None,
) -> Optional[str]:
    if not isinstance(queue_token, str):
        queue_token = None
    if queue_token:
        entry = db_service.find_queue_entry(queue_token)
        if entry and (not slot_id or entry.get("slotId") == slot_id):
            return queue_token
    if user:
        active = db_service.find_active_entry_for_user(user.get("id"), slot_id=slot_id)
        if active:
            return active.get("id")
    return queue_token


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
        populate_by_name = True


def _state(
    student_id: Optional[str] = None,
    slot_id: Optional[str] = None,
    include_staff_details: bool = False,
    viewer_ta_id: Optional[str] = None,
):
    effective_slot_id = slot_id
    if not effective_slot_id and student_id:
        entry = db_service.find_queue_entry(student_id)
        if entry:
            effective_slot_id = entry.get("slotId")
    snapshot = db_service.get_app_snapshot(slot_id=effective_slot_id)
    state = build_state(
        slots=snapshot["slots"],
        availability=snapshot["availability"],
        queue_entries=snapshot["queue"],
        current_by_slot=snapshot["currentBySlot"],
        current_students_by_slot=snapshot.get("currentStudentsBySlot"),
        served_by_slot=snapshot["servedBySlot"],
        student_id=student_id,
        requested_slot_id=effective_slot_id,
        tas_active=snapshot["tasActive"],
        avg_help_minutes=snapshot["averageHelpMinutes"],
        forecast=snapshot["forecast"],
        viewer_ta_id=viewer_ta_id,
    )
    if not include_staff_details:
        state["staff"] = {
            "currentStudent": None,
            "currentStudents": [],
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
    student_entry_id = _resolve_student_entry_id(queue_token, user, slot_id=section_id)
    state = _state(
        student_id=student_entry_id,
        slot_id=section_id,
        include_staff_details=is_ta,
        viewer_ta_id=user["id"] if (is_ta and user) else None,
    )
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
    background_tasks: BackgroundTasks = BackgroundTasks(),
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
    queue_token: Optional[str] = Header(default=None, alias="X-Queue-Token"),
):
    if not isinstance(x_user_token, str):
        x_user_token = None
    if not isinstance(queue_token, str):
        queue_token = None
    slot_id = payload.slot_id or payload.slotId
    section = db_service.find_slot(slot_id)
    if not section:
        raise HTTPException(status_code=400, detail="Choose an available office-hour section before joining.")

    user = db_service.user_for_token(x_user_token)
    if user:
        existing_for_user = db_service.find_active_entry_for_user(user["id"], slot_id=slot_id)
        if existing_for_user:
            raise HTTPException(status_code=409, detail="You are already in line for this section.")
    elif queue_token:
        existing_by_token = db_service.find_queue_entry(queue_token)
        if existing_by_token and existing_by_token.get("slotId") == slot_id:
            raise HTTPException(status_code=409, detail="You are already in line for this section.")

    actor_key = (user or {}).get("id") or x_user_token or queue_token or f"anon-{slot_id}-{payload.name.strip().lower()}"
    _check_queue_rate_limit(actor_key)

    file_payload = payload.attachment or payload.file
    file = None
    if file_payload:
        file = {
            "name": file_payload.name.strip()[:120] or "Attached file",
            "type": file_payload.type.strip()[:80] or "unknown",
            "size": int(file_payload.size or 0),
        }

    message = payload.message.strip()[:1200]
    course_label = section.get("courseCode") or payload.course.strip()[:80] or "General"
    need_label = payload.need.strip()[:120] or "Office hours help"
    if background_tasks is not None and ai_service.has_openai_key():
        ai = ai_service.analyze_locally(course_label, need_label, message, file)
    else:
        ai = ai_service.analyze_question(course_label, need_label, message, file)
    entry = {
        "id": str(uuid4()),
        "slotId": slot_id,
        "studentUserId": user["id"] if user else None,
        "name": payload.name.strip()[:80] or (user.get("name") if user else "Student"),
        "course": course_label,
        "need": need_label,
        "message": message,
        "file": file,
        "ai": ai,
        "status": "waiting",
        "joinedAt": datetime.now(timezone.utc),
    }
    entry = db_service.insert_queue_entry(entry)
    if background_tasks is not None and ai_service.has_openai_key():
        background_tasks.add_task(_refresh_entry_ai, entry["id"], course_label, need_label, message, file)
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
    x_user_token: Optional[str] = Header(default=None, alias="X-User-Token"),
):
    user = db_service.user_for_token(x_user_token)
    resolved_token = _resolve_student_entry_id(queue_token, user, slot_id=slot_id)
    if not resolved_token:
        return {"status": "not_joined", "position": None, "personal_wait_minutes": None, "entry_id": None}
    state = _state(student_id=resolved_token, slot_id=slot_id)
    return {
        "status": state["queue"]["status"],
        "position": state["queue"]["position"],
        "personal_wait_minutes": state["queue"]["personalWaitMinutes"],
        "entry_id": resolved_token,
        "slot_id": state["selectedSlotId"],
    }


@router.delete("/api/queue/me", status_code=status.HTTP_200_OK)
def delete_queue_me(
    slot_id: Optional[str] = Query(default=None),
    queue_token: Optional[str] = Header(default=None, alias="X-Queue-Token"),
    x_user_token: Optional[str] = Header(default=None, alias="X-User-Token"),
):
    user = db_service.user_for_token(x_user_token)
    resolved_token = _resolve_student_entry_id(queue_token, user, slot_id=slot_id)
    if not resolved_token:
        return {"removed": False, "state": db_service.serialize(_state(slot_id=slot_id))}
    removed = db_service.delete_queue_entry(resolved_token)
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
