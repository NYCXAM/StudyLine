"""MongoDB persistence helpers for StudyLine."""

import os
import secrets
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError

from app.services.scheduling_service import create_slot


PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")

try:
    import certifi
except ImportError:
    certifi = None


MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "studyline_dev")
RETENTION_DAYS = int(os.getenv("DB_RETENTION_DAYS", "30"))

client_kwargs: dict[str, Any] = {"serverSelectionTimeoutMS": 5000}
if certifi is not None and MONGO_URL.startswith("mongodb+srv://"):
    client_kwargs["tlsCAFile"] = certifi.where()

try:
    client = MongoClient(MONGO_URL, **client_kwargs)
except PyMongoError:
    client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=5000, connect=False)
db = client[MONGO_DB_NAME]

users_collection = db["users"]
sessions_collection = db["auth_sessions"]
courses_collection = db["courses"]
sections_collection = db["sections"]
queue_collection = db["queue"]
slot_status_collection = db["slot_status"]
settings_collection = db["settings"]

DEFAULT_SETTINGS = {"tasActive": 2, "averageHelpMinutes": 7}


def initialize_database() -> None:
    client.admin.command("ping")
    users_collection.create_index("email", unique=True)
    sessions_collection.create_index("token", unique=True)
    sessions_collection.create_index("expiresAt", expireAfterSeconds=0)
    courses_collection.create_index("id", unique=True)
    courses_collection.create_index("code", unique=True)
    courses_collection.create_index("studentShareCode", unique=True)
    courses_collection.create_index("taShareCode", unique=True)
    courses_collection.create_index("taIds")
    courses_collection.create_index("studentIds")
    sections_collection.create_index("id", unique=True)
    sections_collection.create_index("courseId")
    sections_collection.create_index("studentShareCode", unique=True)
    sections_collection.create_index("taShareCode", unique=True)
    sections_collection.create_index([("date", 1), ("startTime", 1)])
    sections_collection.create_index("expiresAt", expireAfterSeconds=0)
    queue_collection.create_index("id", unique=True)
    queue_collection.create_index([("slotId", 1), ("status", 1), ("joinedAt", 1)])
    queue_collection.create_index("expiresAt", expireAfterSeconds=0)
    slot_status_collection.create_index("slotId", unique=True)
    settings_collection.update_one({"_id": "app"}, {"$setOnInsert": DEFAULT_SETTINGS}, upsert=True)
    if os.getenv("SEED_DEMO_DATA", "").strip().lower() in {"1", "true", "yes"}:
        _seed_if_empty()


def ping_database() -> bool:
    client.admin.command("ping")
    return True


def login_user(email: str, name: str, role: str, google_sub: str | None = None) -> dict[str, Any]:
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        raise ValueError("email is required")

    now = utc_now()
    user_id = f"user-{normalized_email}"
    user_values = {"email": normalized_email, "updatedAt": now}
    if google_sub:
        user_values["googleSub"] = google_sub

    users_collection.update_one(
        {"email": normalized_email},
        {
            "$set": user_values,
            "$setOnInsert": {
                "id": user_id,
                "name": name.strip() or normalized_email.split("@")[0],
                "createdAt": now,
                "taCourseIds": [],
                "studentCourseIds": [],
            },
            "$addToSet": {"roles": role},
        },
        upsert=True,
    )
    user = users_collection.find_one({"email": normalized_email}, {"_id": 0})
    token = secrets.token_urlsafe(32)
    sessions_collection.insert_one(
        {
            "token": token,
            "userId": user["id"],
            "role": role,
            "createdAt": now,
            "expiresAt": now + timedelta(days=14),
        }
    )
    return {"token": token, "user": user}


def user_for_token(token: str | None) -> Optional[dict[str, Any]]:
    if not token or not isinstance(token, str):
        return None
    session = sessions_collection.find_one({"token": token}, {"_id": 0})
    if not session:
        return None
    expires_at = session.get("expiresAt")
    if isinstance(expires_at, datetime):
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= utc_now():
            sessions_collection.delete_one({"token": token})
            return None
    return users_collection.find_one({"id": session["userId"]}, {"_id": 0})


def logout_user(token: str | None) -> bool:
    if not token:
        return False
    return sessions_collection.delete_one({"token": token}).deleted_count > 0


