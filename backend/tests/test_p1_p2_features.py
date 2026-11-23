from copy import deepcopy
from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException

from app.api import routes_courses, routes_queue, routes_ta
from app.services import ai_service, db_service
from app.services.state_service import build_state
from tests.test_requested_fixes import render_frontend


class P1AndP2FeatureTests(unittest.TestCase):
    def setUp(self):
        routes_queue._queue_rate_buckets.clear()
        self.ta_alice = {"id": "user-alice@school.edu", "email": "alice@school.edu", "name": "TA Alice"}
        self.ta_bob = {"id": "user-bob@school.edu", "email": "bob@school.edu", "name": "TA Bob"}
        self.student_1 = {"id": "user-s1@school.edu", "email": "s1@school.edu", "name": "Student One"}
        self.student_2 = {"id": "user-s2@school.edu", "email": "s2@school.edu", "name": "Student Two"}
        self.course = {
            "id": "course-1",
            "code": "CS 101",
            "title": "Intro CS",
            "taIds": [self.ta_alice["id"], self.ta_bob["id"]],
            "studentIds": [self.student_1["id"], self.student_2["id"]],
        }
        self.section = {
            "id": "section-1",
            "courseId": "course-1",
            "courseCode": "CS 101",
            "title": "Office Hour",
            "date": "2026-11-23",
            "startTime": "14:00",
            "endTime": "16:00",
            "location": "Room 101",
            "zoomLink": "",
            "status": "scheduled",
            "taIds": [self.ta_alice["id"], self.ta_bob["id"]],
            "participantTaIds": [self.ta_alice["id"], self.ta_bob["id"]],
            "participantStudentIds": [self.student_1["id"], self.student_2["id"]],
        }
        self.entry_1 = {
            "id": "entry-1",
            "slotId": "section-1",
            "studentUserId": self.student_1["id"],
            "name": "Student One",
            "course": "CS 101",
            "need": "Debugging help",
            "message": "Segfault in linked list",
            "ai": {"summary": "Linked list bug", "estimatedHelpMinutes": 8},
            "status": "waiting",
            "joinedAt": datetime(2026, 11, 23, 14, 1, tzinfo=timezone.utc),
        }
        self.entry_2 = {
            "id": "entry-2",
            "slotId": "section-1",
            "studentUserId": self.student_2["id"],
            "name": "Student Two",
            "course": "CS 101",
            "need": "Concept question",
            "message": "Recursion tree depth",
            "ai": {"summary": "Recursion concept", "estimatedHelpMinutes": 7},
            "status": "waiting",
            "joinedAt": datetime(2026, 11, 23, 14, 2, tzinfo=timezone.utc),
        }

    def test_multi_ta_parallel_calling_does_not_overwrite_peer_ta_student(self):
        status_store: dict[str, dict] = {"slotId": "section-1", "servedCount": 0}
        waiting_queue = [deepcopy(self.entry_1), deepcopy(self.entry_2)]

        def fake_find_status(query, projection=None):
            return deepcopy(status_store) if status_store.get("current") or status_store.get("currentStudents") or status_store.get("servedCount") else None

        def fake_update_status(query, update, upsert=False):
            if "$set" in update:
                status_store.update(deepcopy(update["$set"]))
            if "$unset" in update:
                for key in update["$unset"]:
                    status_store.pop(key, None)

        def fake_inc_served(slot_id):
            status_store["servedCount"] = status_store.get("servedCount", 0) + 1
            return status_store["servedCount"]

        def fake_pop_waiting(slot_id):
            return waiting_queue.pop(0) if waiting_queue else None

        def fake_user_for_token(token):
            return self.ta_alice if token == "alice-token" else self.ta_bob if token == "bob-token" else None

        with (
            patch.object(db_service.slot_status_collection, "find_one", side_effect=fake_find_status),
            patch.object(db_service.slot_status_collection, "update_one", side_effect=fake_update_status),
            patch.object(db_service, "increment_served_count", side_effect=fake_inc_served),
            patch.object(db_service, "pop_next_waiting_entry", side_effect=fake_pop_waiting),
            patch.object(db_service, "user_for_token", side_effect=fake_user_for_token),
            patch.object(db_service, "find_slot", return_value=self.section),
            patch.object(db_service, "section_by_id", return_value=self.section),
            patch.object(db_service, "course_by_id", return_value=self.course),
            patch.object(db_service, "record_section_participation"),
            patch.object(db_service, "list_queue_entries", side_effect=lambda slot_id=None: deepcopy(waiting_queue)),
            patch.object(db_service, "get_settings", return_value={"tasActive": 2, "averageHelpMinutes": 7}),
        ):
            res_alice = routes_ta.call_next(slot_id="section-1", x_user_token="alice-token")
            self.assertEqual(res_alice["next"]["id"], "entry-1")
            self.assertEqual(status_store["servedCount"], 0)

            res_bob = routes_ta.call_next(slot_id="section-1", x_user_token="bob-token")
            self.assertEqual(res_bob["next"]["id"], "entry-2")
            # TA Bob calling next must NOT mark TA Alice's student as served!
            self.assertEqual(status_store["servedCount"], 0)
            self.assertEqual(len(status_store["currentStudents"]), 2)

            # Both Student 1 and Student 2 see status == "called"
            snapshot = db_service.get_app_snapshot(slot_id="section-1")
            s1_state = build_state(
                slots=snapshot["slots"],
                queue_entries=snapshot["queue"],
                current_by_slot=snapshot["currentBySlot"],
                current_students_by_slot=snapshot["currentStudentsBySlot"],
                served_by_slot=snapshot["servedBySlot"],
                student_id="entry-1",
                requested_slot_id="section-1",
            )
            s2_state = build_state(
                slots=snapshot["slots"],
                queue_entries=snapshot["queue"],
                current_by_slot=snapshot["currentBySlot"],
                current_students_by_slot=snapshot["currentStudentsBySlot"],
                served_by_slot=snapshot["servedBySlot"],
                student_id="entry-2",
                requested_slot_id="section-1",
            )
            self.assertEqual(s1_state["queue"]["status"], "called")
            self.assertEqual(s2_state["queue"]["status"], "called")

            # TA Bob serves his student; TA Alice's student stays active
            served_bob = routes_ta.serve_current(slot_id="section-1", x_user_token="bob-token")
            self.assertEqual(served_bob["served"]["id"], "entry-2")
            self.assertEqual(status_store["servedCount"], 1)
            self.assertEqual(len(status_store["currentStudents"]), 1)
            self.assertEqual(status_store["currentStudents"][0]["id"], "entry-1")

    def test_return_current_student_to_queue_restores_waiting_entry(self):
        called_entry = {
            **deepcopy(self.entry_1),
            "status": "called",
            "calledByTaId": self.ta_alice["id"],
            "calledByTaName": "TA Alice",
        }
        inserted_entries = []

        with (
            patch.object(db_service, "clear_current_student", return_value=called_entry),
            patch.object(db_service, "list_waiting_entries", return_value=[deepcopy(self.entry_2)]),
            patch.object(db_service, "insert_queue_entry", side_effect=lambda e: inserted_entries.append(e) or e),
        ):
            restored = db_service.return_current_student_to_queue("section-1", ta_user_id=self.ta_alice["id"])
            self.assertIsNotNone(restored)
            self.assertEqual(restored["status"], "waiting")
            self.assertNotIn("calledByTaId", restored)
            self.assertEqual(len(inserted_entries), 1)
            self.assertGreater(restored["joinedAt"], self.entry_2["joinedAt"])

    def test_account_bound_queue_prevents_duplicates_and_recovers_across_devices(self):
        with (
            patch.object(db_service, "find_slot", return_value=self.section),
            patch.object(db_service, "user_for_token", return_value=self.student_1),
            patch.object(db_service, "find_active_entry_for_user", return_value=self.entry_1),
        ):
            with self.assertRaises(HTTPException) as ctx:
                routes_queue.create_queue_entry(
                    routes_queue.CreateQueueEntryRequest(slotId="section-1", name="Student One"),
                    x_user_token="s1-token",
                )
            self.assertEqual(ctx.exception.status_code, 409)

        snapshot = {
            "slots": [self.section],
            "availability": [self.section],
            "queue": [self.entry_1],
            "currentBySlot": {},
            "currentStudentsBySlot": {},
            "servedBySlot": {},
            "tasActive": 2,
            "averageHelpMinutes": 7,
            "forecast": [],
        }
        with (
            patch.object(db_service, "user_for_token", return_value=self.student_1),
            patch.object(db_service, "find_active_entry_for_user", return_value=self.entry_1),
            patch.object(db_service, "find_queue_entry", return_value=self.entry_1),
            patch.object(db_service, "get_app_snapshot", return_value=snapshot),
        ):
            # Calling queue_me without X-Queue-Token (e.g. from phone) recovers status via X-User-Token
            me = routes_queue.queue_me(slot_id="section-1", queue_token=None, x_user_token="s1-token")
            self.assertEqual(me["status"], "next")
            self.assertEqual(me["entry_id"], "entry-1")

    def test_recurring_sections_creates_weekly_occurrences(self):
        created_dates = []

        def fake_create_section(user, course_id, date_value, start_time, end_time, location, zoom_link="", title=""):
            created_dates.append(date_value)
            return {"id": f"sec-{date_value}", "date": date_value, "startTime": start_time, "endTime": end_time}

        with patch.object(db_service, "create_section", side_effect=fake_create_section):
            sections = db_service.create_recurring_sections(
                user=self.ta_alice,
                course_id="course-1",
                date_value="2026-11-23",
                start_time="10:00",
                end_time="11:00",
                location="Room 101",
                repeat_weeks=4,
            )
            self.assertEqual(len(sections), 4)
            self.assertEqual(
                created_dates,
                ["2026-11-23", "2026-11-30", "2026-12-07", "2026-12-14"],
            )

    def test_async_ai_triage_and_rate_limiting_on_queue_join(self):
        inserted = []
        bg_tasks = BackgroundTasks()

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}),
            patch.object(db_service, "find_slot", return_value=self.section),
            patch.object(db_service, "user_for_token", return_value=None),
            patch.object(db_service, "insert_queue_entry", side_effect=lambda e: inserted.append(e) or e),
            patch.object(routes_queue, "_state", return_value={"staff": {"servedCount": 0}}),
            patch.object(ai_service, "_analyze_with_openai") as mock_openai,
        ):
            result = routes_queue.create_queue_entry(
                routes_queue.CreateQueueEntryRequest(
                    slotId="section-1",
                    name="Fast Student",
                    need="Debugging help",
                    message="Pointer error",
                ),
                background_tasks=bg_tasks,
                x_user_token=None,
                queue_token=None,
            )
            # Immediate response uses fast local heuristic and queues background task
            self.assertEqual(result["entry"]["ai"]["source"], "local")
            mock_openai.assert_not_called()
            self.assertEqual(len(bg_tasks.tasks), 1)

            # 5 requests allowed within window; 6th request from same actor hits 429 rate limit
            for _ in range(4):
                routes_queue.create_queue_entry(
                    routes_queue.CreateQueueEntryRequest(slotId="section-1", name="Fast Student"),
                    background_tasks=bg_tasks,
                )
            with self.assertRaises(HTTPException) as rate_ctx:
                routes_queue.create_queue_entry(
                    routes_queue.CreateQueueEntryRequest(slotId="section-1", name="Fast Student"),
                    background_tasks=bg_tasks,
                )
            self.assertEqual(rate_ctx.exception.status_code, 429)

    def test_ta_ui_renders_return_to_line_and_peer_ta_students(self):
        state = {
            "live": {"estimatedWaitMinutes": 8, "studentsWaiting": 1, "tasActive": 2, "averageHelpMinutes": 7},
            "staff": {
                "currentStudent": {"id": "entry-1", "name": "Student One", "course": "CS 101", "need": "Debugging"},
                "currentStudents": [
                    {"id": "entry-1", "name": "Student One", "course": "CS 101", "need": "Debugging", "calledByTaName": "TA Alice"},
                    {"id": "entry-2", "name": "Student Two", "course": "CS 101", "need": "Concept", "calledByTaName": "TA Bob"},
                ],
                "servedCount": 3,
                "waitingEntries": [],
            },
        }
        html = render_frontend(
            f"renderTaSectionTools({routes_courses.json.dumps(self.course) if hasattr(routes_courses, 'json') else '{}'}, {{}}, {__import__('json').dumps(state)})",
            page="ta",
        )
        self.assertIn("Return to Line", html)
        self.assertIn("Also being helped: Student Two (TA Bob)", html)


if __name__ == "__main__":
    unittest.main()
