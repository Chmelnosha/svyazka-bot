#!/usr/bin/env python3
"""СВЯЗКА quiz bot. Python 3.11+, standard library only."""
import argparse
import html
import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import struct
import zlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from diagnosis import diagnose

ROOT = Path(__file__).resolve().parent
CONTENT = json.loads((ROOT / "content.json").read_text(encoding="utf-8"))
LOG = logging.getLogger("svyazka")
ANSWER_MARKS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")
BLUE = (31, 96, 205)
DARK = (24, 53, 92)
LIGHT = (238, 246, 255)
GRID = (222, 235, 250)
WHITE = (255, 255, 255)


def _fill_rect(buf, w, h, x0, y0, x1, y1, color):
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    row = bytes(color) * max(0, x1 - x0)
    for y in range(y0, y1):
        off = (y * w + x0) * 3
        buf[off:off + len(row)] = row


def _pixel(buf, w, h, x, y, color):
    if 0 <= x < w and 0 <= y < h:
        off = (y * w + x) * 3
        buf[off:off + 3] = bytes(color)


def _line(buf, w, h, x0, y0, x1, y1, color, width=2):
    dx, sx = abs(x1 - x0), 1 if x0 < x1 else -1
    dy, sy = -abs(y1 - y0), 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        r = max(0, width // 2)
        _fill_rect(buf, w, h, x0-r, y0-r, x0+r+1, y0+r+1, color)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _rect(buf, w, h, x, y, rw, rh, color, width=2, fill=None):
    if fill:
        _fill_rect(buf, w, h, x, y, x+rw, y+rh, fill)
    _line(buf, w, h, x, y, x+rw, y, color, width)
    _line(buf, w, h, x+rw, y, x+rw, y+rh, color, width)
    _line(buf, w, h, x+rw, y+rh, x, y+rh, color, width)
    _line(buf, w, h, x, y+rh, x, y, color, width)


def _circle(buf, w, h, cx, cy, r, color, width=2, fill=None):
    if fill:
        for yy in range(-r, r+1):
            xx = int((max(0, r*r - yy*yy)) ** 0.5)
            _line(buf, w, h, cx-xx, cy+yy, cx+xx, cy+yy, fill, 1)
    x, y, d = r, 0, 1-r
    while x >= y:
        pts = ((x,y),(y,x),(-y,x),(-x,y),(-x,-y),(-y,-x),(y,-x),(x,-y))
        for px, py in pts:
            rr = max(0, width // 2)
            _fill_rect(buf, w, h, cx+px-rr, cy+py-rr, cx+px+rr+1, cy+py+rr+1, color)
        y += 1
        if d <= 0:
            d += 2*y + 1
        else:
            x -= 1
            d += 2*(y-x) + 1


def _icon(buf, w, h, x, y, kind):
    # Compact schematic icons: post, people, megaphone, chat, chart, pause.
    if kind == 0:
        _rect(buf, w, h, x, y, 62, 46, BLUE, 2, WHITE)
        _circle(buf, w, h, x+12, y+11, 5, BLUE, 2, LIGHT)
        _line(buf, w, h, x+22, y+10, x+50, y+10, DARK, 2)
        _line(buf, w, h, x+10, y+24, x+50, y+24, GRID, 3)
        _line(buf, w, h, x+10, y+34, x+42, y+34, GRID, 3)
    elif kind == 1:
        for dx in (8, 31, 54):
            _circle(buf, w, h, x+dx, y+15, 7, BLUE, 2, LIGHT)
            _circle(buf, w, h, x+dx, y+38, 12, BLUE, 2, LIGHT)
    elif kind == 2:
        _line(buf, w, h, x+8, y+26, x+43, y+12, BLUE, 3)
        _line(buf, w, h, x+8, y+26, x+43, y+40, BLUE, 3)
        _line(buf, w, h, x+43, y+12, x+43, y+40, BLUE, 3)
        _rect(buf, w, h, x+5, y+22, 10, 10, DARK, 2, LIGHT)
        _line(buf, w, h, x+50, y+18, x+62, y+12, BLUE, 2)
        _line(buf, w, h, x+50, y+34, x+62, y+40, BLUE, 2)
    elif kind == 3:
        _rect(buf, w, h, x+3, y+4, 54, 30, BLUE, 2, WHITE)
        _rect(buf, w, h, x+18, y+24, 48, 28, DARK, 2, LIGHT)
        for dx in (28, 39, 50):
            _circle(buf, w, h, x+dx, y+38, 2, BLUE, 1, BLUE)
    elif kind == 4:
        _line(buf, w, h, x+8, y+47, x+8, y+8, DARK, 2)
        _line(buf, w, h, x+8, y+47, x+65, y+47, DARK, 2)
        for i, bh in enumerate((18, 31, 24)):
            _rect(buf, w, h, x+18+i*15, y+47-bh, 9, bh, BLUE, 2, LIGHT)
    else:
        _circle(buf, w, h, x+34, y+27, 24, BLUE, 2, LIGHT)
        _rect(buf, w, h, x+24, y+14, 6, 26, DARK, 1, DARK)
        _rect(buf, w, h, x+38, y+14, 6, 26, DARK, 1, DARK)


def question_image_bytes(index):
    w, h = 720, 480
    buf = bytearray(WHITE * (w * h))
    for x in range(0, w, 32):
        _line(buf, w, h, x, 0, x, h-1, GRID, 1)
    for y in range(0, h, 32):
        _line(buf, w, h, 0, y, w-1, y, GRID, 1)
    _rect(buf, w, h, 18, 18, w-36, h-36, BLUE, 2)
    count = len(CONTENT["questions"][index]["answers"])
    if count == 5:
        panels = [(50,58),(520,58),(50,305),(285,330),(520,305)]
    else:
        panels = [(65,70),(505,70),(65,300),(505,300)]
    cx, cy = 360, 230
    _circle(buf, w, h, cx, cy, 61, BLUE, 3, LIGHT)
    _circle(buf, w, h, cx, cy, 43, GRID, 2, WHITE)
    _circle(buf, w, h, cx, cy-9, 14, BLUE, 2, LIGHT)
    _circle(buf, w, h, cx, cy+26, 26, BLUE, 2, LIGHT)
    # A small blueprint accent changes with the question block.
    if index < 3:
        _line(buf, w, h, cx-30, cy+48, cx+30, cy+48, DARK, 3)
    elif index < 6:
        _rect(buf, w, h, cx-27, cy-18, 54, 38, DARK, 2, WHITE)
    else:
        _rect(buf, w, h, cx-28, cy-15, 56, 30, BLUE, 2, WHITE)
    for j, (px, py) in enumerate(panels):
        pw, ph = 150, 92
        pcx, pcy = px + pw//2, py + ph//2
        _line(buf, w, h, pcx, pcy, cx, cy, BLUE, 2)
        _circle(buf, w, h, (pcx+cx)//2, (pcy+cy)//2, 3, BLUE, 1, BLUE)
        _rect(buf, w, h, px, py, pw, ph, BLUE, 2, LIGHT)
        _icon(buf, w, h, px+43, py+18, (index*2 + j) % 6)
        # tiny option marker, deliberately visual rather than textual
        _circle(buf, w, h, px+16, py+16, 7, DARK, 2, WHITE)
    # Technical corner marks.
    for x, y, sx, sy in ((28,28,1,1),(w-28,28,-1,1),(28,h-28,1,-1),(w-28,h-28,-1,-1)):
        _line(buf,w,h,x,y,x+18*sx,y,BLUE,2)
        _line(buf,w,h,x,y,x,y+18*sy,BLUE,2)

    stride = w * 3
    raw = b"".join(b"\x00" + bytes(buf[y*stride:(y+1)*stride]) for y in range(h))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))




def load_env(path):
    """Load simple KEY=value lines, never execute a shell or interpolate secrets."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            raise ValueError("Проверь формат .env: нужны строки KEY=value")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def validate_config():
    token = os.getenv("BOT_TOKEN", "").strip()
    contact = os.getenv("CONTACT_USERNAME", "scherbinskiy").strip().lstrip("@")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("Добавь токен от BotFather в BOT_TOKEN внутри .env")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", contact):
        raise ValueError("Укажи Telegram-username для записи в CONTACT_USERNAME внутри .env")
    return token, contact


def result_text(answers):
    result = diagnose(answers)
    copy = CONTENT["results"][result.key]
    limited_check = result.key == "check" and result.limited_data
    title = copy["unknown_title"] if limited_check else copy["title"]
    body = copy["unknown_body"] if limited_check else copy["body"]
    parts = [f"<b>{html.escape(title)}</b>", body]
    if result.evidence:
        examples = []
        for i in result.evidence:
            q = CONTENT["questions"][i]
            examples.append(f"• {html.escape(q['answers'][answers[i]])}")
        parts.append("<b>Ты отметил:</b>\n" + "\n".join(examples))
    if result.secondary:
        names = {"traffic": "привлечение новых людей", "landing": "описание и оформление предложения", "sales": "продолжение разговоров после обращения"}
        parts.append("Также стоит проверить " + names[result.secondary] + ".")
    if answers[6] == 3 and result.key not in ("unbuilt", "check"):
        parts.append("Продажи пока не оцениваем: ты отметил, что обращений ещё не было.")
    parts.append(copy["cta"])
    parts.append("Нажми «НАПИСАТЬ ДИМЕ». Откроется личный чат с готовым сообщением — отправь его, чтобы договориться о разборе.")
    return "\n\n".join(parts)


def booking_link(answers, contact_username):
    result = diagnose(answers)
    copy = CONTENT["results"][result.key]
    title = copy["unknown_title"] if result.key == "check" and result.limited_data else copy["title"]
    draft = "Разбор"
    return f"https://t.me/{contact_username}?" + urllib.parse.urlencode({"text": draft})


def screen(session, contact_username):
    answers = json.loads(session["answers"])
    prefix = f"{session['sid']}:{session['revision']}"
    if session["screen"] == "intro":
        return CONTENT["intro"], {"inline_keyboard": [[{"text": "НАЧАТЬ ТЕСТ", "callback_data": f"start:{prefix}"}]]}
    if len(answers) == 9:
        return result_text(answers), {"inline_keyboard": [
            [{"text": "НАПИСАТЬ ДИМЕ", "url": booking_link(answers, contact_username)}]
        ]}
    i = len(answers)
    q = CONTENT["questions"][i]
    parts = [f"<b>ВОПРОС {i + 1} ИЗ 9</b>\n<i>{html.escape(CONTENT['blocks'][i // 3])}</i>"]
    if i == 3:
        parts.append(html.escape(q["note"]))
    parts.append(f"<b>{html.escape(q['text'])}</b>")
    if i != 3 and q.get("note"):
        parts.append(html.escape(q["note"]))
    parts.append("\n\n".join(f"{ANSWER_MARKS[j]} {html.escape(answer)}" for j, answer in enumerate(q["answers"])))
    buttons = [[{"text": ANSWER_MARKS[j], "callback_data": f"a:{prefix}:{i}:{j}"} for j in range(len(q["answers"]))]]
    if i:
        buttons.append([{"text": "← Назад", "callback_data": f"back:{prefix}"}])
    return "\n\n".join(parts), {"inline_keyboard": buttons}


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                uid INTEGER PRIMARY KEY, sid TEXT NOT NULL, answers TEXT NOT NULL,
                screen TEXT NOT NULL, revision INTEGER NOT NULL,
                message_id INTEGER, updated INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT OR IGNORE INTO meta VALUES ('offset', 0);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, method TEXT NOT NULL,
                payload TEXT NOT NULL, uid INTEGER, sid TEXT, revision INTEGER
            );
        """)

    def get(self, uid):
        return self.db.execute("SELECT * FROM sessions WHERE uid=?", (uid,)).fetchone()

    def save(self, uid, sid, answers, view, revision, message_id=None):
        self.db.execute("""INSERT OR REPLACE INTO sessions
            (uid,sid,answers,screen,revision,message_id,updated) VALUES (?,?,?,?,?,?,?)""",
            (uid, sid, json.dumps(answers), view, revision, message_id, int(time.time())))

    def enqueue(self, method, payload, uid=None, sid=None, revision=None):
        self.db.execute("INSERT INTO outbox(method,payload,uid,sid,revision) VALUES (?,?,?,?,?)",
                        (method, json.dumps(payload, ensure_ascii=False), uid, sid, revision))

    @property
    def offset(self):
        return self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()[0]

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, int(value)))

    def stats(self):
        total = self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        started = self.db.execute(
            "SELECT COUNT(*) FROM sessions WHERE json_array_length(answers) > 0"
        ).fetchone()[0]
        completed = self.db.execute(
            "SELECT COUNT(*) FROM sessions WHERE json_array_length(answers) = 9"
        ).fetchone()[0]
        return total, started, completed


