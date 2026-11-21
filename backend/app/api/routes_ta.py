from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, status

from app.services import db_service
from app.services.state_service import build_state


router = APIRouter(tags=["staff"])


def _state(slot_id: Optional[str] = None):
    snapshot = db_service.get_app_snapshot()
    return build_state(
        slots=snapshot["slots"],
        availability=snapshot["availability"],
        queue_entries=snapshot["queue"],
        current_by_slot=snapshot["currentBySlot"],
        served_by_slot=snapshot["servedBySlot"],
        requested_slot_id=slot_id,
        tas_active=snapshot["tasActive"],
        avg_help_minutes=snapshot["averageHelpMinutes"],
        forecast=snapshot["forecast"],
    )


def _require_section_ta(slot_id: Optional[str], token: Optional[str]):
    if not slot_id:
        raise HTTPException(status_code=400, detail="section id is required")
    user = db_service.user_for_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in.")
    section = db_service.find_slot(slot_id)
    if not section:
        raise HTTPException(status_code=404, detail="section not found")
    if not db_service.is_section_ta(user, slot_id):
        raise HTTPException(status_code=403, detail="Only course TAs can manage this queue.")
    return user, section


@router.get("/api/staff/queue")
def staff_queue(
    slot_id: Optional[str] = Query(default=None),
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
):
    _require_section_ta(slot_id, x_user_token)
    state = _state(slot_id=slot_id)
    return db_service.serialize(
        {
            "served_count": state["staff"]["servedCount"],
            "current_student": state["staff"]["currentStudent"],
            "current_student_id": state["staff"]["currentStudent"]["id"] if state["staff"]["currentStudent"] else None,
            "waiting_entries": [
                {
                    "id": entry["id"],
                    "name": entry.get("name", "Student"),
                    "course": entry.get("course", "General"),
                    "need": entry.get("need", "Office hours help"),
                    "message": entry.get("message", ""),
                    "ai": entry.get("ai", {}),
                    "file": entry.get("file"),
                    "position": entry["position"],
                    "estimated_help_minutes": entry.get("ai", {}).get("estimatedHelpMinutes", 0),
                }
                for entry in state["staff"]["waitingEntries"]
            ],
        }
    )


@router.get("/api/staff/queue/{entry_id}")
def staff_queue_entry(
    entry_id: str,
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
):
    entry = db_service.find_queue_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Queue entry not found.")
    _require_section_ta(entry.get("slotId"), x_user_token)
    return db_service.serialize(entry)


@router.post("/api/staff/call-next", status_code=status.HTTP_200_OK)
def call_next(
    slot_id: Optional[str] = Query(default=None),
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
):
    user, _ = _require_section_ta(slot_id, x_user_token)
    db_service.record_section_participation(slot_id, user["id"], "ta")

    current = db_service.get_current_student(slot_id)
    if current:
        db_service.increment_served_count(slot_id)

    next_entry = db_service.pop_next_waiting_entry(slot_id)
    if next_entry:
        next_entry["status"] = "called"
        next_entry["calledAt"] = datetime.now(timezone.utc)

    db_service.set_current_student(slot_id, next_entry)
    return db_service.serialize({"next": next_entry, "state": _state(slot_id=slot_id)})


@router.post("/api/staff/serve-current", status_code=status.HTTP_200_OK)
def serve_current(
    slot_id: Optional[str] = Query(default=None),
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
):
    user, _ = _require_section_ta(slot_id, x_user_token)
    db_service.record_section_participation(slot_id, user["id"], "ta")

    served = db_service.get_current_student(slot_id)
    if served:
        db_service.increment_served_count(slot_id)
        db_service.clear_current_student(slot_id)

    return db_service.serialize({"served": served, "state": _state(slot_id=slot_id)})
