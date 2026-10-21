"""python -m unittest test_server  (meal-vote 폴더에서 실행)"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from server import App, read_roster

KST = timezone(timedelta(hours=9))

CONFIG = {
    "siteName": "인주공장",
    "utcOffsetHours": 9,
    "remindMinutesBefore": 15,
    "meals": [
        {"id": "lunch", "name": "점심", "notifyAt": "08:00", "cutoff": "09:00", "finalAt": "09:30", "days": [1, 2, 3, 4, 5]},
        {"id": "dinner", "name": "저녁", "notifyAt": "12:00", "cutoff": "14:30", "finalAt": "15:00", "days": [1, 2, 3, 4, 5]},
    ],
    "holidays": ["2026-10-23"],
    "vendor": {"to": ["vendor@example.com"], "cc": []},
}

ROSTER = [
    {"id": 1, "name": "조담당", "title": "대리", "grp": "생산팀", "team": "생산운영실", "email": "a@x.com", "manager": True},
    {"id": 2, "name": "김생산", "title": "선임", "grp": "생산팀", "team": "생산팀", "email": "b@x.com", "manager": False},
    {"id": 3, "name": "이생산", "title": "사원", "grp": "생산팀", "team": "생산팀", "email": "", "manager": False},
    {"id": 4, "name": "박분석", "title": "과장", "grp": "분석", "team": "분석팀", "email": "d@x.com", "manager": True},
]


class Client:
    def __init__(self, app):
        self.app = app
        self.cookie = None

    def call(self, method, path, body=None, query=None):
        raw = json.dumps(body).encode() if body is not None else b""
        resp = self.app.handle(method, path, query or {}, raw, self.cookie)
        headers = dict(resp.headers)
        if "Set-Cookie" in headers:
            self.cookie = headers["Set-Cookie"].split(";")[0]
        if resp.body.startswith(b"{"):
            return resp.status, json.loads(resp.body)
        return resp.status, headers if not resp.body else resp.body

    def open_link(self, url):
        parsed = urlparse(url)
        return self.call("GET", parsed.path, query=parse_qs(parsed.query))

    def vote(self, day, meal, choice):
        return self.call("POST", "/api/vote", {"date": day, "meal": meal, "choice": choice})

    def extra(self, day, meal, count, note=""):
        return self.call("POST", "/api/extra", {"date": day, "meal": meal, "count": count, "note": note})


class MealVoteTest(unittest.TestCase):
    def setUp(self):
        # 2026-10-21(수) 07:50 KST에서 시작
        self.now = datetime(2026, 10, 21, 7, 50, tzinfo=KST)
        self.sent = []
        self.send_ok = True
        self.app = App(CONFIG, ":memory:", clock=lambda: self.now, send=self.capture, base_url="http://srv")
        self.app.sync_roster(ROSTER)

    def capture(self, payload):
        self.sent.append(payload)
        return self.send_ok

    def set_time(self, hh, mm, day=21):
        self.now = datetime(2026, 10, day, hh, mm, tzinfo=KST)

    def user(self, uid):
        return self.app.db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

    def login(self, uid):
        c = Client(self.app)
        status, _ = c.open_link(self.app.personal_url(self.user(uid)))
        self.assertEqual(status, 303)
        return c

    def summary(self, day="2026-10-21"):
        return Client(self.app).call("GET", "/api/summary", query={"date": [day]})[1]

    # ---- 개인 링크 ----

    def test_personal_link_logs_in_without_password(self):
        c = Client(self.app)
        self.assertEqual(c.call("GET", "/api/me")[0], 401)
        c.open_link(self.app.personal_url(self.user(2)))
        self.assertEqual(c.call("GET", "/api/me")[1]["name"], "김생산")
        self.assertEqual(Client(self.app).call("GET", "/me/wrong-token")[0], 404)

    def test_vote_button_link_redirects_without_voting(self):
        c = Client(self.app)
        url = self.app.personal_url(self.user(2), datetime(2026, 10, 21).date(), CONFIG["meals"][0], "eat")
        status, headers = c.open_link(url)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/?d=2026-10-21&m=lunch&c=eat")
        # 링크를 여는 것만으로는 투표되지 않는다 (화면 스크립트가 POST로 투표)
        self.assertEqual(self.summary()["meals"][0]["regular"], 0)

    def test_roster_change_disables_removed_people(self):
        c = self.login(3)
        self.app.sync_roster([r for r in ROSTER if r["id"] != 3])
        self.assertEqual(c.call("GET", "/api/me")[0], 401)
        self.assertEqual(self.summary()["registered"], 3)

    def test_new_link_invalidates_old_one(self):
        old = self.app.personal_url(self.user(2))
        c = self.login(2)
        self.app.new_link(2)
        self.assertEqual(c.call("GET", "/api/me")[0], 401)
        self.assertEqual(Client(self.app).open_link(old)[0], 404)

    # ---- 마감 ----

    def test_vote_locks_at_cutoff(self):
        c = self.login(2)
        self.set_time(8, 59)
        self.assertEqual(c.vote("2026-10-21", "lunch", "eat")[0], 200)
        self.set_time(9, 0)
        status, body = c.vote("2026-10-21", "lunch", "skip")
        self.assertEqual(status, 403)
        self.assertIn("마감", body["error"])
        self.assertEqual(self.summary()["meals"][0]["regular"], 1)

    def test_only_today_can_be_voted(self):
        c = self.login(2)
        self.assertEqual(c.vote("2026-10-21", "lunch", "eat")[0], 200)
        status, body = c.vote("2026-10-22", "lunch", "eat")  # 내일
        self.assertEqual((status, body["error"]), (403, "오늘 식사만 투표할 수 있습니다."))
        self.assertEqual(c.vote("2026-10-24", "lunch", "eat")[0], 400)  # 토요일
        self.assertEqual(c.vote("2026-10-23", "lunch", "eat")[0], 400)  # 휴일
        self.assertEqual(self.login(1).extra("2026-10-22", "lunch", 1)[0], 403)

    def test_slots_show_today_only(self):
        c = self.login(1)
        c.vote("2026-10-21", "lunch", "eat")
        self.set_time(9, 10)
        _, data = c.call("GET", "/api/slots")
        self.assertEqual(data["date"], "2026-10-21")
        self.assertEqual([(m["id"], m["status"], m["choice"], m["extra"]["status"]) for m in data["meals"]],
                         [("lunch", "closed", "eat", "open"), ("dinner", "open", None, "open")])
        self.now = datetime(2026, 10, 23, 9, 0, tzinfo=KST)  # 휴일
        self.assertEqual(c.call("GET", "/api/slots")[1]["meals"], [])

    def test_manager_extras_window(self):
        manager, member = self.login(1), self.login(2)
        self.assertEqual(member.extra("2026-10-21", "lunch", 2)[0], 403)
        self.set_time(9, 10)  # 투표는 마감, 추가 인원은 09:30까지
        self.assertEqual(manager.vote("2026-10-21", "lunch", "eat")[0], 403)
        self.assertEqual(manager.extra("2026-10-21", "lunch", 2, "음봉 교육")[0], 200)
        self.assertEqual(manager.extra("2026-10-21", "lunch", -1)[0], 400)
        self.set_time(9, 30)
        self.assertEqual(manager.extra("2026-10-21", "lunch", 3)[0], 403)
        lunch = self.summary()["meals"][0]
        group = next(g for g in lunch["groups"] if g["grp"] == "생산팀")
        self.assertEqual((group["extra"], group["note"], lunch["phase"]), (2, "음봉 교육", "final"))

    def test_summary_and_exports(self):
        self.login(1).vote("2026-10-21", "lunch", "eat")
        self.login(2).vote("2026-10-21", "lunch", "eat")
        self.login(4).vote("2026-10-21", "lunch", "skip")
        self.login(1).extra("2026-10-21", "lunch", 2, "방문객")
        lunch = self.summary()["meals"][0]
        self.assertEqual((lunch["regular"], lunch["extra"], lunch["total"], lunch["skip"], lunch["noResponse"]), (2, 2, 4, 1, 1))
        self.assertEqual([g["grp"] for g in lunch["groups"]], ["생산팀", "분석"])

        _, body = Client(self.app).call("GET", "/api/export.csv", query={"date": ["2026-10-21"]})
        lines = body.decode("utf-8-sig").splitlines()
        self.assertEqual(lines[0], "날짜,식사,분류,팀,이름,응답,비고")
        self.assertIn("2026-10-21,점심,생산팀,,추가 인원,2명,방문객", lines)

        self.set_time(16, 0)  # 점심·저녁 모두 확정된 뒤
        _, body = Client(self.app).call("GET", "/api/export.csv", query={"month": ["2026-10"]})
        lines = body.decode("utf-8-sig").splitlines()
        self.assertEqual(lines[1:], ["2026-10-21,수,점심,생산팀,2,2,4,방문객"])

    def test_month_view_counts_only_confirmed_meals(self):
        self.now = datetime(2026, 10, 20, 8, 0, tzinfo=KST)  # 화요일
        self.login(1).vote("2026-10-20", "lunch", "eat")
        self.login(4).vote("2026-10-20", "lunch", "eat")
        self.login(4).vote("2026-10-20", "dinner", "eat")
        self.set_time(8, 30)  # 다음 날(21일) 아침: 20일 끼니는 모두 확정, 21일 점심은 진행 중
        self.login(2).vote("2026-10-21", "lunch", "eat")
        _, data = Client(self.app).call("GET", "/api/month", query={"month": ["2026-10"]})
        days = {d["date"]: d["meals"] for d in data["days"]}
        self.assertEqual(list(days)[-2:], ["2026-10-20", "2026-10-21"])  # 오늘까지만
        self.assertEqual(days["2026-10-20"]["lunch"], {"total": 2, "extra": 0, "final": True})
        self.assertEqual(days["2026-10-21"]["lunch"], {"total": 1, "extra": 0, "final": False})
        self.assertEqual(data["totals"], {"lunch": 2, "dinner": 1})
        self.assertEqual(data["groups"], [{"grp": "생산팀", "meals": {"lunch": 1, "dinner": 0}, "total": 1},
                                          {"grp": "분석", "meals": {"lunch": 1, "dinner": 1}, "total": 2}])
        self.assertEqual(Client(self.app).call("GET", "/api/month", query={"month": ["2026-13"]})[0], 400)

    # ---- 자동 알림 ----

    def run_at(self, hh, mm):
        self.set_time(hh, mm)
        before = len(self.sent)
        self.app.run_due_notices()
        return self.sent[before:]

    def test_daily_notice_flow(self):
        self.assertEqual(self.run_at(7, 59), [])

        sent = self.run_at(8, 0)
        self.assertEqual([p["kind"] for p in sent], ["open"])
        self.assertEqual([m["to"] for m in sent[0]["teamsChat"]], ["a@x.com", "b@x.com", "d@x.com"])  # 이메일 없는 3번 제외
        buttons = sent[0]["teamsChat"][0]["card"]["actions"]
        self.assertEqual([b["title"] for b in buttons], ["먹음", "안먹음"])
        self.assertIn("c=eat", buttons[0]["url"])
        self.assertEqual(self.run_at(8, 5), [])  # 같은 알림은 한 번만

        self.login(2).vote("2026-10-21", "lunch", "eat")
        sent = self.run_at(8, 45)
        self.assertEqual([m["to"] for m in sent[0]["teamsChat"]], ["a@x.com", "d@x.com"])  # 투표한 사람은 빼고

        sent = self.run_at(9, 0)
        self.assertEqual(sent[0]["kind"], "tally")
        self.assertEqual([m["to"] for m in sent[0]["teamsChat"]], ["a@x.com", "d@x.com"])  # 담당자만

        self.set_time(9, 10)
        self.login(1).extra("2026-10-21", "lunch", 2, "방문객")
        sent = self.run_at(9, 30)
        final = sent[0]
        self.assertEqual(final["kind"], "final")
        self.assertEqual(final["email"][0]["to"], "vendor@example.com")
        self.assertEqual(final["email"][0]["subject"], "[인주공장] 10/21 (수) 점심 식수 3명")
        self.assertIn("방문객", final["email"][0]["html"])
        self.assertEqual(final["teamsChannel"][0]["card"]["body"][0]["text"], "10/21 (수) 점심 식수 최종 3명")
        self.assertEqual(final["records"][0], {"date": "2026-10-21", "meal": "점심", "group": "생산팀",
                                               "regular": 1, "extra": 2, "total": 3, "note": "방문객"})
        self.assertEqual(self.run_at(10, 0), [])

    def test_late_start_skips_stale_notices_but_still_sends_final(self):
        sent = self.run_at(8, 50)  # 08:00 알림 시간은 지났고 리마인드 시간대
        self.assertEqual([p["kind"] for p in sent], ["remind"])
        sent = self.run_at(10, 20)  # 09:30에 꺼져 있었다면 1시간 안에는 늦게라도 확정 통보
        self.assertEqual([p["kind"] for p in sent], ["final"])

    def test_first_start_in_the_evening_sends_nothing(self):
        self.assertEqual(self.run_at(22, 0), [])

    def test_failed_send_is_retried(self):
        self.send_ok = False
        self.assertEqual(len(self.run_at(8, 0)), 1)
        self.assertEqual(len(self.run_at(8, 1)), 0)  # 2분 안에는 다시 안 보냄
        self.send_ok = True
        self.assertEqual(len(self.run_at(8, 2)), 1)
        self.assertEqual(len(self.run_at(8, 10)), 0)

    def test_no_notices_on_holiday(self):
        self.now = datetime(2026, 10, 23, 8, 0, tzinfo=KST)
        self.app.run_due_notices()
        self.assertEqual(self.sent, [])


class RosterFileTest(unittest.TestCase):
    def write(self, text, encoding):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode(encoding))
        self.addCleanup(os.remove, path)
        return path

    def test_reads_excel_csv_in_both_encodings(self):
        text = "번호,이름,직급,분류,팀,이메일,담당자\r\n1,홍길동,대리,생산팀,생산운영실,a@x.com,O\r\n2,김철수,부장,생산팀,,,\r\n"
        for encoding in ("utf-8-sig", "cp949"):
            rows = read_roster(self.write(text, encoding))
            self.assertEqual([(r["name"], r["manager"]) for r in rows], [("홍길동", True), ("김철수", False)])

    def test_rejects_duplicate_numbers(self):
        path = self.write("번호,이름,분류\n1,가,생산팀\n1,나,생산팀\n", "utf-8")
        with self.assertRaises(ValueError):
            read_roster(path)


if __name__ == "__main__":
    unittest.main()