class QuizBot:
    def __init__(self, store, contact_username):
        self.store = store
        self.contact_username = contact_username

    def say(self, uid, text, markup=None, session=None):
        payload = {"chat_id": uid, "text": text, "parse_mode": "HTML"}
        if markup is not None:
            payload["reply_markup"] = markup
        self.store.enqueue("sendMessage", payload, uid,
                           session["sid"] if session else None,
                           session["revision"] if session else None)

    def photo(self, uid, text, markup, question_index, session):
        payload = {
            "chat_id": uid,
            "caption": text,
            "parse_mode": "HTML",
            "reply_markup": markup,
            "_question_index": question_index,
        }
        self.store.enqueue("sendPhotoGenerated", payload, uid, session["sid"], session["revision"])

    def render(self, uid):
        session = self.store.get(uid)
        text, markup = screen(session, self.contact_username)
        answers = json.loads(session["answers"])
        if session["screen"] == "quiz" and len(answers) < 9:
            self.photo(uid, text, markup, len(answers), session)
        else:
            self.say(uid, text, markup, session)

    def maybe_register_admin(self, user):
        username = (user.get("username") or "").lower()
        if username and username == self.contact_username.lower():
            self.store.set_meta("admin_uid", user.get("id"))

    def completion_payload(self, user, answers):
        result = diagnose(answers)
        copy = CONTENT["results"][result.key]
        title = copy["unknown_title"] if result.key == "check" and result.limited_data else copy["title"]
        username = user.get("username") or ""
        name = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x).strip()
        return {
            "completed_at": int(time.time()),
            "telegram_id": user.get("id"),
            "username": username,
            "name": name,
            "result": title,
            "answers": [CONTENT["questions"][i]["answers"][choice] for i, choice in enumerate(answers)],
        }

    def notify_admin(self, user, answers):
        admin_uid = self.store.get_meta("admin_uid")
        if not admin_uid:
            return
        data = self.completion_payload(user, answers)
        username = data["username"]
        lines = [
            "<b>НОВОЕ ПРОХОЖДЕНИЕ ТЕСТА</b>",
            f"Пользователь: {html.escape(data['name'] or 'Без имени')}",
            f"Telegram: {'@' + html.escape(username) if username else 'нет username'}",
            f"ID: <code>{data['telegram_id']}</code>",
            f"Результат: <b>{html.escape(data['result'])}</b>",
            "",
            "<b>Ответы:</b>"
        ]
        for i, answer in enumerate(data["answers"], 1):
            lines.append(f"{i}. {html.escape(answer)}")
        self.say(admin_uid, "\n".join(lines))

    def send_to_sheets(self, user, answers):
        url = os.getenv("SHEETS_WEBHOOK_URL", "").strip()
        if not url:
            return
        data = self.completion_payload(user, answers)
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if not 200 <= response.status < 300:
                    LOG.warning("Google Sheets webhook вернул HTTP %s", response.status)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            LOG.warning("Не удалось записать результат в Google Sheets: %s", type(exc).__name__)

    def retire(self, uid, session):
        self.store.db.execute("DELETE FROM outbox WHERE uid=? AND sid IS NOT NULL", (uid,))
        if session and session["message_id"]:
            self.store.enqueue("editMessageReplyMarkup", {"chat_id": uid, "message_id": session["message_id"], "reply_markup": {"inline_keyboard": []}}, uid)

    def fresh(self, uid):
        self.retire(uid, self.store.get(uid))
        self.store.save(uid, uuid.uuid4().hex[:12], [], "intro", 0)
        self.render(uid)

    def handle(self, update):
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or update_id < self.store.offset:
            return
        with self.store.db:
            if "callback_query" in update:
                self.callback(update["callback_query"])
            elif "message" in update:
                self.message(update["message"])
            self.store.db.execute("UPDATE meta SET value=? WHERE key='offset'", (update_id + 1,))

    def message(self, message):
        chat = message.get("chat", {})
        user = message.get("from", {})
        if chat.get("type") != "private" or user.get("is_bot") or chat.get("id") != user.get("id"):
            return
        uid = chat["id"]
        self.maybe_register_admin(user)
        text = message.get("text", "").strip()
        command = text.split()[0].split("@")[0].lower() if text else ""
        session = self.store.get(uid)
        if command == "/stats":
            if (user.get("username") or "").lower() != self.contact_username.lower():
                self.say(uid, "Команда недоступна.")
                return
            total, started, completed = self.store.stats()
            conversion = (completed / total * 100) if total else 0
            self.say(
                uid,
                "<b>СТАТИСТИКА БОТА</b>\n\n"
                f"Запустили: <b>{total}</b>\n"
                f"Начали тест: <b>{started}</b>\n"
                f"Завершили: <b>{completed}</b>\n"
                f"Конверсия запуск → завершение: <b>{conversion:.1f}%</b>"
            )
        elif command == "/delete":
            self.retire(uid, session)
            self.store.db.execute("DELETE FROM sessions WHERE uid=?", (uid,))
            self.say(uid, "Твои ответы и результат удалены из базы бота. Сообщения в Telegram остаются в чате.\n\nЧтобы начать снова, нажми /start.")
        elif command == "/restart" or (command == "/start" and session is None):
            self.fresh(uid)
        elif command == "/start":
            self.retire(uid, session)
            self.store.save(uid, session["sid"], json.loads(session["answers"]), session["screen"], session["revision"] + 1)
            self.render(uid)
        elif command == "/help":
            self.say(uid, "Ответь на 9 вопросов, выбирая цифру под сообщением.\n\n/start — начать или продолжить\n/restart — начать заново\n/delete — удалить сохранённые ответы\n\nТест предлагает, какой участок связки проверить первым, на основании твоих ответов.")
        elif session is None:
            self.say(uid, "Нажми /start, чтобы пройти тест «Где ты теряешь клиентов?».")
        else:
            self.say(uid, "Выбери ответ кнопкой под последним вопросом. Если потерял его в переписке, нажми /start — я покажу текущий шаг.")

    def callback(self, callback):
        callback_id = callback.get("id")
        if not callback_id:
            return
        message = callback.get("message", {})
        chat = message.get("chat", {})
        user = callback.get("from", {})
        uid = user.get("id")
        self.maybe_register_admin(user)
        def ack(text=""):
            payload = {"callback_query_id": callback_id}
            if text:
                payload["text"] = text
            self.store.enqueue("answerCallbackQuery", payload, uid)
        if chat.get("type") != "private" or chat.get("id") != uid:
            ack("Открой личный чат с ботом.")
            return
        session = self.store.get(uid)
        fields = callback.get("data", "").split(":")
        if (not session or len(fields) < 3 or fields[1] != session["sid"]
                or fields[2] != str(session["revision"])
                or message.get("message_id") != session["message_id"]):
            ack("Эта кнопка уже неактуальна. Используй последний вопрос или /start.")
            return
        action = fields[0]
        answers = json.loads(session["answers"])
        view = session["screen"]
        if action == "start" and len(fields) == 3 and view == "intro":
            view = "quiz"
        elif action == "a" and len(fields) == 5 and view == "quiz" and len(answers) < 9:
            if not fields[3].isdigit() or not fields[4].isdigit():
                ack("Выбери ответ кнопкой под вопросом.")
                return
            index, choice = int(fields[3]), int(fields[4])
            if index != len(answers) or not 0 <= choice < len(CONTENT["questions"][index]["answers"]):
                ack("Этот ответ не подходит к текущему вопросу.")
                return
            answers.append(choice)
            if len(answers) == 9:
                view = "result"
        elif action == "back" and len(fields) == 3 and view == "quiz" and answers:
            answers.pop()
        elif action == "restart" and len(fields) == 3 and view == "result":
            ack()
            self.fresh(uid)
            return
        else:
            ack("Используй кнопки под последним сообщением.")
            return
        ack()
        self.retire(uid, session)
        self.store.save(uid, session["sid"], answers, view, session["revision"] + 1)
        if view == "result" and len(answers) == 9:
            self.notify_admin(user, answers)
            self.send_to_sheets(user, answers)
        self.render(uid)


