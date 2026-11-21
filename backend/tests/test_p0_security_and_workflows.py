import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException

from app.api import routes_courses, routes_queue, routes_slots, routes_ta
from app.services import db_service
from tests.test_requested_fixes import render_frontend


class P0SecurityAndWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.ta_user = {"id": "user-ta@school.edu", "email": "ta@school.edu", "name": "TA Alice"}
        self.student_user = {"id": "user-stu@school.edu", "email": "stu@school.edu", "name": "Student Bob"}
        self.other_student = {"id": "user-other@school.edu", "email": "other@school.edu", "name": "Student Eve"}
        self.course = {
            "id": "course-1",
            "code": "CS 101",
            "title": "Intro CS",
            "taIds": [self.ta_user["id"]],
            "studentIds": [self.student_user["id"]],
            "studentShareCode": "S-C-111111",
            "taShareCode": "T-C-222222",
        }
        self.section = {
            "id": "section-1",
            "courseId": "course-1",
            "courseCode": "CS 101",
            "title": "TA Alice's Office Hour",
            "date": "2026-11-20",
            "startTime": "19:00",
            "endTime": "21:00",
            "location": "Room 101",
            "zoomLink": "https://zoom.us/j/123",
            "status": "scheduled",
            "taIds": [self.ta_user["id"]],
            "participantTaIds": [self.ta_user["id"]],
            "participantStudentIds": [self.student_user["id"]],
            "studentShareCode": "S-S-333333",
            "taShareCode": "T-S-444444",
        }
        self.queue_entry = {
            "id": "entry-1",
            "slotId": "section-1",
            "studentUserId": self.student_user["id"],
            "name": "Student Bob",
            "course": "CS 101",
            "need": "Debugging help",
            "message": "Private homework pointer segfault details",
            "ai": {"summary": "Pointer bug", "estimatedHelpMinutes": 10},
            "status": "waiting",
            "joinedAt": datetime(2026, 11, 20, 19, 5, tzinfo=timezone.utc),
        }

    def test_create_course_blocks_hijacking_existing_course(self):
        with patch.object(db_service, "courses_collection") as courses_col:
            courses_col.find_one.return_value = self.course
            with self.assertRaises(PermissionError):
                db_service.create_course(self.student_user, "cs 101", "Hijacked Course")
            courses_col.update_one.assert_not_called()

        with (
            patch.object(db_service, "user_for_token", return_value=self.student_user),
            patch.object(db_service, "courses_collection") as courses_col,
        ):
            courses_col.find_one.return_value = self.course
            with self.assertRaises(HTTPException) as ctx:
                routes_courses.create_course(
                    routes_courses.CreateCourseRequest(code="CS 101", title="Hijack"),
                    x_user_token="stu-token",
                )
            self.assertEqual(ctx.exception.status_code, 403)

    def test_expired_session_token_is_rejected_and_cleaned_up(self):
        expired_session = {
            "token": "expired-token",
            "userId": self.student_user["id"],
            "expiresAt": datetime.now(timezone.utc) - timedelta(minutes=5),
        }
        with (
            patch.object(db_service, "sessions_collection") as sessions_col,
            patch.object(db_service, "users_collection") as users_col,
        ):
            sessions_col.find_one.return_value = expired_session
            self.assertIsNone(db_service.user_for_token("expired-token"))
            sessions_col.delete_one.assert_called_once_with({"token": "expired-token"})
            users_col.find_one.assert_not_called()

    def test_staff_queue_endpoints_require_course_ta(self):
        def fake_user_for_token(token):
            if token == "ta-token":
                return self.ta_user
            if token == "stu-token":
                return self.student_user
            return None

        with (
            patch.object(db_service, "user_for_token", side_effect=fake_user_for_token),
            patch.object(db_service, "find_slot", return_value=self.section),
            patch.object(db_service, "section_by_id", return_value=self.section),
            patch.object(db_service, "course_by_id", return_value=self.course),
        ):
            with self.assertRaises(HTTPException) as unauth_ctx:
                routes_ta.call_next(slot_id="section-1", x_user_token=None)
            self.assertEqual(unauth_ctx.exception.status_code, 401)

            with self.assertRaises(HTTPException) as forbidden_ctx:
                routes_ta.call_next(slot_id="section-1", x_user_token="stu-token")
            self.assertEqual(forbidden_ctx.exception.status_code, 403)

            with self.assertRaises(HTTPException) as serve_ctx:
                routes_ta.serve_current(slot_id="section-1", x_user_token="stu-token")
            self.assertEqual(serve_ctx.exception.status_code, 403)

            with self.assertRaises(HTTPException) as list_ctx:
                routes_ta.staff_queue(slot_id="section-1", x_user_token="stu-token")
            self.assertEqual(list_ctx.exception.status_code, 403)

    def test_section_state_redacts_student_questions_and_ta_share_codes_for_non_tas(self):
        snapshot = {
            "slots": [self.section],
            "availability": [self.section],
            "queue": [self.queue_entry],
            "currentBySlot": {"section-1": self.queue_entry},
            "servedBySlot": {"section-1": 2},
            "tasActive": 1,
            "averageHelpMinutes": 7,
            "forecast": [],
        }

        def fake_user_for_token(token):
            if token == "ta-token":
                return self.ta_user
            if token == "stu-token":
                return self.other_student
            return None

        with (
            patch.object(db_service, "user_for_token", side_effect=fake_user_for_token),
            patch.object(db_service, "section_by_id", return_value=self.section),
            patch.object(db_service, "course_by_id", return_value=self.course),
            patch.object(db_service, "get_app_snapshot", return_value=snapshot),
        ):
            student_view = routes_queue.section_state(
                "section-1",
                queue_token=None,
                x_user_token="stu-token",
            )
            self.assertEqual(student_view["state"]["staff"]["waitingEntries"], [])
            self.assertIsNone(student_view["state"]["staff"]["currentStudent"])
            self.assertNotIn("taShareCode", student_view["course"])
            self.assertNotIn("taShareCode", student_view["section"])

            ta_view = routes_queue.section_state(
                "section-1",
                queue_token=None,
                x_user_token="ta-token",
            )
            self.assertEqual(len(ta_view["state"]["staff"]["waitingEntries"]), 1)
            self.assertEqual(
                ta_view["state"]["staff"]["waitingEntries"][0]["message"],
                "Private homework pointer segfault details",
            )
            self.assertEqual(ta_view["course"]["taShareCode"], "T-C-222222")
            self.assertEqual(ta_view["section"]["taShareCode"], "T-S-444444")

    def test_delete_queue_entry_and_availability_enforce_authorization(self):
        def fake_user_for_token(token):
            if token == "ta-token":
                return self.ta_user
            if token == "stu-token":
                return self.student_user
            if token == "other-token":
                return self.other_student
            return None

        with (
            patch.object(db_service, "user_for_token", side_effect=fake_user_for_token),
            patch.object(db_service, "find_queue_entry", return_value=self.queue_entry),
            patch.object(db_service, "find_slot", return_value=self.section),
            patch.object(db_service, "section_by_id", return_value=self.section),
            patch.object(db_service, "course_by_id", return_value=self.course),
            patch.object(db_service, "delete_queue_entry", return_value=True),
            patch.object(db_service, "delete_slot", return_value=True),
            patch.object(routes_queue, "_state", return_value={"staff": {"servedCount": 0}}),
        ):
            with self.assertRaises(HTTPException) as ctx:
                routes_queue.delete_queue_entry(
                    "entry-1",
                    queue_token="wrong-token",
                    x_user_token="other-token",
                )
            self.assertEqual(ctx.exception.status_code, 403)

            own_delete = routes_queue.delete_queue_entry(
                "entry-1",
                queue_token="wrong-token",
                x_user_token="stu-token",
            )
            self.assertTrue(own_delete["removed"])

            with self.assertRaises(HTTPException) as slot_ctx:
                routes_slots.delete_availability("section-1", x_user_token="stu-token")
            self.assertEqual(slot_ctx.exception.status_code, 403)

    def test_section_time_validation_and_local_date_filtering(self):
        with patch.object(db_service, "course_by_id", return_value=self.course):
            with self.assertRaises(ValueError):
                db_service.create_section(
                    self.ta_user,
                    self.course["id"],
                    "2026-11-20",
                    "14:00",
                    "13:00",
                    "Room 1",
                )

        with patch.object(db_service, "sections_collection") as sections_col:
            sections_col.find.return_value.sort.return_value = [self.section]
            course_view = db_service._course_with_sections(
                self.course,
                "student",
                self.student_user["id"],
                local_date="2026-11-20",
            )
            self.assertEqual(len(course_view["upcomingSections"]), 1)
            self.assertEqual(len(course_view["pastSections"]), 0)
            self.assertNotIn("taShareCode", course_view)

    def test_frontend_notifies_when_called_and_polls_for_ta(self):
        expression = """
          queueToken = "queue-1";
          queueNotificationEnabled = true;
          maybeNotifyNextStudent(
            {code: "CS 101"},
            {title: "TA Alice's Office Hour"},
            {queue: {status: "called"}}
          );
          maybeNotifyNextStudent(
            {code: "CS 101"},
            {title: "TA Alice's Office Hour"},
            {queue: {status: "called"}}
          );
          JSON.stringify(notifications);
        """
        notifications = json.loads(render_frontend(expression, page="student"))
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["title"], "It’s your turn")


if __name__ == "__main__":
    unittest.main()
