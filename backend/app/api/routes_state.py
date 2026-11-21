from typing import Optional

from fastapi import APIRouter, Header, Query

from app.services import db_service, state_service


router = APIRouter(tags=["state"])


@router.get("/api/state")
def get_state(
    student_id: Optional[str] = Query(default=None, alias="studentId"),
    slot_id: Optional[str] = Query(default=None, alias="slotId"),
    x_user_token: Optional[str] = Header(default=None, alias="X-User-Token"),
):
    snapshot = db_service.get_app_snapshot()
    state = state_service.build_state(
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
    user = db_service.user_for_token(x_user_token)
    if not db_service.is_section_ta(user, state.get("selectedSlotId")):
        state["staff"] = {
            "currentStudent": None,
            "waitingEntries": [],
            "servedCount": state["staff"]["servedCount"],
        }
    return db_service.serialize(state)