class APIError(Exception):
    def __init__(self, code, description="", retry_after=0):
        super().__init__(f"Telegram API: код {code}")
        self.code, self.description, self.retry_after = code, description, retry_after


class Telegram:
    def __init__(self, token):
        self._base = "https://api.telegram.org/bot" + token + "/"

    def _request(self, method, body, content_type):
        request = urllib.request.Request(self._base + method, data=body, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read())
            except (ValueError, OSError):
                result = {"ok": False, "error_code": exc.code}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise APIError(0) from None
        if not result.get("ok"):
            raise APIError(result.get("error_code", 0), result.get("description", ""), result.get("parameters", {}).get("retry_after", 0))
        return result["result"]

    def call(self, method, payload=None):
        body = json.dumps(payload or {}).encode()
        return self._request(method, body, "application/json")

    def call_file(self, method, payload, field, data, filename):
        boundary = "----svyazka" + uuid.uuid4().hex
        b = boundary.encode()
        parts = []
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            value = str(value).encode("utf-8")
            parts.extend([
                b"--" + b + b"\r\n",
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                value, b"\r\n"
            ])
        parts.extend([
            b"--" + b + b"\r\n",
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: image/png\r\n\r\n",
            data, b"\r\n",
            b"--" + b + b"--\r\n"
        ])
        return self._request(method, b"".join(parts), f"multipart/form-data; boundary={boundary}")