def update_user_profile(user_id: str, name: str, preferences: dict[str, Any]) -> dict[str, Any]:
    """Update the editable profile fields while preserving account identity."""
    cleaned_name = " ".join((name or "").strip().split())
    if not 2 <= len(cleaned_name) <= 80:
        raise ValueError("username must be between 2 and 80 characters")
    allowed = {"emailNotifications", "compactMode"}
    clean_preferences = {key: bool(value) for key, value in preferences.items() if key in allowed}
    now = utc_now()
    update: dict[str, Any] = {"name": cleaned_name, "updatedAt": now}
    if clean_preferences:
        update["preferences"] = clean_preferences
    updated = users_collection.find_one_and_update(
        {"id": user_id}, {"$set": update}, return_document=ReturnDocument.AFTER, projection={"_id": 0}
    )
    if not updated:
        raise ValueError("user account was not found")
    return updated


def create_course(user: dict[str, Any], code: str, title: str | None = None) -> dict[str, Any]:
    course_code = (code or "").strip().upper()
    if not course_code:
        raise ValueError("course code is required")

    existing = courses_collection.find_one({"code": course_code}, {"_id": 0})
    if existing:
        if user["id"] in (existing.get("taIds") or []):
            return course_by_id(existing["id"])
        raise PermissionError("A course with this code already exists. Use its TA share code to join.")

    course = {
        "id": f"course-{uuid4()}",
        "code": course_code,
        "title": (title or course_code).strip(),
        "taIds": [user["id"]],
        "studentIds": [],
        "createdBy": user["id"],
        "createdAt": utc_now(),
        "studentShareCode": _share_code("S-C"),
        "taShareCode": _share_code("T-C"),
    }
    courses_collection.insert_one(deepcopy(course))
    _add_course_to_user(user["id"], course["id"], "ta")
    return course


def course_by_id(course_id: str) -> Optional[dict[str, Any]]:
    return courses_collection.find_one({"id": course_id}, {"_id": 0})


def is_course_ta(user: Optional[dict[str, Any]], course_id: Optional[str]) -> bool:
    if not user or not course_id:
        return False
    course = course_by_id(course_id)
    return bool(course and user["id"] in (course.get("taIds") or []))


def is_section_ta(user: Optional[dict[str, Any]], section_id: Optional[str]) -> bool:
    if not user or not section_id:
        return False
    section = section_by_id(section_id)
    if not section:
        return False
    if user["id"] in (section.get("taIds") or []) or user["id"] in (section.get("participantTaIds") or []):
        return True
    return is_course_ta(user, section.get("courseId"))


def sanitize_share_codes(document: Optional[dict[str, Any]], is_ta: bool) -> Optional[dict[str, Any]]:
    if document is None or is_ta:
        return document
    cleaned = dict(document)
    cleaned.pop("taShareCode", None)
    cleaned.pop("studentShareCode", None)
    return cleaned


def update_course(course_id: str, values: dict[str, Any]) -> Optional[dict[str, Any]]:
    updates = {}
    if values.get("code"):
        next_code = values["code"].strip().upper()
        conflict = courses_collection.find_one({"code": next_code, "id": {"$ne": course_id}}, {"_id": 0})
        if conflict:
            raise ValueError("A course with this code already exists.")
        updates["code"] = next_code
    if values.get("title"):
        updates["title"] = values["title"].strip()
    if not updates:
        return course_by_id(course_id)
    courses_collection.update_one({"id": course_id}, {"$set": updates})
    if "code" in updates:
        sections_collection.update_many({"courseId": course_id}, {"$set": {"courseCode": updates["code"]}})
    return course_by_id(course_id)


def list_courses_for_user(
    user: dict[str, Any],
    role: str,
    local_date: Optional[str] = None,
) -> list[dict[str, Any]]:
    membership_field = "taIds" if role == "ta" else "studentIds"
    courses = list(courses_collection.find({membership_field: user["id"]}, {"_id": 0}).sort([("code", 1)]))
    return [_course_with_sections(course, role, user["id"], local_date=local_date) for course in courses]


def add_user_to_course(user: dict[str, Any], course_id: str, role: str) -> None:
    course_field = "taIds" if role == "ta" else "studentIds"
    courses_collection.update_one({"id": course_id}, {"$addToSet": {course_field: user["id"]}})
    _add_course_to_user(user["id"], course_id, role)


