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
    draft = f"Дима, привет! Прошёл тест «Где ты теряешь клиентов?».\n\nМой результат: {title.lower()}.\n\nХочу записаться на разбор."
    return f"https://t.me/{contact_username}?" + urllib.parse.urlencode({"text": draft})


def screen(session, contact_username):
    answers = json.loads(session["answers"])
    prefix = f"{session['sid']}:{session['revision']}"
    if session["screen"] == "intro":
        return CONTENT["intro"], {"inline_keyboard": [[{"text": "НАЧАТЬ ТЕСТ", "callback_data": f"start:{prefix}"}]]}
    if len(answers) == 9:
        return result_text(answers), {"inline_keyboard": [
            [{"text": "НАПИСАТЬ ДИМЕ", "url": booking_link(answers, contact_username)}],
            [{"text": "Пройти заново", "callback_data": f"restart:{prefix}"}]
        ]}
    i = len(answers)
    q = CONTENT["questions"][i]
    parts = [f"<b>ВОПРОС {i + 1} ИЗ 9</b>\n<i>{html.escape(CONTENT['blocks'][i // 3])}</i>"]
    if i == 3:
        parts.append(html.escape(q["note"]))
    parts.append(f"<b>{html.escape(q['text'])}</b>")
    if i != 3 and q.get("note"):
        parts.append(html.escape(q["note"]))
    parts.append("\n\n".join(f"{j + 1}. {html.escape(answer)}" for j, answer in enumerate(q["answers"])))
    buttons = [[{"text": str(j + 1), "callback_data": f"a:{prefix}:{i}:{j}"} for j in range(len(q["answers"]))]]
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

    def render(self, uid):
        session = self.store.get(uid)
        text, markup = screen(session, self.contact_username)
        self.say(uid, text, markup, session)

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
        text = message.get("text", "").strip()
        command = text.split()[0].split("@")[0].lower() if text else ""
        session = self.store.get(uid)
        if command == "/delete":
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
        self.render(uid)


class APIError(Exception):
    def __init__(self, code, description="", retry_after=0):
        super().__init__(f"Telegram API: код {code}")
        self.code, self.description, self.retry_after = code, description, retry_after


class Telegram:
    def __init__(self, token):
        self._base = "https://api.telegram.org/bot" + token + "/"

    def call(self, method, payload=None):
        body = json.dumps(payload or {}).encode()
        request = urllib.request.Request(self._base + method, data=body, headers={"Content-Type": "application/json"})
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


def drain(store, api):
    """Durable outgoing queue: saved answers survive an API outage or restart."""
    while True:
        row = store.db.execute("SELECT * FROM outbox ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return
        method = row["method"]
        try:
            result = api.call(method, json.loads(row["payload"]))
        except APIError as exc:
            if exc.code in (400, 403) and (method != "sendMessage" or exc.code == 403):
                with store.db:
                    store.db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))
                LOG.warning("Пропущена недоступная операция %s (код %s)", method, exc.code)
                continue
            raise
        with store.db:
            if method == "sendMessage" and row["sid"] is not None:
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
            print(f"{j}. {answer}")
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