def drain(store, api):
    """Durable outgoing queue: saved answers survive an API outage or restart."""
    while True:
        row = store.db.execute("SELECT * FROM outbox ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return
        method = row["method"]
        payload = json.loads(row["payload"])
        try:
            if method == "sendPhotoGenerated":
                question_index = int(payload.pop("_question_index"))
                result = api.call_file("sendPhoto", payload, "photo",
                                       question_image_bytes(question_index),
                                       f"question-{question_index + 1}.png")
            else:
                result = api.call(method, payload)
        except APIError as exc:
            if exc.code in (400, 403) and (method != "sendMessage" or exc.code == 403):
                with store.db:
                    store.db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))
                LOG.warning("Пропущена недоступная операция %s (код %s)", method, exc.code)
                continue
            raise
        with store.db:
            if method in ("sendMessage", "sendPhotoGenerated") and row["sid"] is not None:
                store.db.execute("UPDATE sessions SET message_id=? WHERE uid=? AND sid=? AND revision=?",
                                 (result["message_id"], row["uid"], row["sid"], row["revision"]))
            store.db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))


def demo():
    """Try the actual quiz and result locally, with no token and no network."""
    print(re.sub(r"<[^>]+>", "", CONTENT["intro"]))
    answers = []
    while len(answers) < 9:
        i = len(answers)
        q = CONTENT["questions"][i]
        print(f"\n{i + 1}/9. {q['text']}")
        for j, answer in enumerate(q["answers"], 1):
            print(f"{ANSWER_MARKS[j - 1]} {answer}")
        choice = input("Ответ (цифра; b — назад): ").strip()
        if choice == "b" and answers:
            answers.pop()
        elif choice.isdigit() and 1 <= int(choice) <= len(q["answers"]):
            answers.append(int(choice) - 1)
        else:
            print("Выбери номер ответа.")
    print("\n" + html.unescape(re.sub(r"<[^>]+>", "", result_text(answers))))