def join_share_code(
    user: dict[str, Any],
    code: str,
    role: str,
    local_date: Optional[str] = None,
) -> dict[str, Any]:
    normalized_code = (code or "").strip().upper()
    if not normalized_code:
        raise ValueError("share code is required")

    course_code_field = "taShareCode" if role == "ta" else "studentShareCode"
    section_code_field = course_code_field
    course = courses_collection.find_one({course_code_field: normalized_code}, {"_id": 0})
    section = sections_collection.find_one({section_code_field: normalized_code}, {"_id": 0})

    if not course and not section:
        raise LookupError("share code not found")

    if section:
        course = course_by_id(section["courseId"])
        if role == "ta":
            sections_collection.update_one({"id": section["id"]}, {"$addToSet": {"taIds": user["id"]}})
        record_section_participation(section["id"], user["id"], role)

    add_user_to_course(user, course["id"], role)
    is_ta = role == "ta"
    return {
        "course": _course_with_sections(course_by_id(course["id"]), role, user["id"], local_date=local_date),
        "section": sanitize_share_codes(section, is_ta),
    }


def _validate_section_schedule(date_value: str, start_time: str, end_time: str) -> tuple[str, str, str]:
    clean_date = (date_value or "").strip()[:10]
    clean_start = (start_time or "").strip()[:5]
    clean_end = (end_time or "").strip()[:5]
    try:
        date.fromisoformat(clean_date)
        start_dt = datetime.fromisoformat(f"{clean_date}T{clean_start}:00")
        end_dt = datetime.fromisoformat(f"{clean_date}T{clean_end}:00")
    except ValueError as error:
        raise ValueError("Enter a valid date (YYYY-MM-DD) and time (HH:MM).") from error
    if end_dt <= start_dt:
        raise ValueError("End time must be after start time.")
    return clean_date, clean_start, clean_end


def create_section(
    user: dict[str, Any],
    course_id: str,
    date_value: str,
    start_time: str,
    end_time: str,
    location: str,
    zoom_link: str = "",
    title: str = "",
) -> dict[str, Any]:
    course = course_by_id(course_id)
    if not course:
        raise LookupError("course not found")
    clean_date, clean_start, clean_end = _validate_section_schedule(date_value, start_time, end_time)

    add_user_to_course(user, course_id, "ta")
    section = create_slot(
        date=clean_date,
        start_time=clean_start,
        ta_name=user.get("name") or "TA",
        location=location.strip() or "Office Hours Room",
        course_id=course_id,
        course_code=course["code"],
    )
    section["id"] = f"section-{uuid4()}"
    section["endTime"] = clean_end
    section["title"] = title.strip() or f"{user.get('name') or 'TA'}'s Office Hour"
    section["zoomLink"] = zoom_link.strip()
    section["taIds"] = [user["id"]]
    section["participantTaIds"] = [user["id"]]
    section["participantStudentIds"] = []
    section["createdBy"] = user["id"]
    section["createdAt"] = utc_now()
    section["saved"] = False
    section["studentShareCode"] = _share_code("S-S")
    section["taShareCode"] = _share_code("T-S")
    section["expiresAt"] = _section_expiry(section)
    sections_collection.insert_one(deepcopy(section))
    return _without_id(section)


def create_recurring_sections(
    user: dict[str, Any],
    course_id: str,
    date_value: str,
    start_time: str,
    end_time: str,
    location: str,
    zoom_link: str = "",
    title: str = "",
    repeat_weeks: int = 1,
) -> list[dict[str, Any]]:
    weeks = max(1, min(16, int(repeat_weeks or 1)))
    clean_date, clean_start, clean_end = _validate_section_schedule(date_value, start_time, end_time)
    base_date = date.fromisoformat(clean_date)
    created = []
    for week_index in range(weeks):
        occurrence_date = (base_date + timedelta(days=7 * week_index)).isoformat()
        created.append(
            create_section(
                user=user,
                course_id=course_id,
                date_value=occurrence_date,
                start_time=clean_start,
                end_time=clean_end,
                location=location,
                zoom_link=zoom_link,
                title=title,
            )
        )
    return created


