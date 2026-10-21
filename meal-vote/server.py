#!/usr/bin/env python3
"""사내 식수 투표 서버.

Python 표준 라이브러리만 사용하므로 별도로 설치할 패키지가 없다.
투표 마감은 서버 시계로만 판단한다. 마감 시각이 지나면 관리자를 포함한
누구도 앱을 통해 투표를 바꿀 수 없고, 관리자 계정이나 관리자 화면도 없다.

    python server.py                        서버 실행
    python server.py reset-pin 사번 새PIN    PIN을 잊은 직원의 PIN 재설정
    python server.py remove-user 사번        퇴사자 처리 (과거 기록은 보존)
"""

import argparse
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import traceback
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")
DEFAULT_DB = os.path.join(BASE_DIR, "meal.db")

STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/board": ("board.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

WEEKDAY_KO = "월화수목금토일"
ALL_DAYS = [1, 2, 3, 4, 5, 6, 7]
CHOICE_KO = {"eat": "먹음", "skip": "안먹음", None: "미응답"}
PIN_ITERATIONS = 100_000
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 10
MAX_BODY = 10_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    emp_no       TEXT    NOT NULL UNIQUE,
    name         TEXT    NOT NULL,
    dept         TEXT    NOT NULL,
    pin_hash     TEXT    NOT NULL,
    pin_salt     TEXT    NOT NULL,
    failed       INTEGER NOT NULL DEFAULT 0,
    locked_until INTEGER,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS votes (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    meal_date  TEXT    NOT NULL,
    meal       TEXT    NOT NULL,
    choice     TEXT    NOT NULL CHECK (choice IN ('eat', 'skip')),
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (user_id, meal_date, meal)
);
-- 누가 언제 무엇으로 바꿨는지 남기는 이력. 앱에는 수정/삭제 기능이 없다.
CREATE TABLE IF NOT EXISTS vote_log (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL,
    meal_date TEXT    NOT NULL,
    meal      TEXT    NOT NULL,
    choice    TEXT    NOT NULL,
    at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS votes_by_date ON votes (meal_date, meal);
"""

Response = namedtuple("Response", "status headers body")
Request = namedtuple("Request", "data query user token")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    seen = set()
    for meal in cfg["meals"]:
        if not re.fullmatch(r"[a-z0-9_]+", meal["id"]) or meal["id"] in seen:
            raise ValueError(f"식사 id가 잘못되었거나 중복입니다: {meal['id']}")
        seen.add(meal["id"])
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", meal["cutoff"]):
            raise ValueError(f"마감 시각은 HH:MM 형식이어야 합니다: {meal['cutoff']}")
        if meal.get("cutoffDayOffset", 0) > 0:
            raise ValueError(f"cutoffDayOffset은 0(당일) 또는 음수(며칠 전)여야 합니다: {meal['id']}")
    return cfg


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PIN_ITERATIONS).hex()


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def epoch_ms(dt):
    return int(dt.timestamp() * 1000)


def day_label(day):
    return f"{day.month}/{day.day} ({WEEKDAY_KO[day.weekday()]})"


def parse_day(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def json_resp(data, status=200, headers=()):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return Response(status, [("Content-Type", "application/json; charset=utf-8"), *headers], body)


def session_cookie(token, max_age):
    return ("Set-Cookie", f"sid={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}")


class App:
    def __init__(self, config, db_path, clock=None):
        self.meals = config["meals"]
        self.meal_by_id = {m["id"]: m for m in self.meals}
        self.holidays = set(config.get("holidays", []))
        self.departments = config.get("departments", [])
        self.days_ahead = config.get("openDaysAhead", 7)
        self.session_seconds = config.get("sessionDays", 30) * 86400
        self.tz = timezone(timedelta(hours=config.get("utcOffsetHours", 9)))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(SCHEMA)

    def now(self):
        return self.clock().astimezone(self.tz)

    # ---- 일정: 마감 판단은 전부 여기서, 서버 시각으로만 한다 ----

    def cutoff_at(self, meal, day):
        hh, mm = map(int, meal["cutoff"].split(":"))
        cut_day = day + timedelta(days=meal.get("cutoffDayOffset", 0))
        return datetime(cut_day.year, cut_day.month, cut_day.day, hh, mm, tzinfo=self.tz)

    def cutoff_label(self, meal):
        offset = meal.get("cutoffDayOffset", 0)
        when = "당일" if offset == 0 else "전날" if offset == -1 else f"{-offset}일 전"
        return f"{when} {meal['cutoff']} 마감"

    def is_served(self, meal, day):
        return day.isoweekday() in meal.get("days", ALL_DAYS) and day.isoformat() not in self.holidays

    def status(self, meal, day, now):
        if now >= self.cutoff_at(meal, day):
            return "closed"
        if day > now.date() + timedelta(days=self.days_ahead):
            return "upcoming"
        return "open"

    # ---- 요청 처리 ----

    def handle(self, method, path, query, body, cookie_header):
        try:
            if method == "GET" and path in STATIC_ROUTES:
                return self.static(path)
            route = ROUTES.get((method, path))
            if route is None:
                raise ApiError(404, "찾을 수 없는 주소입니다.")
            name, needs_login = route
            data = {}
            if body:
                try:
                    data = json.loads(body)
                except ValueError:
                    raise ApiError(400, "잘못된 요청입니다.")
                if not isinstance(data, dict):
                    raise ApiError(400, "잘못된 요청입니다.")
            token = self.cookie_token(cookie_header)
            user = self.session_user(token)
            if needs_login and user is None:
                raise ApiError(401, "로그인이 필요합니다.")
            result = getattr(self, name)(Request(data, query, user, token))
        except ApiError as e:
            return json_resp({"error": e.message}, e.status)
        except Exception:
            traceback.print_exc()
            return json_resp({"error": "서버 오류가 발생했습니다."}, 500)
        return result if isinstance(result, Response) else json_resp(result)

    def static(self, path):
        filename, content_type = STATIC_ROUTES[path]
        with open(os.path.join(STATIC_DIR, filename), "rb") as f:
            return Response(200, [("Content-Type", content_type), ("Cache-Control", "no-cache")], f.read())

    def cookie_token(self, cookie_header):
        if not cookie_header:
            return None
        try:
            morsel = SimpleCookie(cookie_header).get("sid")
        except CookieError:
            return None
        return morsel.value if morsel else None

    def session_user(self, token):
        if not token:
            return None
        with self.lock:
            return self.db.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1",
                (hash_token(token), int(self.now().timestamp())),
            ).fetchone()

    def new_session(self, user_id):
        """호출하는 쪽에서 lock과 트랜잭션을 잡고 있어야 한다."""
        token = secrets.token_urlsafe(32)
        now_ts = int(self.now().timestamp())
        self.db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_ts,))
        self.db.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (hash_token(token), user_id, now_ts + self.session_seconds),
        )
        return token

    # ---- API ----

    def api_config(self, req):
        return {
            "serverTime": epoch_ms(self.now()),
            "departments": self.departments,
            "meals": [{"id": m["id"], "name": m["name"], "cutoffLabel": self.cutoff_label(m)} for m in self.meals],
        }

    def api_register(self, req):
        emp_no = str(req.data.get("empNo", "")).strip()
        name = str(req.data.get("name", "")).strip()
        dept = str(req.data.get("dept", "")).strip()
        pin = str(req.data.get("pin", ""))
        if not re.fullmatch(r"[0-9A-Za-z-]{1,20}", emp_no):
            raise ApiError(400, "사번은 영문·숫자 20자 이내로 입력하세요.")
        if not 1 <= len(name) <= 20:
            raise ApiError(400, "이름을 입력하세요.")
        if not 1 <= len(dept) <= 30 or (self.departments and dept not in self.departments):
            raise ApiError(400, "부서를 선택하세요.")
        if not re.fullmatch(r"\d{4,8}", pin):
            raise ApiError(400, "PIN은 숫자 4~8자리로 정하세요.")
        salt = secrets.token_bytes(16)
        pin_hash = hash_pin(pin, salt)
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM users WHERE emp_no = ?", (emp_no,)).fetchone():
                raise ApiError(409, "이미 등록된 사번입니다. 로그인하세요.")
            cur = self.db.execute(
                "INSERT INTO users (emp_no, name, dept, pin_hash, pin_salt, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (emp_no, name, dept, pin_hash, salt.hex(), self.now().isoformat(timespec="seconds")),
            )
            token = self.new_session(cur.lastrowid)
        return json_resp({"ok": True}, headers=[session_cookie(token, self.session_seconds)])

    def api_login(self, req):
        emp_no = str(req.data.get("empNo", "")).strip()
        pin = str(req.data.get("pin", ""))
        now_ts = int(self.now().timestamp())
        with self.lock:
            user = self.db.execute("SELECT * FROM users WHERE emp_no = ? AND active = 1", (emp_no,)).fetchone()
        if user is None:
            raise ApiError(401, "사번 또는 PIN이 맞지 않습니다.")
        if user["locked_until"] and user["locked_until"] > now_ts:
            minutes = math.ceil((user["locked_until"] - now_ts) / 60)
            raise ApiError(429, f"PIN을 여러 번 틀려 잠겼습니다. {minutes}분 뒤에 다시 시도하세요.")
        ok = hmac.compare_digest(hash_pin(pin, bytes.fromhex(user["pin_salt"])), user["pin_hash"])
        with self.lock, self.db:
            if ok:
                self.db.execute("UPDATE users SET failed = 0, locked_until = NULL WHERE id = ?", (user["id"],))
                token = self.new_session(user["id"])
            else:
                self.db.execute("UPDATE users SET failed = failed + 1 WHERE id = ?", (user["id"],))
                failed = self.db.execute("SELECT failed FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
                if failed >= MAX_FAILED_LOGINS:
                    self.db.execute(
                        "UPDATE users SET failed = 0, locked_until = ? WHERE id = ?",
                        (now_ts + LOCK_MINUTES * 60, user["id"]),
                    )
        if not ok:
            if failed >= MAX_FAILED_LOGINS:
                raise ApiError(429, f"PIN을 {MAX_FAILED_LOGINS}번 틀려 {LOCK_MINUTES}분간 잠깁니다.")
            raise ApiError(401, f"사번 또는 PIN이 맞지 않습니다. (남은 시도 {MAX_FAILED_LOGINS - failed}회)")
        return json_resp({"ok": True}, headers=[session_cookie(token, self.session_seconds)])

    def api_logout(self, req):
        if req.token:
            with self.lock, self.db:
                self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(req.token),))
        return json_resp({"ok": True}, headers=[session_cookie("", 0)])

    def api_me(self, req):
        u = req.user
        return {"empNo": u["emp_no"], "name": u["name"], "dept": u["dept"]}

    def api_slots(self, req):
        now = self.now()
        today = now.date()
        with self.lock:
            rows = self.db.execute(
                "SELECT meal_date, meal, choice FROM votes WHERE user_id = ? AND meal_date >= ?",
                (req.user["id"], today.isoformat()),
            ).fetchall()
        mine = {(r["meal_date"], r["meal"]): r["choice"] for r in rows}
        days = []
        for i in range(self.days_ahead + 1):
            day = today + timedelta(days=i)
            meals = [
                {
                    "id": meal["id"],
                    "name": meal["name"],
                    "cutoffAt": epoch_ms(self.cutoff_at(meal, day)),
                    "cutoffLabel": self.cutoff_label(meal),
                    "status": self.status(meal, day, now),
                    "choice": mine.get((day.isoformat(), meal["id"])),
                }
                for meal in self.meals
                if self.is_served(meal, day)
            ]
            days.append({"date": day.isoformat(), "label": day_label(day), "isToday": i == 0, "meals": meals})
        return {"serverTime": epoch_ms(now), "days": days}

    def api_vote(self, req):
        meal = self.meal_by_id.get(req.data.get("meal"))
        day = parse_day(req.data.get("date"))
        choice = req.data.get("choice")
        if meal is None or day is None or choice not in ("eat", "skip"):
            raise ApiError(400, "잘못된 요청입니다.")
        if not self.is_served(meal, day):
            raise ApiError(400, "그날은 식사가 없습니다.")
        with self.lock:
            now = self.now()
            status = self.status(meal, day, now)
            if status == "closed":
                raise ApiError(403, f"{meal['name']} 투표가 마감되었습니다. ({self.cutoff_label(meal)})")
            if status == "upcoming":
                raise ApiError(403, "아직 투표가 열리지 않은 날짜입니다.")
            stamp = now.isoformat(timespec="seconds")
            with self.db:
                self.db.execute(
                    "INSERT INTO votes (user_id, meal_date, meal, choice, updated_at) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT (user_id, meal_date, meal) DO UPDATE"
                    " SET choice = excluded.choice, updated_at = excluded.updated_at",
                    (req.user["id"], day.isoformat(), meal["id"], choice, stamp),
                )
                self.db.execute(
                    "INSERT INTO vote_log (user_id, meal_date, meal, choice, at) VALUES (?, ?, ?, ?, ?)",
                    (req.user["id"], day.isoformat(), meal["id"], choice, stamp),
                )
        return {"ok": True, "choice": choice}

    def day_records(self, query):
        """현황판·CSV 공용: 그날 집계 대상 직원과 응답을 읽는다."""
        raw = (query.get("date") or [None])[0]
        day = parse_day(raw) if raw else self.now().date()
        if day is None:
            raise ApiError(400, "날짜 형식이 잘못되었습니다.")
        with self.lock:
            # 퇴사 처리된 직원도 그날 응답이 있으면 기록대로 집계한다.
            users = self.db.execute(
                "SELECT id, emp_no, name, dept FROM users"
                " WHERE active = 1 OR id IN (SELECT user_id FROM votes WHERE meal_date = ?)"
                " ORDER BY dept, name",
                (day.isoformat(),),
            ).fetchall()
            votes = self.db.execute(
                "SELECT user_id, meal, choice FROM votes WHERE meal_date = ?", (day.isoformat(),)
            ).fetchall()
        choice_of = {(v["user_id"], v["meal"]): v["choice"] for v in votes}
        served = [m for m in self.meals if self.is_served(m, day)]
        return day, users, choice_of, served

    def api_summary(self, req):
        day, users, choice_of, served = self.day_records(req.query)
        now = self.now()
        meals = []
        for meal in served:
            eaters, skip, by_dept = [], 0, {}
            for u in users:
                choice = choice_of.get((u["id"], meal["id"]))
                if choice == "eat":
                    eaters.append({"name": u["name"], "dept": u["dept"]})
                    by_dept[u["dept"]] = by_dept.get(u["dept"], 0) + 1
                elif choice == "skip":
                    skip += 1
            meals.append({
                "id": meal["id"],
                "name": meal["name"],
                "cutoffAt": epoch_ms(self.cutoff_at(meal, day)),
                "cutoffLabel": self.cutoff_label(meal),
                "status": self.status(meal, day, now),
                "eat": len(eaters),
                "skip": skip,
                "noResponse": len(users) - len(eaters) - skip,
                "byDept": [{"dept": d, "eat": n} for d, n in sorted(by_dept.items())],
                "eaters": eaters,
            })
        return {
            "date": day.isoformat(),
            "label": day_label(day),
            "serverTime": epoch_ms(now),
            "registered": len(users),
            "meals": meals,
        }

    def api_export(self, req):
        day, users, choice_of, served = self.day_records(req.query)
        order = {"eat": 0, "skip": 1, None: 2}
        buf = io.StringIO()
        out = csv.writer(buf)
        out.writerow(["날짜", "식사", "부서", "사번", "이름", "응답"])
        for meal in served:
            rows = sorted(users, key=lambda u: (order[choice_of.get((u["id"], meal["id"]))], u["dept"], u["name"]))
            for u in rows:
                choice = choice_of.get((u["id"], meal["id"]))
                out.writerow([day.isoformat(), meal["name"], u["dept"], u["emp_no"], u["name"], CHOICE_KO[choice]])
        # 엑셀에서 한글이 깨지지 않도록 BOM을 붙인다.
        body = ("﻿" + buf.getvalue()).encode("utf-8")
        return Response(200, [
            ("Content-Type", "text/csv; charset=utf-8"),
            ("Content-Disposition", f'attachment; filename="meal_{day.isoformat()}.csv"'),
        ], body)

    # ---- 명령줄 전용 (웹에는 노출하지 않음) ----

    def reset_pin(self, emp_no, pin):
        if not re.fullmatch(r"\d{4,8}", pin):
            raise ValueError("PIN은 숫자 4~8자리여야 합니다.")
        salt = secrets.token_bytes(16)
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE users SET pin_hash = ?, pin_salt = ?, failed = 0, locked_until = NULL WHERE emp_no = ?",
                (hash_pin(pin, salt), salt.hex(), emp_no),
            )
            self.db.execute(
                "DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE emp_no = ?)", (emp_no,)
            )
        return cur.rowcount == 1

    def remove_user(self, emp_no):
        with self.lock, self.db:
            cur = self.db.execute("UPDATE users SET active = 0 WHERE emp_no = ? AND active = 1", (emp_no,))
            self.db.execute(
                "DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE emp_no = ?)", (emp_no,)
            )
        return cur.rowcount == 1


# (메서드 이름, 로그인 필요 여부)
ROUTES = {
    ("GET", "/api/config"): ("api_config", False),
    ("POST", "/api/register"): ("api_register", False),
    ("POST", "/api/login"): ("api_login", False),
    ("POST", "/api/logout"): ("api_logout", False),
    ("GET", "/api/me"): ("api_me", True),
    ("GET", "/api/slots"): ("api_slots", True),
    ("POST", "/api/vote"): ("api_vote", True),
    ("GET", "/api/summary"): ("api_summary", False),
    ("GET", "/api/export.csv"): ("api_export", False),
}


class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = "MealVote/1.0"

    def run(self, method):
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            resp = json_resp({"error": "잘못된 요청입니다."}, 400)
        else:
            body = self.rfile.read(length) if length else b""
            resp = self.app.handle(method, url.path, parse_qs(url.query), body, self.headers.get("Cookie"))
        self.send_response(resp.status)
        for key, value in resp.headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(resp.body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(resp.body)

    def do_GET(self):
        self.run("GET")

    def do_POST(self):
        self.run("POST")

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/summary"):  # 현황판 자동 새로고침은 로그에서 뺀다
            sys.stderr.write(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}\n")


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # 실제로 패킷을 보내지는 않는다
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description="사내 식수 투표 서버")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="설정 파일 경로 (기본: config.json)")
    parser.add_argument("--db", default=DEFAULT_DB, help="데이터베이스 파일 경로 (기본: meal.db)")
    parser.add_argument("--port", type=int, help="포트 (기본: 설정 파일의 port)")
    sub = parser.add_subparsers(dest="command")
    p_reset = sub.add_parser("reset-pin", help="PIN을 잊은 직원의 PIN 재설정")
    p_reset.add_argument("emp_no")
    p_reset.add_argument("pin")
    p_remove = sub.add_parser("remove-user", help="퇴사자 처리 (과거 기록은 보존)")
    p_remove.add_argument("emp_no")
    args = parser.parse_args()

    config = load_config(args.config)
    app = App(config, args.db)

    if args.command == "reset-pin":
        print("PIN을 재설정했습니다." if app.reset_pin(args.emp_no, args.pin) else "해당 사번이 없습니다.")
        return
    if args.command == "remove-user":
        print("퇴사 처리했습니다." if app.remove_user(args.emp_no) else "해당 사번이 없거나 이미 처리되었습니다.")
        return

    port = args.port or config.get("port", 8080)
    Handler.app = app
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"식수 투표 서버 실행 중 (서버 시각 {app.now():%Y-%m-%d %H:%M})")
    print(f"  직원 투표 화면 : http://{lan_ip()}:{port}/")
    print(f"  식수 현황판    : http://{lan_ip()}:{port}/board")
    print("  종료하려면 Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
