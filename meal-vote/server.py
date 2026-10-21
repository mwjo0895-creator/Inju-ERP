#!/usr/bin/env python3
"""인주공장 식수 투표 서버.

Python 표준 라이브러리만 사용하므로 별도로 설치할 패키지가 없다.
하루 흐름 (config.json 기본값, 점심 기준):

    08:00  아직 투표하지 않은 사람에게 Teams 개인 메시지 (먹음 / 안먹음 버튼)
    08:45  그래도 안 한 사람에게 한 번 더
    09:00  투표 마감 → 팀별 식수 담당자에게 1차 집계
    09:30  추가 인원 등록 마감 → 최종 인원을 Teams 채널에 올리고 업체에 메일

마감은 서버 시계로만 판단하며 관리자 계정이나 관리자 화면은 없다.
직원은 Teams로 받은 개인 링크로 들어오므로 로그인도 없다.

    python server.py                   서버 실행
    python server.py links             직원별 개인 링크 출력
    python server.py new-link 번호      개인 링크 새로 발급 (링크가 남에게 넘어갔을 때)
    python server.py send-test 메일주소  Teams 개인 메시지·채널·메일 연결 시험
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")
DEFAULT_ROSTER = os.path.join(BASE_DIR, "roster.csv")
DEFAULT_DB = os.path.join(BASE_DIR, "meal.db")
OUTBOX = os.path.join(BASE_DIR, "outbox.log")

STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/board": ("board.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

WEEKDAY_KO = "월화수목금토일"
ALL_DAYS = [1, 2, 3, 4, 5, 6, 7]
CHOICE_KO = {"eat": "먹음", "skip": "안먹음", None: "미투표"}
MANAGER_MARKS = {"O", "○", "Y", "1", "예", "담당자"}
SESSION_SECONDS = 365 * 86400
MAX_BODY = 10_000
MAX_EXTRA = 200
SCHEMA_VERSION = 2
RETRY_SECONDS = 120
FINAL_GRACE = timedelta(hours=1)
SCHEDULER_SECONDS = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,              -- roster.csv의 번호
    name       TEXT    NOT NULL,
    title      TEXT    NOT NULL DEFAULT '',
    grp        TEXT    NOT NULL,                 -- 분류 (식수 집계 단위)
    team       TEXT    NOT NULL DEFAULT '',
    email      TEXT    NOT NULL DEFAULT '',
    manager    INTEGER NOT NULL DEFAULT 0,       -- 팀별 식수 담당자
    active     INTEGER NOT NULL DEFAULT 1,
    link_token TEXT    NOT NULL UNIQUE           -- 개인 링크
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
-- 팀별 담당자가 등록하는 정기 인원 외 추가 인원 (방문객 등)
CREATE TABLE IF NOT EXISTS extras (
    meal_date  TEXT    NOT NULL,
    meal       TEXT    NOT NULL,
    grp        TEXT    NOT NULL,
    count      INTEGER NOT NULL CHECK (count >= 0),
    note       TEXT    NOT NULL DEFAULT '',
    updated_by INTEGER NOT NULL,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (meal_date, meal, grp)
);
-- 보낸 알림. 서버를 껐다 켜도 같은 알림을 두 번 보내지 않는다.
CREATE TABLE IF NOT EXISTS notices (
    meal_date TEXT NOT NULL,
    meal      TEXT NOT NULL,
    kind      TEXT NOT NULL,
    sent_at   TEXT NOT NULL,
    PRIMARY KEY (meal_date, meal, kind)
);
CREATE INDEX IF NOT EXISTS votes_by_date ON votes (meal_date, meal);
"""

