"""Queue domain helpers delegating persistence to db_service."""

from app.services.db_service import (
    delete_queue_entry,
    find_active_entry_for_user,
    find_queue_entry,
    insert_queue_entry,
    list_queue_entries,
    list_waiting_entries,
    pop_next_waiting_entry,
    return_current_student_to_queue,
    update_queue_entry_ai,
)

__all__ = [
    "delete_queue_entry",
    "find_active_entry_for_user",
    "find_queue_entry",
    "insert_queue_entry",
    "list_queue_entries",
    "list_waiting_entries",
    "pop_next_waiting_entry",
    "return_current_student_to_queue",
    "update_queue_entry_ai",
]