def update_section(section_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    section = section_by_id(section_id)
    if not section:
        return None

    allowed = {"courseId", "title", "date", "startTime", "endTime", "location", "zoomLink", "status"}
    next_values = {key: value for key, value in updates.items() if key in allowed and value is not None}
    if any(key in next_values for key in ("date", "startTime", "endTime")):
        clean_date, clean_start, clean_end = _validate_section_schedule(
            next_values.get("date", section.get("date", "")),
            next_values.get("startTime", section.get("startTime", "")),
            next_values.get("endTime", section.get("endTime", "")),
        )
        if "date" in next_values:
            next_values["date"] = clean_date
        if "startTime" in next_values:
            next_values["startTime"] = clean_start
        if "endTime" in next_values:
            next_values["endTime"] = clean_end
    if "courseId" in next_values:
        course = course_by_id(next_values["courseId"])
        if course:
            next_values["courseCode"] = course["code"]
        else:
            next_values.pop("courseId", None)

    labels = {
        "title": "Title", "date": "Date", "startTime": "Start time", "endTime": "End time",
        "location": "Location", "zoomLink": "Zoom link", "status": "Status",
        "courseCode": "Course",
    }
    changes = [
        {"field": key, "label": label, "before": section.get(key) or "", "after": next_values[key]}
        for key, label in labels.items()
        if key in next_values and (section.get(key) or "") != next_values[key]
    ]
    if changes and updates.get("highlightChange"):
        next_values["changeHighlighted"] = True
        next_values["changes"] = changes
        next_values["changeNotice"] = "; ".join(
            f"{change['label']}: {change['before'] or 'Not set'} → {change['after'] or 'Not set'}"
            for change in changes
        )
        next_values["changedAt"] = utc_now()
    elif changes:
        next_values["changeHighlighted"] = False
        next_values["changeNotice"] = ""
        next_values["changes"] = []

    if updates.get("saved") is True:
        next_values["saved"] = True
        update_doc = {"$set": next_values, "$unset": {"expiresAt": ""}}
    else:
        merged = {**section, **next_values}
        next_values["expiresAt"] = _section_expiry(merged)
        update_doc = {"$set": next_values}

    sections_collection.update_one({"id": section_id}, update_doc)
    return section_by_id(section_id)


def delete_section(section_id: str) -> bool:
    result = sections_collection.delete_one({"id": section_id})
    queue_collection.delete_many({"slotId": section_id})
    slot_status_collection.delete_one({"slotId": section_id})
    return result.deleted_count > 0


def section_by_id(section_id: str) -> Optional[dict[str, Any]]:
    return sections_collection.find_one({"id": section_id}, {"_id": 0})


def section_context(section_id: str) -> Optional[dict[str, Any]]:
    section = section_by_id(section_id)
    if not section:
        return None
    return {"section": section, "course": course_by_id(section["courseId"])}


def record_section_participation(section_id: str, user_id: str, role: str) -> None:
    field = "participantTaIds" if role == "ta" else "participantStudentIds"
    sections_collection.update_one({"id": section_id}, {"$addToSet": {field: user_id}})


def find_slot(slot_id: str) -> Optional[dict[str, Any]]:
    return section_by_id(slot_id)


def list_slots(course_id: Optional[str] = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {}
    if course_id:
        query["courseId"] = course_id
    return list(sections_collection.find(query, {"_id": 0}).sort([("date", 1), ("startTime", 1)]))


def insert_slots(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inserted = []
    for slot in slots:
        section = deepcopy(slot)
        section.setdefault("id", f"section-{uuid4()}")
        section.setdefault("studentShareCode", _share_code("S-S"))
        section.setdefault("taShareCode", _share_code("T-S"))
        section.setdefault("saved", False)
        section["expiresAt"] = _section_expiry(section)
        sections_collection.insert_one(deepcopy(section))
        inserted.append(_without_id(section))
    return inserted


def delete_slot(slot_id: str) -> bool:
    return delete_section(slot_id)


def list_queue_entries(slot_id: Optional[str] = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {}
    if slot_id:
        query["slotId"] = slot_id
    return list(queue_collection.find(query, {"_id": 0}).sort([("joinedAt", 1)]))


def list_waiting_entries(slot_id: Optional[str] = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"status": "waiting"}
    if slot_id:
        query["slotId"] = slot_id
    return list(queue_collection.find(query, {"_id": 0}).sort([("joinedAt", 1)]))


def _active_students_from_status(status_doc: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    if not status_doc:
        return []
    students = status_doc.get("currentStudents")
    if isinstance(students, list):
        return [item for item in students if isinstance(item, dict)]
    current = status_doc.get("current")
    return [current] if isinstance(current, dict) else []


def _persist_active_students(slot_id: str, active_students: list[dict[str, Any]]) -> None:
    cleaned_list = []
    for item in active_students:
        if not isinstance(item, dict):
            continue
        cleaned = deepcopy(item)
        cleaned.pop("_id", None)
        cleaned.pop("expiresAt", None)
        cleaned_list.append(cleaned)
    if cleaned_list:
        slot_status_collection.update_one(
            {"slotId": slot_id},
            {
                "$set": {"current": cleaned_list[0], "currentStudents": cleaned_list},
                "$setOnInsert": {"servedCount": 0},
            },
            upsert=True,
        )
    else:
        slot_status_collection.update_one(
            {"slotId": slot_id},
            {
                "$unset": {"current": "", "currentStudents": ""},
                "$setOnInsert": {"servedCount": 0},
            },
            upsert=True,
        )


def find_queue_entry(entry_id: str) -> Optional[dict[str, Any]]:
    entry = queue_collection.find_one({"id": entry_id}, {"_id": 0})
    if entry:
        return entry
    status_doc = slot_status_collection.find_one(
        {"$or": [{"current.id": entry_id}, {"currentStudents.id": entry_id}]},
        {"_id": 0},
    )
    for item in _active_students_from_status(status_doc):
        if item.get("id") == entry_id:
            return item
    return None


def find_active_entry_for_user(user_id: Optional[str], slot_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    if not user_id:
        return None
    query: dict[str, Any] = {"studentUserId": user_id, "status": "waiting"}
    if slot_id:
        query["slotId"] = slot_id
    waiting = queue_collection.find_one(query, {"_id": 0})
    if waiting:
        return waiting
    status_query: dict[str, Any] = {
        "$or": [{"current.studentUserId": user_id}, {"currentStudents.studentUserId": user_id}]
    }
    if slot_id:
        status_query["slotId"] = slot_id
    status_doc = slot_status_collection.find_one(status_query, {"_id": 0})
    for item in _active_students_from_status(status_doc):
        if item.get("studentUserId") == user_id:
            return item
    return None


def insert_queue_entry(entry: dict[str, Any]) -> dict[str, Any]:
    entry = deepcopy(entry)
    now = utc_now()
    entry.setdefault("createdAt", now)
    entry.setdefault("expiresAt", now + timedelta(days=RETENTION_DAYS))
    queue_collection.insert_one(deepcopy(entry))
    if entry.get("slotId") and entry.get("studentUserId"):
        record_section_participation(entry["slotId"], entry["studentUserId"], "student")
    return _without_id(entry)


def update_queue_entry_ai(entry_id: str, ai: dict[str, Any]) -> None:
    if not entry_id or not ai:
        return
    queue_collection.update_one({"id": entry_id}, {"$set": {"ai": ai}})
    status_doc = slot_status_collection.find_one(
        {"$or": [{"current.id": entry_id}, {"currentStudents.id": entry_id}]},
        {"_id": 0},
    )
    if status_doc and status_doc.get("slotId"):
        active = _active_students_from_status(status_doc)
        for item in active:
            if item.get("id") == entry_id:
                item["ai"] = ai
        _persist_active_students(status_doc["slotId"], active)


def delete_queue_entry(entry_id: str) -> bool:
    queue_result = queue_collection.delete_one({"id": entry_id})
    status_doc = slot_status_collection.find_one(
        {"$or": [{"current.id": entry_id}, {"currentStudents.id": entry_id}]},
        {"_id": 0},
    )
    status_removed = False
    if status_doc and status_doc.get("slotId"):
        active = _active_students_from_status(status_doc)
        remaining = [item for item in active if item.get("id") != entry_id]
        if len(remaining) != len(active):
            _persist_active_students(status_doc["slotId"], remaining)
            status_removed = True
    return queue_result.deleted_count > 0 or status_removed


def pop_next_waiting_entry(slot_id: str) -> Optional[dict[str, Any]]:
    return queue_collection.find_one_and_delete(
        {"slotId": slot_id, "status": "waiting"},
        sort=[("joinedAt", 1)],
        projection={"_id": 0},
    )


def get_current_by_slot() -> dict[str, Optional[dict[str, Any]]]:
    result: dict[str, Optional[dict[str, Any]]] = {}
    for status in slot_status_collection.find({}, {"_id": 0, "slotId": 1, "current": 1, "currentStudents": 1}):
        active = _active_students_from_status(status)
        result[status["slotId"]] = active[0] if active else None
    return result


def get_current_students_by_slot() -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for status in slot_status_collection.find({}, {"_id": 0, "slotId": 1, "current": 1, "currentStudents": 1}):
        result[status["slotId"]] = _active_students_from_status(status)
    return result


def get_current_students(slot_id: str) -> list[dict[str, Any]]:
    status = slot_status_collection.find_one({"slotId": slot_id}, {"_id": 0})
    return _active_students_from_status(status)


def get_current_student(slot_id: str, ta_user_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    active = get_current_students(slot_id)
    if not active:
        return None
    if ta_user_id:
        for item in active:
            if item.get("calledByTaId") == ta_user_id:
                return item
        for item in active:
            if not item.get("calledByTaId"):
                return item
        return None
    return active[0]


def set_current_student(
    slot_id: str,
    entry: Optional[dict[str, Any]],
    ta_user_id: Optional[str] = None,
) -> None:
    if ta_user_id is None:
        if entry is None:
            clear_current_student(slot_id)
            return
        _persist_active_students(slot_id, [entry])
        return

    active = get_current_students(slot_id)
    removed_own = False
    remaining: list[dict[str, Any]] = []
    for item in active:
        if not removed_own and item.get("calledByTaId") == ta_user_id:
            removed_own = True
            continue
        remaining.append(item)
    if not removed_own:
        final_remaining: list[dict[str, Any]] = []
        for item in remaining:
            if not removed_own and not item.get("calledByTaId"):
                removed_own = True
                continue
            final_remaining.append(item)
        remaining = final_remaining

    if entry is not None:
        remaining.append(entry)
    _persist_active_students(slot_id, remaining)


def clear_current_student(
    slot_id: str,
    ta_user_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    if ta_user_id is None:
        active = get_current_students(slot_id)
        _persist_active_students(slot_id, [])
        return active[0] if active else None

    active = get_current_students(slot_id)
    target = None
    remaining: list[dict[str, Any]] = []
    for item in active:
        if target is None and item.get("calledByTaId") == ta_user_id:
            target = item
            continue
        remaining.append(item)
    if target is None and remaining:
        target = remaining.pop(0)
    _persist_active_students(slot_id, remaining)
    return target


def return_current_student_to_queue(
    slot_id: str,
    ta_user_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    returned = clear_current_student(slot_id, ta_user_id=ta_user_id)
    if not returned:
        return None
    restored = deepcopy(returned)
    restored["status"] = "waiting"
    restored.pop("calledAt", None)
    restored.pop("calledByTaId", None)
    restored.pop("calledByTaName", None)
    waiting = list_waiting_entries(slot_id)
    if waiting and isinstance(waiting[0].get("joinedAt"), datetime):
        restored["joinedAt"] = waiting[0]["joinedAt"] + timedelta(milliseconds=1)
    elif not isinstance(restored.get("joinedAt"), datetime):
        restored["joinedAt"] = utc_now()
    insert_queue_entry(restored)
    return _without_id(restored)


def increment_served_count(slot_id: str) -> int:
    updated = slot_status_collection.find_one_and_update(
        {"slotId": slot_id},
        {"$inc": {"servedCount": 1}},
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
        upsert=True,
    )
    return int(updated.get("servedCount", 0)) if updated else 0


def get_served_by_slot() -> dict[str, int]:
    result: dict[str, int] = {}
    for status in slot_status_collection.find({}, {"_id": 0, "slotId": 1, "servedCount": 1}):
        result[status["slotId"]] = int(status.get("servedCount", 0))
    return result


def get_settings() -> dict[str, int]:
    settings = settings_collection.find_one({"_id": "app"}, {"_id": 0})
    return {**DEFAULT_SETTINGS, **(settings or {})}


def get_app_snapshot(
    course_id: Optional[str] = None,
    slot_id: Optional[str] = None,
) -> dict[str, Any]:
    settings = get_settings()
    if slot_id:
        slot = find_slot(slot_id)
        slots = [slot] if slot else []
        queue = list_queue_entries(slot_id=slot_id)
        status_doc = slot_status_collection.find_one({"slotId": slot_id}, {"_id": 0})
        active = _active_students_from_status(status_doc)
        current_by_slot = {slot_id: active[0] if active else None} if status_doc else {}
        current_students_by_slot = {slot_id: active} if status_doc else {}
        served_by_slot = {slot_id: int(status_doc.get("servedCount", 0))} if status_doc else {}
    else:
        slots = list_slots(course_id=course_id)
        queue = list_queue_entries()
        current_by_slot = get_current_by_slot()
        current_students_by_slot = get_current_students_by_slot()
        served_by_slot = get_served_by_slot()
    return {
        "slots": slots,
        "availability": slots,
        "queue": queue,
        "currentBySlot": current_by_slot,
        "currentStudentsBySlot": current_students_by_slot,
        "servedBySlot": served_by_slot,
        "tasActive": settings["tasActive"],
        "averageHelpMinutes": settings["averageHelpMinutes"],
        "forecast": [],
    }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def serialize(document: Any) -> Any:
    if isinstance(document, list):
        return [serialize(item) for item in document]
    if isinstance(document, dict):
        return {key: serialize(value) for key, value in document.items() if key != "_id"}
    if isinstance(document, datetime):
        return document.isoformat()
    return document


def _without_id(document: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if document is None:
        return None
    document = dict(document)
    document.pop("_id", None)
    return document


def _share_code(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3).upper()}"


def _add_course_to_user(user_id: str, course_id: str, role: str) -> None:
    field = "taCourseIds" if role == "ta" else "studentCourseIds"
    users_collection.update_one({"id": user_id}, {"$addToSet": {field: course_id}})


def _current_date_key(local_date: Optional[str] = None) -> str:
    if local_date:
        candidate = local_date.strip()[:10]
        try:
            date.fromisoformat(candidate)
            return candidate
        except ValueError:
            pass
    offset_env = os.getenv("APP_TIMEZONE_OFFSET_HOURS")
    if offset_env is not None:
        try:
            return (utc_now() + timedelta(hours=float(offset_env))).date().isoformat()
        except ValueError:
            pass
    local_now = datetime.now().astimezone()
    if local_now.utcoffset() == timedelta(0):
        return (utc_now() - timedelta(hours=5)).date().isoformat()
    return local_now.date().isoformat()


def _course_with_sections(
    course: dict[str, Any],
    role: str,
    user_id: str,
    local_date: Optional[str] = None,
) -> dict[str, Any]:
    now_key = _current_date_key(local_date)
    is_ta = role == "ta" and user_id in (course.get("taIds") or [])
    sections = list(sections_collection.find({"courseId": course["id"]}, {"_id": 0}).sort([("date", 1), ("startTime", 1)]))
    upcoming = [
        sanitize_share_codes(section, is_ta)
        for section in sections
        if section.get("date", "") >= now_key and section.get("status") != "cancelled"
    ]
    participant_field = "participantTaIds" if role == "ta" else "participantStudentIds"
    past = [
        sanitize_share_codes(section, is_ta)
        for section in sections
        if (section.get("date", "") < now_key or section.get("status") == "cancelled")
        and user_id in section.get(participant_field, [])
    ]
    return {**sanitize_share_codes(course, is_ta), "upcomingSections": upcoming, "pastSections": past}


def _section_expiry(section: dict[str, Any]) -> datetime:
    end_at = datetime.fromisoformat(f"{section['date']}T{section['endTime']}:00").replace(tzinfo=timezone.utc)
    return end_at + timedelta(days=RETENTION_DAYS)


def _seed_if_empty() -> None:
    if courses_collection.count_documents({}) > 0:
        return

    demo = login_user("demo.ta@studyline.local", "Bobby", "ta")["user"]
    course = create_course(demo, "CS 101", "Intro Computer Science")
    today = date.today().isoformat()
    create_section(demo, course["id"], today, "11:30", "12:00", "Library 204", "")