LINK_NOT_FOUND = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>식수 투표</title>
<p style="font:16px/1.6 sans-serif;padding:24px">링크가 맞지 않습니다.<br>
Teams로 받은 가장 최근 메시지의 버튼을 눌러 주세요.</p>""".encode("utf-8")

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
        times = [meal["notifyAt"], meal["cutoff"], meal["finalAt"]]
        for t in times:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                raise ValueError(f"시각은 HH:MM 형식이어야 합니다: {t}")
        if not times[0] < times[1] <= times[2]:
            raise ValueError(f"{meal['name']}: notifyAt < cutoff <= finalAt 순서여야 합니다.")
    return cfg


def read_roster(path):
    """roster.csv를 읽는다. 엑셀에서 'CSV UTF-8'로 저장해도, 그냥 'CSV'(CP949)로 저장해도 된다."""
    with open(path, "rb") as f:
        raw = f.read()
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("roster.csv를 읽을 수 없습니다. 엑셀에서 'CSV UTF-8' 형식으로 다시 저장하세요.")
    reader = csv.DictReader(io.StringIO(text))
    missing = {"번호", "이름", "분류"} - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"roster.csv에 {', '.join(sorted(missing))} 열이 없습니다.")
    rows, seen = [], set()
    for line, rec in enumerate(reader, start=2):
        if not any((v or "").strip() for v in rec.values()):
            continue
        field = lambda key: (rec.get(key) or "").strip()
        if not field("번호").isdigit():
            raise ValueError(f"roster.csv {line}번째 줄: 번호는 숫자여야 합니다.")
        uid = int(field("번호"))
        if uid in seen:
            raise ValueError(f"roster.csv {line}번째 줄: 번호 {uid}이(가) 중복입니다.")
        seen.add(uid)
        if not field("이름") or not field("분류"):
            raise ValueError(f"roster.csv {line}번째 줄: 이름과 분류는 꼭 있어야 합니다.")
        rows.append({
            "id": uid,
            "name": field("이름"),
            "title": field("직급"),
            "grp": field("분류"),
            "team": field("팀"),
            "email": field("이메일"),
            "manager": field("담당자").upper() in MANAGER_MARKS,
        })
    if not rows:
        raise ValueError("roster.csv에 직원이 한 명도 없습니다.")
    return rows


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


def display_name(user):
    return f"{user['name']} {user['title']}" if user["title"] else user["name"]


def json_resp(data, status=200, headers=()):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return Response(status, [("Content-Type", "application/json; charset=utf-8"), *headers], body)


def csv_resp(rows, filename):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    # 엑셀에서 한글이 깨지지 않도록 BOM을 붙인다.
    return Response(200, [
        ("Content-Type", "text/csv; charset=utf-8"),
        ("Content-Disposition", f'attachment; filename="{filename}"'),
    ], ("﻿" + buf.getvalue()).encode("utf-8"))


def session_cookie(token, max_age):
    return ("Set-Cookie", f"sid={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}")


# ---- Teams 적응형 카드 ----

def card(body, actions=()):
    result = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
              "version": "1.4", "body": body}
    if actions:
        result["actions"] = list(actions)
    return result


def text_block(text, heading=False, subtle=False):
    block = {"type": "TextBlock", "text": text, "wrap": True}
    if heading:
        block.update(weight="Bolder", size="Medium")
    if subtle:
        block["isSubtle"] = True
    return block


def fact_set(pairs):
    return {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in pairs]}


def open_url(title, url):
    return {"type": "Action.OpenUrl", "title": title, "url": url}


class App:
    def __init__(self, config, db_path, clock=None, send=None, base_url="http://localhost:8080"):
        self.meals = config["meals"]
        self.meal_by_id = {m["id"]: m for m in self.meals}
        self.holidays = set(config.get("holidays", []))
        self.days_ahead = config.get("openDaysAhead", 7)
        self.remind = timedelta(minutes=config.get("remindMinutesBefore", 15))
        self.vendor = config.get("vendor") or {}
        self.site = config.get("siteName", "")
        self.tz = timezone(timedelta(hours=config.get("utcOffsetHours", 9)))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.send = send or (lambda payload: True)
        self.base_url = base_url.rstrip("/")
        self.groups = []  # roster.csv에 나온 순서대로의 분류 목록
        self.roster_mtime = None
        self.last_try = {}
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            tables = self.db.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
            if tables and version != SCHEMA_VERSION:
                raise RuntimeError("meal.db가 이전 버전 형식입니다. 서버를 끄고 meal.db를 지운 뒤 다시 실행하세요.")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(SCHEMA)
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def now(self):
        return self.clock().astimezone(self.tz)

    # ---- 명단 (roster.csv가 기준) ----

    def load_roster(self, path):
        self.sync_roster(read_roster(path))
        self.roster_mtime = os.path.getmtime(path)

    def reload_roster_if_changed(self, path):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if mtime == self.roster_mtime:
            return
        try:
            self.load_roster(path)
            print("[명단] roster.csv가 바뀌어 다시 읽었습니다.", file=sys.stderr)
        except ValueError as e:
            self.roster_mtime = mtime
            print(f"[명단] {e} 이전 명단을 그대로 씁니다.", file=sys.stderr)

    def sync_roster(self, rows):
        with self.lock, self.db:
            known = {r[0] for r in self.db.execute("SELECT id FROM users")}
            for r in rows:
                values = (r["name"], r["title"], r["grp"], r["team"], r["email"], int(r["manager"]))
                if r["id"] in known:
                    self.db.execute(
                        "UPDATE users SET name = ?, title = ?, grp = ?, team = ?, email = ?, manager = ?, active = 1"
                        " WHERE id = ?", (*values, r["id"]))
                else:
                    self.db.execute(
                        "INSERT INTO users (name, title, grp, team, email, manager, id, link_token)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (*values, r["id"], secrets.token_urlsafe(16)))
            # 명단에서 빠진 사람은 비활성화한다. 지난 투표 기록은 남는다.
            ids = [r["id"] for r in rows]
            marks = ",".join("?" * len(ids))
            self.db.execute(f"UPDATE users SET active = 0 WHERE id NOT IN ({marks})", ids)
            self.db.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE active = 0)")
        self.groups = list(dict.fromkeys(r["grp"] for r in rows))

    def active_users(self, where="", args=()):
        with self.lock:
            return self.db.execute(f"SELECT * FROM users WHERE active = 1 {where} ORDER BY id", args).fetchall()

    def personal_url(self, user, day=None, meal=None, choice=None):
        url = f"{self.base_url}/me/{user['link_token']}"
        if choice:
            url += "?" + urlencode({"d": day.isoformat(), "m": meal["id"], "c": choice})
        return url

    def new_link(self, uid):
        with self.lock, self.db:
            cur = self.db.execute("UPDATE users SET link_token = ? WHERE id = ? AND active = 1",
                                  (secrets.token_urlsafe(16), uid))
            self.db.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
            return self.db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone() if cur.rowcount else None

    # ---- 일정: 마감 판단은 전부 여기서, 서버 시각으로만 한다 ----

    def at(self, day, hhmm):
        hh, mm = map(int, hhmm.split(":"))
        return datetime(day.year, day.month, day.day, hh, mm, tzinfo=self.tz)

    def is_served(self, meal, day):
        return day.isoweekday() in meal.get("days", ALL_DAYS) and day.isoformat() not in self.holidays

    def beyond_window(self, day, now):
        return day > now.date() + timedelta(days=self.days_ahead)

    def vote_status(self, meal, day, now):
        if now >= self.at(day, meal["cutoff"]):
            return "closed"
        return "upcoming" if self.beyond_window(day, now) else "open"

    def extra_status(self, meal, day, now):
        if now >= self.at(day, meal["finalAt"]):
            return "closed"
        return "upcoming" if self.beyond_window(day, now) else "open"

    def phase(self, meal, day, now):
        if self.beyond_window(day, now):
            return "upcoming"
        if now < self.at(day, meal["cutoff"]):
            return "voting"
        if now < self.at(day, meal["finalAt"]):
            return "extras"
        return "final"

    # ---- 요청 처리 ----

    def handle(self, method, path, query, body, cookie_header):
        try:
            if method == "GET" and path in STATIC_ROUTES:
                return self.static(path)
            token = self.cookie_token(cookie_header)
            user = self.session_user(token)
            if method == "GET" and path.startswith("/me/"):
                return self.personal_link(path[len("/me/"):], query, user)
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
            if needs_login and user is None:
                raise ApiError(401, "Teams로 받은 메시지의 버튼으로 들어와 주세요.")
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

    def personal_link(self, link_token, query, current):
        """개인 링크: 누른 사람을 이 브라우저에 기억시키고, 버튼으로 온 경우 투표 화면에 넘겨준다.

        투표 자체는 화면의 스크립트가 POST로 한다. 링크 미리보기 같은 자동 요청이 대신 투표하지 않게 하려는 것.
        """
        with self.lock:
            user = self.db.execute("SELECT id FROM users WHERE link_token = ? AND active = 1",
                                   (link_token,)).fetchone()
        if user is None:
            return Response(404, [("Content-Type", "text/html; charset=utf-8")], LINK_NOT_FOUND)
        headers = [("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer")]
        if current is None or current["id"] != user["id"]:
            with self.lock, self.db:
                headers.append(session_cookie(self.new_session(user["id"]), SESSION_SECONDS))
        quick = {k: query[k][0] for k in ("d", "m", "c") if k in query}
        headers.append(("Location", "/?" + urlencode(quick) if len(quick) == 3 else "/"))
        return Response(303, headers, b"")

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
            (hash_token(token), user_id, now_ts + SESSION_SECONDS),
        )
        return token

    # ---- API ----

    def api_logout(self, req):
        if req.token:
            with self.lock, self.db:
                self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(req.token),))
        return json_resp({"ok": True}, headers=[session_cookie("", 0)])

    def api_me(self, req):
        u = req.user
        return {"name": u["name"], "title": u["title"], "grp": u["grp"], "team": u["team"],
                "manager": bool(u["manager"])}

    def api_slots(self, req):
        now = self.now()
        today = now.date().isoformat()
        u = req.user
        with self.lock:
            rows = self.db.execute(
                "SELECT meal_date, meal, choice FROM votes WHERE user_id = ? AND meal_date >= ?", (u["id"], today)
            ).fetchall()
            extra_rows = self.db.execute(
                "SELECT meal_date, meal, count, note FROM extras WHERE grp = ? AND meal_date >= ?", (u["grp"], today)
            ).fetchall()
        mine = {(r["meal_date"], r["meal"]): r["choice"] for r in rows}
        extras = {(r["meal_date"], r["meal"]): r for r in extra_rows}
        days = []
        for i in range(self.days_ahead + 1):
            day = now.date() + timedelta(days=i)
            meals = []
            for meal in self.meals:
                if not self.is_served(meal, day):
                    continue
                key = (day.isoformat(), meal["id"])
                item = {
                    "id": meal["id"],
                    "name": meal["name"],
                    "cutoff": meal["cutoff"],
                    "finalAt": meal["finalAt"],
                    "cutoffAt": epoch_ms(self.at(day, meal["cutoff"])),
                    "finalAtMs": epoch_ms(self.at(day, meal["finalAt"])),
                    "status": self.vote_status(meal, day, now),
                    "choice": mine.get(key),
                }
                if u["manager"]:
                    extra = extras.get(key)
                    item["extra"] = {
                        "status": self.extra_status(meal, day, now),
                        "count": extra["count"] if extra else 0,
                        "note": extra["note"] if extra else "",
                    }
                meals.append(item)
            days.append({"date": day.isoformat(), "label": day_label(day), "isToday": i == 0, "meals": meals})
        return {"serverTime": epoch_ms(now), "days": days}

    def parse_slot(self, data):
        meal = self.meal_by_id.get(data.get("meal"))
        day = parse_day(data.get("date"))
        if meal is None or day is None:
            raise ApiError(400, "잘못된 요청입니다.")
        if not self.is_served(meal, day):
            raise ApiError(400, "그날은 식사가 없습니다.")
        return meal, day

    def api_vote(self, req):
        meal, day = self.parse_slot(req.data)
        choice = req.data.get("choice")
        if choice not in ("eat", "skip"):
            raise ApiError(400, "잘못된 요청입니다.")
        with self.lock:
            now = self.now()
            status = self.vote_status(meal, day, now)
            if status == "closed":
                raise ApiError(403, f"{meal['name']} 투표가 마감되었습니다. ({meal['cutoff']} 마감)")
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

    def api_extra(self, req):
        if not req.user["manager"]:
            raise ApiError(403, "팀별 식수 담당자만 추가 인원을 등록할 수 있습니다.")
        meal, day = self.parse_slot(req.data)
        count = req.data.get("count")
        note = str(req.data.get("note") or "").strip()
        if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= MAX_EXTRA:
            raise ApiError(400, f"추가 인원은 0~{MAX_EXTRA} 사이 숫자로 입력하세요.")
        if len(note) > 200:
            raise ApiError(400, "특이사항은 200자 이내로 적어 주세요.")
        with self.lock:
            now = self.now()
            status = self.extra_status(meal, day, now)
            if status == "closed":
                raise ApiError(403, f"{meal['name']} 추가 인원 등록이 마감되었습니다. ({meal['finalAt']} 확정)")
            if status == "upcoming":
                raise ApiError(403, "아직 등록할 수 없는 날짜입니다.")
            with self.db:
                key = (day.isoformat(), meal["id"], req.user["grp"])
                if count == 0 and not note:
                    self.db.execute("DELETE FROM extras WHERE meal_date = ? AND meal = ? AND grp = ?", key)
                else:
                    self.db.execute(
                        "INSERT INTO extras (meal_date, meal, grp, count, note, updated_by, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT (meal_date, meal, grp) DO UPDATE SET count = excluded.count,"
                        " note = excluded.note, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                        (*key, count, note, req.user["id"], now.isoformat(timespec="seconds")),
                    )
        return {"ok": True, "count": count, "note": note}

    # ---- 집계 (현황판, CSV, 알림 공용) ----

    def day_data(self, day):
        with self.lock:
            # 명단에서 빠진 사람도 그날 응답이 있으면 기록대로 집계한다.
            users = self.db.execute(
                "SELECT id, name, title, grp, team FROM users"
                " WHERE active = 1 OR id IN (SELECT user_id FROM votes WHERE meal_date = ?) ORDER BY id",
                (day.isoformat(),),
            ).fetchall()
            votes = self.db.execute(
                "SELECT user_id, meal, choice FROM votes WHERE meal_date = ?", (day.isoformat(),)
            ).fetchall()
            extras = self.db.execute(
                "SELECT meal, grp, count, note FROM extras WHERE meal_date = ?", (day.isoformat(),)
            ).fetchall()
        choice_of = {(v["user_id"], v["meal"]): v["choice"] for v in votes}
        extra_of = {(e["meal"], e["grp"]): (e["count"], e["note"]) for e in extras}
        return users, choice_of, extra_of

    def summarize(self, day, meal, data, now):
        users, choice_of, extra_of = data
        order = list(self.groups)
        for g in [u["grp"] for u in users] + [g for (m, g) in extra_of if m == meal["id"]]:
            if g not in order:
                order.append(g)
        rows = {g: {"grp": g, "regular": 0, "extra": 0, "total": 0, "note": "", "eaters": []} for g in order}
        skip = 0
        for u in users:
            choice = choice_of.get((u["id"], meal["id"]))
            if choice == "eat":
                rows[u["grp"]]["regular"] += 1
                rows[u["grp"]]["eaters"].append(display_name(u))
            elif choice == "skip":
                skip += 1
        for (m, g), (count, note) in extra_of.items():
            if m == meal["id"]:
                rows[g]["extra"], rows[g]["note"] = count, note
        for r in rows.values():
            r["total"] = r["regular"] + r["extra"]
        regular = sum(r["regular"] for r in rows.values())
        extra = sum(r["extra"] for r in rows.values())
        return {
            "id": meal["id"],
            "name": meal["name"],
            "cutoff": meal["cutoff"],
            "finalAt": meal["finalAt"],
            "phase": self.phase(meal, day, now),
            "regular": regular,
            "extra": extra,
            "total": regular + extra,
            "skip": skip,
            "noResponse": len(users) - regular - skip,
            "groups": [rows[g] for g in order],
        }

    def query_day(self, query):
        raw = (query.get("date") or [None])[0]
        day = parse_day(raw) if raw else self.now().date()
        if day is None:
            raise ApiError(400, "날짜 형식이 잘못되었습니다.")
        return day

    def api_summary(self, req):
        day = self.query_day(req.query)
        now = self.now()
        data = self.day_data(day)
        return {
            "date": day.isoformat(),
            "label": day_label(day),
            "serverTime": epoch_ms(now),
            "registered": len(data[0]),
            "meals": [self.summarize(day, m, data, now) for m in self.meals if self.is_served(m, day)],
        }

    def api_export(self, req):
        month = (req.query.get("month") or [None])[0]
        if month:
            return self.export_month(month)
        day = self.query_day(req.query)
        users, choice_of, extra_of = self.day_data(day)
        order = {"eat": 0, "skip": 1, None: 2}
        rows = [["날짜", "식사", "분류", "팀", "이름", "응답", "비고"]]
        for meal in self.meals:
            if not self.is_served(meal, day):
                continue
            ranked = sorted(users, key=lambda u: (order[choice_of.get((u["id"], meal["id"]))], u["id"]))
            for u in ranked:
                choice = choice_of.get((u["id"], meal["id"]))
                rows.append([day.isoformat(), meal["name"], u["grp"], u["team"], display_name(u), CHOICE_KO[choice], ""])
            for (m, g), (count, note) in extra_of.items():
                if m == meal["id"]:
                    rows.append([day.isoformat(), meal["name"], g, "", "추가 인원", f"{count}명", note])
        return csv_resp(rows, f"meal_{day.isoformat()}.csv")

    def export_month(self, month):
        """월별 식대 정산용: 확정된 끼니만 날짜·식사·분류별 인원으로 내보낸다."""
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ApiError(400, "월 형식이 잘못되었습니다. (예: 2026-10)")
        now = self.now()
        rows = [["날짜", "요일", "식사", "분류", "정기", "추가", "합계", "특이사항"]]
        day = date.fromisoformat(month + "-01")
        while day.strftime("%Y-%m") == month:
            data = None
            for meal in self.meals:
                if not self.is_served(meal, day) or self.phase(meal, day, now) != "final":
                    continue
                data = data or self.day_data(day)
                for g in self.summarize(day, meal, data, now)["groups"]:
                    if g["total"]:
                        rows.append([day.isoformat(), WEEKDAY_KO[day.weekday()], meal["name"], g["grp"],
                                     g["regular"], g["extra"], g["total"], g["note"]])
            day += timedelta(days=1)
        return csv_resp(rows, f"meal_{month}.csv")

    # ---- 자동 알림 ----

    def due_notices(self, now):
        """지금 보내야 하는 알림 (날짜, 식사, 종류) 목록.

        서버가 잠깐 꺼져 있었으면 켜진 뒤 보내되, 시간대를 지난 알림은 보내지 않는다.
        확정 통보는 1시간까지만 늦게 보낸다 (처음 설치한 날 오후에 켜도 업체에 0명 메일이 가지 않게).
        """
        day = now.date()
        due = []
        for meal in self.meals:
            if not self.is_served(meal, day):
                continue
            notify, cutoff, final = (self.at(day, meal[k]) for k in ("notifyAt", "cutoff", "finalAt"))
            remind = max(cutoff - self.remind, notify) if self.remind else cutoff
            windows = [("open", notify, remind), ("remind", remind, cutoff),
                       ("tally", cutoff, final), ("final", final, final + FINAL_GRACE)]
            due += [(day, meal, kind) for kind, start, end in windows if start <= now < end]
        return due

    def run_due_notices(self):
        now = self.now()
        for day, meal, kind in self.due_notices(now):
            key = (day.isoformat(), meal["id"], kind)
            with self.lock:
                if self.db.execute("SELECT 1 FROM notices WHERE meal_date = ? AND meal = ? AND kind = ?", key).fetchone():
                    continue
            last = self.last_try.get(key)
            if last and (now - last).total_seconds() < RETRY_SECONDS:
                continue
            self.last_try[key] = now
            payload = self.build_notice(day, meal, kind, now)
            if payload is not None and not self.send(payload):
                print(f"[알림] {day_label(day)} {meal['name']} {kind} 보내기 실패, 잠시 뒤 다시 시도합니다.", file=sys.stderr)
                continue
            with self.lock, self.db:
                self.db.execute("INSERT OR IGNORE INTO notices (meal_date, meal, kind, sent_at) VALUES (?, ?, ?, ?)",
                                (*key, now.isoformat(timespec="seconds")))
            print(f"[알림] {day_label(day)} {meal['name']} {kind} 완료", file=sys.stderr)

    def build_notice(self, day, meal, kind, now):
        if kind in ("open", "remind"):
            return self.vote_request(day, meal, kind)
        summary = self.summarize(day, meal, self.day_data(day), now)
        if kind == "tally":
            return self.tally_notice(day, meal, summary)
        return self.final_notice(day, meal, summary)

    def payload(self, kind, day, meal, chat=(), channel=(), email=(), records=()):
        # 받는 쪽(Power Automate)이 단순하도록 네 목록은 비어 있어도 항상 보낸다.
        return {"kind": kind, "date": day.isoformat(), "meal": meal["name"], "teamsChat": list(chat),
                "teamsChannel": list(channel), "email": list(email), "records": list(records)}

    def vote_request(self, day, meal, kind):
        users = self.active_users(
            "AND email != '' AND id NOT IN (SELECT user_id FROM votes WHERE meal_date = ? AND meal = ?)",
            (day.isoformat(), meal["id"]),
        )
        if not users:
            return None
        when = f"{day_label(day)} {meal['name']}"
        if kind == "open":
            title, text = f"{when} 식사 투표", f"{meal['cutoff']}까지 눌러 주세요. 투표하지 않으면 식사가 준비되지 않습니다."
        else:
            title, text = f"{when} 투표가 곧 마감됩니다", f"아직 투표하지 않으셨습니다. {meal['cutoff']}에 마감됩니다."
        chats = [{
            "to": u["email"],
            "card": card([text_block(title, heading=True), text_block(text)], [
                open_url("먹음", self.personal_url(u, day, meal, "eat")),
                open_url("안먹음", self.personal_url(u, day, meal, "skip")),
                open_url("다른 날도 미리 투표", self.personal_url(u)),
            ]),
        } for u in users]
        return self.payload(kind, day, meal, chat=chats)

    def tally_notice(self, day, meal, summary):
        managers = self.active_users("AND manager = 1 AND email != ''")
        if not managers:
            return None
        facts = fact_set([(g["grp"], f"{g['regular']}명") for g in summary["groups"]])
        chats = [{
            "to": m["email"],
            "card": card([
                text_block(f"{day_label(day)} {meal['name']} 1차 집계: 정기 {summary['regular']}명", heading=True),
                facts,
                text_block(f"{m['grp']} 추가 인원(방문객 등)이 있으면 {meal['finalAt']}까지 등록해 주세요. "
                           "없으면 그대로 두시면 됩니다."),
            ], [open_url("추가 인원 등록", self.personal_url(m))]),
        } for m in managers]
        return self.payload("tally", day, meal, chat=chats)

    def final_notice(self, day, meal, s):
        to = self.vendor.get("to") or []
        groups = s["groups"]
        body = [
            text_block(f"{day_label(day)} {meal['name']} 식수 최종 {s['total']}명", heading=True),
            text_block(f"정기 {s['regular']}명 + 추가 {s['extra']}명" + (" · 업체에 메일로 보냈습니다." if to else ""),
                       subtle=True),
            fact_set([(g["grp"], f"{g['total']}명" + (f" (추가 {g['extra']})" if g["extra"] else "")) for g in groups]),
        ]
        body += [text_block(f"{g['grp']} 특이사항: {g['note']}", subtle=True) for g in groups if g["note"]]
        channel = [{"card": card(body, [open_url("현황판", f"{self.base_url}/board")])}]
        email = []
        if to:
            prefix = f"[{self.site}] " if self.site else ""
            email = [{
                "to": ";".join(to),
                "cc": ";".join(self.vendor.get("cc") or []),
                "subject": f"{prefix}{day_label(day)} {meal['name']} 식수 {s['total']}명",
                "html": self.vendor_html(day, meal, s),
            }]
        records = [{"date": day.isoformat(), "meal": meal["name"], "group": g["grp"], "regular": g["regular"],
                    "extra": g["extra"], "total": g["total"], "note": g["note"]} for g in groups]
        return self.payload("final", day, meal, channel=channel, email=email, records=records)

    def vendor_html(self, day, meal, s):
        cell = 'style="border:1px solid #999;padding:6px 10px"'
        num = 'style="border:1px solid #999;padding:6px 10px;text-align:right"'
        rows = "".join(
            f"<tr><td {cell}>{escape(g['grp'])}</td><td {num}>{g['regular']}</td><td {num}>{g['extra']}</td>"
            f"<td {num}><b>{g['total']}</b></td><td {cell}>{escape(g['note'])}</td></tr>"
            for g in s["groups"]
        )
        return (
            f"<p>안녕하세요. {escape(self.site)} {day_label(day)} {escape(meal['name'])} 식수 인원을 알려드립니다.</p>"
            f"<p style=\"font-size:18px\"><b>총 {s['total']}명</b> (정기 {s['regular']}명 + 추가 {s['extra']}명)</p>"
            '<table style="border-collapse:collapse">'
            f"<tr><th {cell}>구분</th><th {cell}>정기</th><th {cell}>추가</th><th {cell}>합계</th><th {cell}>특이사항</th></tr>"
            f"{rows}"
            f"<tr><td {cell}><b>합계</b></td><td {num}>{s['regular']}</td><td {num}>{s['extra']}</td>"
            f"<td {num}><b>{s['total']}</b></td><td {cell}></td></tr></table>"
            f"<p style=\"color:#666\">이 메일은 식수 투표 시스템에서 {meal['finalAt']}에 자동으로 보냈습니다.</p>"
        )

    def test_payload(self, email):
        today = self.now().date().isoformat()
        return {
            "kind": "test", "date": today, "meal": "",
            "teamsChat": [{"to": email, "card": card([text_block("식수 투표 연결 시험", heading=True),
                                                       text_block("이 메시지가 보이면 Teams 개인 메시지가 연결된 것입니다.")])}],
            "teamsChannel": [{"card": card([text_block("식수 투표 연결 시험", heading=True),
                                            text_block("이 메시지가 보이면 채널 게시가 연결된 것입니다.")])}],
            "email": [{"to": email, "cc": "", "subject": "식수 투표 연결 시험",
                       "html": "<p>이 메일이 보이면 업체 메일 발송이 연결된 것입니다.</p>"}],
            "records": [],
        }


# (메서드 이름, 로그인 필요 여부)
ROUTES = {
    ("POST", "/api/logout"): ("api_logout", False),
    ("GET", "/api/me"): ("api_me", True),
    ("GET", "/api/slots"): ("api_slots", True),
    ("POST", "/api/vote"): ("api_vote", True),
    ("POST", "/api/extra"): ("api_extra", True),
    ("GET", "/api/summary"): ("api_summary", False),
    ("GET", "/api/export.csv"): ("api_export", False),
}


def make_sender(url, outbox_path):
    """알림을 Power Automate 워크플로 주소로 보낸다. 주소가 비어 있으면 outbox.log에 적기만 한다."""
    def send(payload):
        data = json.dumps(payload, ensure_ascii=False)
        if not url:
            with open(outbox_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {data}\n")
            return True
        request = urllib.request.Request(url, data=data.encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, OSError) as e:
            print(f"[알림] 워크플로 호출 실패: {e}", file=sys.stderr)
            return False
    return send


def scheduler_loop(app, roster_path):
    while True:
        try:
            app.reload_roster_if_changed(roster_path)
            app.run_due_notices()
        except Exception:
            traceback.print_exc()
        time.sleep(SCHEDULER_SECONDS)


class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = "MealVote/2.0"

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
        if self.path.startswith("/api/summary"):  # 현황판 자동 새로고침은 로그에서 뺀다
            return
        line = re.sub(r"/me/[^ ?\"]+", "/me/***", fmt % args)  # 개인 링크는 로그에 남기지 않는다
        sys.stderr.write(f"[{self.log_date_time_string()}] {self.address_string()} {line}\n")


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # 실제로 패킷을 보내지는 않는다
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description="인주공장 식수 투표 서버")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="설정 파일 (기본: config.json)")
    parser.add_argument("--roster", default=DEFAULT_ROSTER, help="직원 명단 (기본: roster.csv)")
    parser.add_argument("--db", default=DEFAULT_DB, help="데이터베이스 파일 (기본: meal.db)")
    parser.add_argument("--port", type=int, help="포트 (기본: 설정 파일의 port)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("links", help="직원별 개인 링크 출력")
    p_new = sub.add_parser("new-link", help="개인 링크 새로 발급")
    p_new.add_argument("number", type=int)
    p_test = sub.add_parser("send-test", help="Teams 개인 메시지·채널·메일 연결 시험")
    p_test.add_argument("email")
    args = parser.parse_args()

    config = load_config(args.config)
    port = args.port or config.get("port", 8080)
    base_url = config.get("baseUrl") or f"http://{lan_ip()}:{port}"
    webhook = config.get("webhookUrl", "")
    if not os.path.exists(args.roster):
        sys.exit("roster.csv가 없습니다. roster.example.csv를 복사해 roster.csv를 만들고 명단을 채우세요.")
    try:
        app = App(config, args.db, send=make_sender(webhook, OUTBOX), base_url=base_url)
        app.load_roster(args.roster)
    except (RuntimeError, ValueError) as e:
        sys.exit(str(e))

    if args.command == "links":
        for u in app.active_users():
            print(f"{u['id']:>3}  {u['grp']}  {display_name(u)}  {app.personal_url(u)}")
        return
    if args.command == "new-link":
        u = app.new_link(args.number)
        print(f"{display_name(u)}의 새 링크: {app.personal_url(u)}" if u else "해당 번호가 명단에 없습니다.")
        return
    if args.command == "send-test":
        ok = app.send(app.test_payload(args.email))
        print("보냈습니다. Teams와 메일함을 확인하세요." if ok and webhook else
              "보냈습니다(outbox.log에 기록)." if ok else "보내지 못했습니다. 위 오류를 확인하세요.")
        return

    missing = [display_name(u) for u in app.active_users("AND email = ''")]
    Handler.app = app
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=scheduler_loop, args=(app, args.roster), daemon=True).start()
    print(f"식수 투표 서버 실행 중 (서버 시각 {app.now():%Y-%m-%d %H:%M})")
    print(f"  직원 화면 : {base_url}/  (각자 Teams로 받은 개인 링크로 들어옵니다)")
    print(f"  현황판    : {base_url}/board")
    print(f"  Teams 알림: {'켜짐' if webhook else '꺼짐 - config.json의 webhookUrl이 비어 있어 outbox.log에 기록만 합니다'}")
    if missing:
        print(f"  이메일이 없어 Teams 메시지를 못 받는 사람 {len(missing)}명: {', '.join(missing)}")
    print("  종료하려면 Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