def main():
    parser = argparse.ArgumentParser(description="Telegram-бот «СВЯЗКА»")
    parser.add_argument("--demo", action="store_true", help="Пройти тест локально без токена")
    parser.add_argument("--remove-webhook", action="store_true", help="Явно отключить старый webhook этого бота")
    args = parser.parse_args()
    if args.demo:
        demo()
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        load_env(ROOT / ".env")
        token, contact = validate_config()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    api = Telegram(token)
    try:
        me = api.call("getMe")
        webhook = api.call("getWebhookInfo")
        if webhook.get("url"):
            if not args.remove_webhook:
                print("У бота уже настроен webhook. Останови прежнюю интеграцию; для перехода на эту версию используй --remove-webhook.", file=sys.stderr)
                raise SystemExit(2)
            api.call("deleteWebhook", {"drop_pending_updates": False})
        api.call("setMyCommands", {"commands": [
            {"command": "start", "description": "Начать или продолжить тест"},
            {"command": "restart", "description": "Пройти тест заново"},
            {"command": "help", "description": "Как пройти тест"},
            {"command": "stats", "description": "Статистика бота"},
            {"command": "delete", "description": "Удалить сохранённые ответы"}
        ]})
    except APIError as exc:
        print(f"Не удалось подключиться к Telegram (код {exc.code}). Проверь токен и доступ к сети.", file=sys.stderr)
        raise SystemExit(2) from None
    db_path = Path(os.getenv("SESSION_DB", "data/quiz.sqlite3"))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    store = Store(db_path)
    bot = QuizBot(store, contact)
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOG.info("Бот @%s запущен", me.get("username", ""))
    backoff = 1
    while not stopping:
        try:
            drain(store, api)
            updates = api.call("getUpdates", {"offset": store.offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]})
            for update in updates:
                bot.handle(update)
                drain(store, api)
                if stopping:
                    break
            backoff = 1
        except APIError as exc:
            if exc.code in (401, 409):
                LOG.error("Код %s: проверь токен и что запущен только один экземпляр бота", exc.code)
                break
            LOG.warning("Связь с Telegram временно недоступна (код %s); повторяем", exc.code)
            delay = exc.retry_after or backoff
            for _ in range(max(1, int(delay))):
                if stopping:
                    break
                time.sleep(1)
            backoff = min(backoff * 2, 30)
        except sqlite3.Error:
            LOG.error("Ошибка базы данных. Проверь доступ к диску и свободное место.")
            break
    store.db.close()


if __name__ == "__main__":
    main()
