"""python -m unittest test_server  (meal-vote 폴더에서 실행)"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from server import App

KST = timezone(timedelta(hours=9))

CONFIG = {
    "utcOffsetHours": 9,
    "openDaysAhead": 7,
    "departments": ["생산", "품질"],
    "meals": [
        {"id": "lunch", "name": "중식", "cutoff": "09:30", "cutoffDayOffset": 0, "days": [1, 2, 3, 4, 5]},
        {"id": "dinner", "name": "석식", "cutoff": "16:00", "cutoffDayOffset": -1, "days": [1, 2, 3, 4, 5]},
    ],
    "holidays": ["2026-10-23"],
}


class Client:
    def __init__(self, app):
        self.app = app
        self.cookie = None

    def call(self, method, path, body=None, query=None):
        raw = json.dumps(body).encode() if body is not None else b""
        resp = self.app.handle(method, path, query or {}, raw, self.cookie)
        for key, value in resp.headers:
            if key == "Set-Cookie":
                self.cookie = value.split(";")[0]
        return resp.status, json.loads(resp.body) if resp.body.startswith(b"{") else resp.body

    def register(self, emp_no, name="홍길동", dept="생산", pin="1234"):
        return self.call("POST", "/api/register", {"empNo": emp_no, "name": name, "dept": dept, "pin": pin})

    def vote(self, day, meal, choice):
        return self.call("POST", "/api/vote", {"date": day, "meal": meal, "choice": choice})


class MealVoteTest(unittest.TestCase):
    def setUp(self):
        # 2026-10-21(수) 08:00 KST에서 시작
        self.now = datetime(2026, 10, 21, 8, 0, tzinfo=KST)
        self.app = App(CONFIG, ":memory:", clock=lambda: self.now)

    def set_time(self, day, hh, mm, ss=0):
        y, m, d = map(int, day.split("-"))
        self.now = datetime(y, m, d, hh, mm, ss, tzinfo=KST)

    def test_vote_locks_automatically_at_cutoff(self):
        c = Client(self.app)
        self.assertEqual(c.register("1001")[0], 200)
        self.assertEqual(c.vote("2026-10-21", "lunch", "eat")[0], 200)

        self.set_time("2026-10-21", 9, 29, 59)
        self.assertEqual(c.vote("2026-10-21", "lunch", "skip")[0], 200)

        self.set_time("2026-10-21", 9, 30)
        status, body = c.vote("2026-10-21", "lunch", "eat")
        self.assertEqual(status, 403)
        self.assertIn("마감", body["error"])

        # 마감 전 마지막 응답이 그대로 남는다
        _, summary = c.call("GET", "/api/summary", query={"date": ["2026-10-21"]})
        lunch = next(m for m in summary["meals"] if m["id"] == "lunch")
        self.assertEqual((lunch["eat"], lunch["skip"], lunch["status"]), (0, 1, "closed"))

    def test_previous_day_cutoff(self):
        c = Client(self.app)
        c.register("1001")
        # 10/22 석식은 전날(10/21) 16:00 마감
        self.set_time("2026-10-21", 15, 59)
        self.assertEqual(c.vote("2026-10-22", "dinner", "eat")[0], 200)
        self.set_time("2026-10-21", 16, 0)
        self.assertEqual(c.vote("2026-10-22", "dinner", "skip")[0], 403)
        # 10/22 중식은 아직 열려 있다
        self.assertEqual(c.vote("2026-10-22", "lunch", "eat")[0], 200)

    def test_no_meal_on_weekends_holidays_and_far_future(self):
        c = Client(self.app)
        c.register("1001")
        self.assertEqual(c.vote("2026-10-24", "lunch", "eat")[0], 400)  # 토요일
        self.assertEqual(c.vote("2026-10-23", "lunch", "eat")[0], 400)  # 휴일
        self.assertEqual(c.vote("2026-10-28", "lunch", "eat")[0], 200)  # 7일 뒤
        self.assertEqual(c.vote("2026-10-29", "lunch", "eat")[0], 403)  # 8일 뒤는 아직 안 열림

    def test_slots_reflect_server_time(self):
        c = Client(self.app)
        c.register("1001")
        c.vote("2026-10-21", "lunch", "eat")
        self.set_time("2026-10-21", 10, 0)
        _, data = c.call("GET", "/api/slots")
        today = data["days"][0]
        self.assertTrue(today["isToday"])
        self.assertEqual([(m["id"], m["status"], m["choice"]) for m in today["meals"]], [("lunch", "closed", "eat"), ("dinner", "closed", None)])
        self.assertEqual(data["days"][2]["meals"], [])  # 10/23 휴일

    def test_summary_counts_by_department(self):
        people = [("1001", "김생산", "생산", "eat"), ("1002", "이생산", "생산", "eat"), ("2001", "박품질", "품질", "skip"), ("2002", "최품질", "품질", None)]
        for emp_no, name, dept, choice in people:
            c = Client(self.app)
            c.register(emp_no, name, dept)
            if choice:
                c.vote("2026-10-21", "lunch", choice)
        _, summary = Client(self.app).call("GET", "/api/summary", query={"date": ["2026-10-21"]})
        lunch = summary["meals"][0]
        self.assertEqual((lunch["eat"], lunch["skip"], lunch["noResponse"]), (2, 1, 1))
        self.assertEqual(lunch["byDept"], [{"dept": "생산", "eat": 2}])
        self.assertEqual(summary["registered"], 4)

        _, csv_body = Client(self.app).call("GET", "/api/export.csv", query={"date": ["2026-10-21"]})
        lines = csv_body.decode("utf-8-sig").splitlines()
        self.assertEqual(lines[0], "날짜,식사,부서,사번,이름,응답")
        self.assertEqual(lines[1], "2026-10-21,중식,생산,1001,김생산,먹음")

    def test_login_requires_correct_pin_and_locks(self):
        Client(self.app).register("1001", pin="4321")
        c = Client(self.app)
        self.assertEqual(c.call("GET", "/api/me")[0], 401)
        self.assertEqual(c.vote("2026-10-21", "lunch", "eat")[0], 401)
        for _ in range(4):
            self.assertEqual(c.call("POST", "/api/login", {"empNo": "1001", "pin": "0000"})[0], 401)
        self.assertEqual(c.call("POST", "/api/login", {"empNo": "1001", "pin": "0000"})[0], 429)
        self.assertEqual(c.call("POST", "/api/login", {"empNo": "1001", "pin": "4321"})[0], 429)  # 잠김
        self.now += timedelta(minutes=11)
        self.assertEqual(c.call("POST", "/api/login", {"empNo": "1001", "pin": "4321"})[0], 200)
        self.assertEqual(c.call("GET", "/api/me")[1]["empNo"], "1001")

    def test_register_validation(self):
        c = Client(self.app)
        self.assertEqual(c.register("1001", dept="없는부서")[0], 400)
        self.assertEqual(c.register("1001", pin="12")[0], 400)
        self.assertEqual(c.register("1001")[0], 200)
        self.assertEqual(Client(self.app).register("1001")[0], 409)

    def test_logout_and_cli_commands(self):
        c = Client(self.app)
        c.register("1001", pin="1111")
        c.vote("2026-10-21", "lunch", "eat")
        c.call("POST", "/api/logout", {})
        self.assertEqual(c.call("GET", "/api/me")[0], 401)

        self.assertTrue(self.app.reset_pin("1001", "2222"))
        self.assertEqual(c.call("POST", "/api/login", {"empNo": "1001", "pin": "2222"})[0], 200)

        self.assertTrue(self.app.remove_user("1001"))
        self.assertEqual(c.call("GET", "/api/me")[0], 401)
        # 퇴사 처리 후에도 그날 응답 기록은 집계에 남고, 이후 날짜에는 빠진다
        _, today = c.call("GET", "/api/summary", query={"date": ["2026-10-21"]})
        self.assertEqual(today["meals"][0]["eat"], 1)
        _, later = c.call("GET", "/api/summary", query={"date": ["2026-10-22"]})
        self.assertEqual(later["registered"], 0)


if __name__ == "__main__":
    unittest.main()
