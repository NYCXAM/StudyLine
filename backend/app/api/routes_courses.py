from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.services import db_service


router = APIRouter(tags=["courses"])


class CreateCourseRequest(BaseModel):
    code: str = Field(max_length=40)
    title: str = Field(default="", max_length=120)


class UpdateCourseRequest(BaseModel):
    code: str | None = Field(default=None, max_length=40)
    title: str | None = Field(default=None, max_length=120)


class CreateSectionRequest(BaseModel):
    course_id: str = Field(alias="courseId")
    title: str = Field(default="", max_length=120)
    date: str
    start_time: str = Field(alias="startTime")
    end_time: str = Field(alias="endTime")
    location: str = Field(default="", max_length=160)
    zoom_link: str = Field(default="", alias="zoomLink", max_length=240)
    repeat_weeks: int = Field(default=1, alias="repeatWeeks", ge=1, le=16)

    class Config:
        populate_by_name = True


class UpdateSectionRequest(BaseModel):
    course_id: str | None = Field(default=None, alias="courseId")
    title: str | None = Field(default=None, max_length=120)
    date: str | None = None
    start_time: str | None = Field(default=None, alias="startTime")
    end_time: str | None = Field(default=None, alias="endTime")
    location: str | None = None
    zoom_link: str | None = Field(default=None, alias="zoomLink")
    status: str | None = None
    highlight_change: bool = Field(default=False, alias="highlightChange")
    saved: bool | None = None

    class Config:
        populate_by_name = True


class JoinShareRequest(BaseModel):
    code: str = Field(max_length=40)
    role: str


def _require_user(token: str | None):
    user = db_service.user_for_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return user


def _require_course_ta(user, course_id: str):
    course = db_service.course_by_id(course_id)
    if not course:
        raise HTTPException(status_code=404, detail="course not found")
    if user["id"] not in course.get("taIds", []):
        raise HTTPException(status_code=403, detail="Only course TAs can change this.")
    return course


@router.get("/api/courses")
def list_courses(
    role: str,
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
    x_local_date: str | None = Header(default=None, alias="X-Local-Date"),
):
    if role not in {"student", "ta"}:
        raise HTTPException(status_code=400, detail="role must be student or ta")
    user = _require_user(x_user_token)
    courses = db_service.list_courses_for_user(user, role, local_date=x_local_date)
    return {"courses": db_service.serialize(courses)}


@router.post("/api/courses")
def create_course(
    payload: CreateCourseRequest,
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
):
    user = _require_user(x_user_token)
    try:
        course = db_service.create_course(user, payload.code, payload.title)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"course": db_service.serialize(course)}


@router.patch("/api/courses/{course_id}")
def update_course(
    course_id: str,
    payload: UpdateCourseRequest,
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
):
    user = _require_user(x_user_token)
    _require_course_ta(user, course_id)
    try:
        updated = db_service.update_course(course_id, {"code": payload.code, "title": payload.title})
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"course": db_service.serialize(updated)}


@router.post("/api/sections")
def create_section(
    payload: CreateSectionRequest,
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
):
    user = _require_user(x_user_token)
    _require_course_ta(user, payload.course_id)
    try:
        if payload.repeat_weeks > 1:
            sections = db_service.create_recurring_sections(
                user=user,
                course_id=payload.course_id,
                date_value=payload.date,
                start_time=payload.start_time,
                end_time=payload.end_time,
                location=payload.location,
                zoom_link=payload.zoom_link,
                title=payload.title,
                repeat_weeks=payload.repeat_weeks,
            )
            return {
                "section": db_service.serialize(sections[0]),
                "sections": db_service.serialize(sections),
            }
        section = db_service.create_section(
            user=user,
            course_id=payload.course_id,
            date_value=payload.date,
            start_time=payload.start_time,
            end_time=payload.end_time,
            location=payload.location,
            zoom_link=payload.zoom_link,
            title=payload.title,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"section": db_service.serialize(section), "sections": [db_service.serialize(section)]}


@router.patch("/api/sections/{section_id}")
def update_section(
    section_id: str,
    payload: UpdateSectionRequest,
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
):
    user = _require_user(x_user_token)
    existing = db_service.section_by_id(section_id)
    if not existing:
        raise HTTPException(status_code=404, detail="section not found")
    _require_course_ta(user, existing["courseId"])
    updates = {
        "courseId": payload.course_id,
        "title": payload.title,
        "date": payload.date,
        "startTime": payload.start_time,
        "endTime": payload.end_time,
        "location": payload.location,
        "zoomLink": payload.zoom_link,
        "status": payload.status,
        "highlightChange": payload.highlight_change,
        "saved": payload.saved,
    }
    try:
        section = db_service.update_section(section_id, updates)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"section": db_service.serialize(section)}


@router.delete("/api/sections/{section_id}")
def delete_section(
    section_id: str,
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
):
    user = _require_user(x_user_token)
    existing = db_service.section_by_id(section_id)
    if not existing:
        raise HTTPException(status_code=404, detail="section not found")
    _require_course_ta(user, existing["courseId"])
    return {"removed": db_service.delete_section(section_id)}


@router.post("/api/share/join")
def join_share(
    payload: JoinShareRequest,
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
    x_local_date: str | None = Header(default=None, alias="X-Local-Date"),
):
    user = _require_user(x_user_token)
    if payload.role not in {"student", "ta"}:
        raise HTTPException(status_code=400, detail="role must be student or ta")
    try:
        result = db_service.join_share_code(user, payload.code, payload.role, local_date=x_local_date)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return db_service.serialize(result)
