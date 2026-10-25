#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZINDAGI · платёжный бот «Премиум»

Как это работает
  1. В приложении пользователь жмёт «Оформить подписку» и попадает в этого бота.
  2. Бот присылает реквизиты (карта, сумма, комментарий) и ждёт чек 10 минут.
  3. Нет чека за 10 минут → «запрос отменён» + кнопка «Повторить».
  4. Пришёл чек → пользователю «платёж обрабатывается», админу — чек, имя, номер
     и кнопки «Подтвердить» / «Отказать».
  5. Админ подтвердил → бот пишет в Firebase  premium/<telegram-id> = {until: мс},
     пользователю приходит сообщение об успешной подписке, а приложение
     разблокируется само (оно читает эту запись).

Запуск:  pip install -r requirements.txt  →  заполнить .env  →  python bot.py
На хостинге (Bothost и т. п.): переменные — в панели хостинга или в файле .env рядом с bot.py;
ключ Firebase — переменной FIREBASE_CREDENTIALS_JSON (ключ проекта ZINDAGI, не другого проекта!);
база заявок хранится в DATA_DIR, если такая переменная есть.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import requests

log = logging.getLogger("zindagi-pay")
TZ = timezone(timedelta(hours=5))  # Таджикистан, UTC+5
DAY_MS = 86_400_000


# ───────────────────────────── настройки ─────────────────────────────
def load_env(path: str = ".env") -> None:
    """Мини-загрузчик .env (без внешних библиотек)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Cfg:
    def __init__(self, env=os.environ):
        self.token = env.get("BOT_TOKEN", "").strip()
        self.admins = [int(x) for x in re.split(r"[,\s]+", env.get("ADMIN_IDS", "")) if re.fullmatch(r"-?\d+", x or "")]
        self.db_url = env.get("FIREBASE_DB_URL", "https://zindagi-1c81c-default-rtdb.firebaseio.com").strip().rstrip("/")
        self.cred_path = env.get("GOOGLE_APPLICATION_CREDENTIALS", "serviceAccount.json").strip()
        # Ключ Firebase можно передать не файлом, а переменной (удобно для Railway): JSON целиком или его base64
        self.cred_json = env.get("FIREBASE_CREDENTIALS_JSON", "").strip()
        self.price = int(env.get("PRICE_TJS", "20"))
        self.card = env.get("CARD_NUMBER", "4444 8888 1227 1025").strip()
        self.holder = env.get("CARD_HOLDER", "EHSON IDIEV").strip()
        self.note = env.get("PAY_NOTE", "Подписка премиума на 1 месяц").strip()
        self.wait_min = float(env.get("WAIT_MINUTES", "10"))
        self.days = int(env.get("PREMIUM_DAYS", "30"))
        self.app_url = env.get("APP_URL", "").strip()
        # Второй бот — бот ВХОДА (@zindagiRObot): присылает коды и привязывает номер к Telegram. Нужен ОТДЕЛЬНЫЙ токен.
        self.login_token = env.get("LOGIN_BOT_TOKEN", "").strip()
        # База заявок и подписок (SQLite). На хостингах с постоянной папкой (Bothost: DATA_DIR=/app/data) кладём её туда,
        # иначе при пересоздании контейнера файл пропадёт вместе с заявками и сроками подписок.
        data_dir = env.get("DATA_DIR", "").strip()
        default_db = os.path.join(data_dir, "premium.sqlite3") if data_dir else "premium.sqlite3"
        self.sqlite = (env.get("SQLITE_PATH", "").strip() or default_db)
        # Заявки «нет доступа к Telegram» — отдельный файл рядом с основной базой
        self.access_sqlite = (env.get("ACCESS_SQLITE_PATH", "").strip()
                              or os.path.join(os.path.dirname(os.path.abspath(self.sqlite)), "access.sqlite3"))

    def check(self) -> None:
        if not self.token:
            raise SystemExit("Не задан BOT_TOKEN (токен платёжного бота из @BotFather).")
        if not self.admins:
            raise SystemExit("Не задан ADMIN_IDS (Telegram ID админа; узнать можно у @userinfobot).")


# ───────────────────────────── утилиты ─────────────────────────────
def fmt_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")


def fmt_d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%d.%m.%Y")


def canon_phone(p: Any) -> str:
    """Любой ввод → цифры С кодом страны (99290…, 99890…, 7999…) или '' если номер не поддерживается.

    Поддерживаются Таджикистан (+992, 9 цифр), Узбекистан (+998, 9 цифр) и Россия (+7, 10 цифр, мобильные 9XX).
    Старые номера из базы и от старой версии приложения — ровно 9 цифр без кода — считаются таджикскими."""
    d = re.sub(r"\D", "", str(p or ""))
    if len(d) == 9:
        return "992" + d
    if len(d) == 12 and d[:3] in ("992", "998"):
        return d
    if len(d) == 11 and d[0] == "7" and d[1] == "9":
        return d
    return ""


def split_phone(p: Any) -> tuple[str, str]:
    """→ (код страны, национальный номер); ('', '') для неподдерживаемого номера."""
    c = canon_phone(p)
    if not c:
        return "", ""
    cc = "7" if c[0] == "7" else c[:3]
    return cc, c[len(cc):]


def phone_keys(phone: str) -> list[str]:
    """Ключи узла phones/ для номера: новый (с кодом страны) и, для таджикских, старый 9-значный."""
    return [phone] + ([phone[3:]] if phone.startswith("992") else [])


def fmt_phone(p: str | None) -> str:
    cc, n = split_phone(p)
    if not cc:
        d = re.sub(r"\D", "", str(p or ""))
        return f"+{d}" if d else "не указан"
    g = (3, 3, 2, 2) if cc == "7" else (2, 3, 2, 2)
    parts, i = [], 0
    for k in g:
        parts.append(n[i:i + k])
        i += k
    return f"+{cc} " + " ".join(parts)


def parse_any_phone(text: str) -> str:
    """Телефон, который человек набрал в чате (любой страны): +7 999…, 8 999…, 90 123 45 67 … → '+<цифры>' или ''."""
    d = re.sub(r"\D", "", text or "")
    if len(d) == 11 and d[0] == "8" and d[1] == "9":      # российский «8 9xx…»
        d = "7" + d[1:]
    if not 8 <= len(d) <= 15:
        return ""
    c = canon_phone(d)
    return fmt_phone(c) if c else "+" + d


def parse_payload(payload: str) -> tuple[str, str]:
    """start-параметр из приложения: 'p' + base64url('<телефон>|<имя>') → (телефон с кодом страны, имя)."""
    if not payload or payload[0] != "p":
        return "", ""
    raw = payload[1:]
    try:
        text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
    except Exception:
        return "", ""
    phone, _, name = text.partition("|")
    digits = re.sub(r"\D", "", phone)
    return canon_phone(digits) or digits[-9:], name.strip()[:60]


def kb(rows: list[list[tuple[str, str]]]) -> dict:
    """Инлайн-клавиатура: (текст, callback_data | 'url:https://…')."""
    def btn(t: str, d: str) -> dict:
        return {"text": t, "url": d[4:]} if d.startswith("url:") else {"text": t, "callback_data": d}
    return {"inline_keyboard": [[btn(t, d) for t, d in row] for row in rows]}


# ───────────────────────────── Telegram API ─────────────────────────────
class Telegram:
    def __init__(self, token: str, base: str = "https://api.telegram.org"):
        self.url = f"{base}/bot{token}/"
        self.s = requests.Session()

    def call(self, method: str, _t: int = 30, **params: Any) -> Any:
        """Вызов метода. Ошибки не роняют бота: вернётся None."""
        for _ in range(3):
            try:
                r = self.s.post(self.url + method, json=params, timeout=_t)
                j = r.json()
            except (requests.RequestException, ValueError) as e:
                log.warning("Telegram %s: %s", method, e)
                return None
            if j.get("ok"):
                return j["result"]
            if r.status_code == 429:
                time.sleep(min(int(j.get("parameters", {}).get("retry_after", 1)), 10))
                continue
            log.warning("Telegram %s → %s %s", method, r.status_code, j.get("description"))
            return None
        return None

    def get_updates(self, offset: Optional[int], timeout: int) -> list[dict]:
        body: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            body["offset"] = offset
        r = self.s.post(self.url + "getUpdates", timeout=timeout + 15, json=body)
        j = r.json()
        if not j.get("ok"):
            raise RuntimeError(f"getUpdates: {j.get('description')}")
        return j["result"]


# ───────────────────────────── Firebase ─────────────────────────────
class Firebase:
    """Запись статуса подписки в Realtime Database (через service account)."""

    def __init__(self, db_url: str, cred_path: str, cred_json: str = ""):
        import firebase_admin
        from firebase_admin import credentials, db

        if cred_json:
            raw = cred_json if cred_json.lstrip().startswith("{") else base64.b64decode(cred_json).decode("utf-8")
            cred = credentials.Certificate(json.loads(raw))
        else:
            cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred, {"databaseURL": db_url})
        self._db = db

    # --- общие операции (для бота входа) ---
    def get(self, path: str) -> Any:
        return self._db.reference(path).get()

    def keys(self, path: str) -> list[str]:
        """Только имена дочерних узлов (без загрузки данных)."""
        res = self._db.reference(path).get(shallow=True)
        return list(res.keys()) if isinstance(res, dict) else []

    def set(self, path: str, value: Any) -> None:
        self._db.reference(path).set(value)

    def delete(self, path: str) -> None:
        self._db.reference(path).delete()

    def claim(self, path: str, value: Any) -> bool:
        """Атомарно занять ключ: True, если после операции там лежит именно value."""
        res = self._db.reference(path).transaction(lambda cur: value if cur is None else cur)
        return res == value

    def custom_token(self, uid: int) -> str:
        """Токен входа в Firebase Auth для приложения: uid = Telegram-ID. Нужен, чтобы данные пользователя
        (users/<uid>) могли читать и писать только он сам, и чтобы на новом телефоне они подтянулись."""
        from firebase_admin import auth
        t = auth.create_custom_token(str(uid))
        return t.decode() if isinstance(t, bytes) else str(t)

    def revoke(self, uid: int) -> None:
        """Завершить все входы пользователя в Firebase Auth (после блокировки)."""
        from firebase_admin import auth
        auth.revoke_refresh_tokens(str(uid))

    def set_premium(self, uid: int, until_ms: int) -> None:
        self._db.reference(f"premium/{uid}").set(
            {"until": int(until_ms), "plan": "month", "updatedAt": int(time.time() * 1000)})

    def clear_premium(self, uid: int) -> None:
        self._db.reference(f"premium/{uid}").delete()


# ───────────────────────────── хранилище ─────────────────────────────
class Store:
    def __init__(self, path: str):
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, name TEXT, phone TEXT, username TEXT, tg_name TEXT);
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            status TEXT NOT NULL,                -- awaiting | review | approved | rejected | expired
            created REAL NOT NULL, expires REAL NOT NULL,
            prompt_msg INTEGER, file_id TEXT, file_type TEXT, extras INTEGER DEFAULT 0,
            decided_by INTEGER, decided REAL);
        CREATE TABLE IF NOT EXISTS admin_msgs(
            payment_id INTEGER, admin_id INTEGER, message_id INTEGER);
        CREATE TABLE IF NOT EXISTS premium(
            user_id INTEGER PRIMARY KEY, until INTEGER NOT NULL,      -- миллисекунды
            warned INTEGER DEFAULT 0, ended INTEGER DEFAULT 0);
        """)
        self.c.commit()

    # users
    def upsert_user(self, uid: int, name: str, phone: str, username: str, tg_name: str) -> None:
        old = self.user(uid)
        self.c.execute(
            "INSERT INTO users(id,name,phone,username,tg_name) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=?, phone=?, username=?, tg_name=?",
            (uid, name or (old["name"] if old else ""), phone or (old["phone"] if old else ""), username, tg_name,
             name or (old["name"] if old else ""), phone or (old["phone"] if old else ""), username, tg_name))
        self.c.commit()

    def user(self, uid: int) -> Optional[sqlite3.Row]:
        return self.c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    # payments
    def payment(self, pid: int) -> Optional[sqlite3.Row]:
        return self.c.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()

    def active_request(self, uid: int, now: float) -> Optional[sqlite3.Row]:
        return self.c.execute(
            "SELECT * FROM payments WHERE user_id=? AND status='awaiting' AND expires>? ORDER BY id DESC LIMIT 1",
            (uid, now)).fetchone()

    def review_request(self, uid: int) -> Optional[sqlite3.Row]:
        return self.c.execute(
            "SELECT * FROM payments WHERE user_id=? AND status='review' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()

    def all_review(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM payments WHERE status='review' ORDER BY id").fetchall()

    def create_request(self, uid: int, chat_id: int, now: float, wait_s: float) -> int:
        cur = self.c.execute(
            "INSERT INTO payments(user_id,chat_id,status,created,expires) VALUES(?,?,?,?,?)",
            (uid, chat_id, "awaiting", now, now + wait_s))
        self.c.commit()
        return int(cur.lastrowid)

    def set_prompt(self, pid: int, msg_id: int) -> None:
        self.c.execute("UPDATE payments SET prompt_msg=? WHERE id=?", (msg_id, pid))
        self.c.commit()

    def set_receipt(self, pid: int, file_id: str, file_type: str) -> None:
        self.c.execute("UPDATE payments SET status='review', file_id=?, file_type=? WHERE id=?", (file_id, file_type, pid))
        self.c.commit()

    def add_extra(self, pid: int) -> int:
        self.c.execute("UPDATE payments SET extras=extras+1 WHERE id=?", (pid,))
        self.c.commit()
        return int(self.payment(pid)["extras"])

    def set_status(self, pid: int, status: str, by: Optional[int] = None, now: Optional[float] = None) -> None:
        self.c.execute("UPDATE payments SET status=?, decided_by=?, decided=? WHERE id=?", (status, by, now, pid))
        self.c.commit()

    def expire_due(self, now: float) -> list[sqlite3.Row]:
        rows = self.c.execute("SELECT * FROM payments WHERE status='awaiting' AND expires<=?", (now,)).fetchall()
        for r in rows:
            self.c.execute("UPDATE payments SET status='expired' WHERE id=?", (r["id"],))
        self.c.commit()
        return rows

    def next_expiry(self) -> Optional[float]:
        r = self.c.execute("SELECT MIN(expires) m FROM payments WHERE status='awaiting'").fetchone()
        return r["m"] if r and r["m"] is not None else None

    def add_admin_msg(self, pid: int, admin_id: int, message_id: int) -> None:
        self.c.execute("INSERT INTO admin_msgs VALUES(?,?,?)", (pid, admin_id, message_id))
        self.c.commit()

    def admin_msgs(self, pid: int) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM admin_msgs WHERE payment_id=?", (pid,)).fetchall()

    # premium
    def premium(self, uid: int) -> Optional[sqlite3.Row]:
        return self.c.execute("SELECT * FROM premium WHERE user_id=?", (uid,)).fetchone()

    def set_premium(self, uid: int, until_ms: int) -> None:
        self.c.execute(
            "INSERT INTO premium(user_id,until,warned,ended) VALUES(?,?,0,0) "
            "ON CONFLICT(user_id) DO UPDATE SET until=?, warned=0, ended=0", (uid, until_ms, until_ms))
        self.c.commit()

    def del_premium(self, uid: int) -> None:
        self.c.execute("DELETE FROM premium WHERE user_id=?", (uid,))
        self.c.commit()

    def expiring(self, now_ms: int, within_ms: int) -> list[sqlite3.Row]:
        return self.c.execute(
            "SELECT * FROM premium WHERE warned=0 AND until>? AND until<=?", (now_ms, now_ms + within_ms)).fetchall()

    def ended(self, now_ms: int) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM premium WHERE ended=0 AND until<=?", (now_ms,)).fetchall()

    def mark(self, uid: int, col: str) -> None:
        assert col in ("warned", "ended")
        self.c.execute(f"UPDATE premium SET {col}=1 WHERE user_id=?", (uid,))
        self.c.commit()

    def premium_until(self, uid: int) -> int:
        r = self.premium(uid)
        return int(r["until"]) if r else 0

    def move_premium(self, old_uid: int, new_uid: int, until_ms: int) -> None:
        """Перенос подписки на другой Telegram (вызывается из потока бота входа): одной транзакцией."""
        with self.c:  # commit/rollback целиком
            self.c.execute("DELETE FROM premium WHERE user_id=?", (old_uid,))
            if until_ms:
                self.c.execute(
                    "INSERT INTO premium(user_id,until,warned,ended) VALUES(?,?,0,0) "
                    "ON CONFLICT(user_id) DO UPDATE SET until=?, warned=0, ended=0", (new_uid, until_ms, until_ms))


class AccessStore:
    """Заявки «нет доступа к Telegram» и шаги диалога. Отдельный файл SQLite — не мешает базе платёжного бота."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.lock = threading.RLock()
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS access_flow(
            user_id INTEGER PRIMARY KEY, step TEXT NOT NULL,   -- name | tgphone | receipt
            phone TEXT NOT NULL, chat_id INTEGER NOT NULL, name TEXT, tg_phone TEXT, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS access_req(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            username TEXT, tg_name TEXT, phone TEXT NOT NULL,   -- phone: номер из приложения, с кодом страны
            name TEXT, tg_phone TEXT, file_id TEXT, file_type TEXT,
            status TEXT NOT NULL,                               -- new | pending | approved | rejected
            reason TEXT, old_uid INTEGER, created REAL NOT NULL, decided_by INTEGER, decided REAL);
        CREATE TABLE IF NOT EXISTS access_admin_msgs(req_id INTEGER, admin_id INTEGER, message_id INTEGER);
        CREATE TABLE IF NOT EXISTS support(
            uid INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, name TEXT, username TEXT, phone TEXT,
            state TEXT NOT NULL,                                -- open | closed
            last_admin REAL NOT NULL DEFAULT 0, last_user REAL NOT NULL DEFAULT 0, closed_sent INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS support_msgs(admin_id INTEGER, message_id INTEGER, uid INTEGER);
        CREATE TABLE IF NOT EXISTS sec_resp(rid INTEGER PRIMARY KEY, answer TEXT NOT NULL, ts REAL NOT NULL);
        """)
        self.c.commit()

    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock, self.c:
            return self.c.execute(sql, args)

    # диалог
    def flow(self, uid: int) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.c.execute("SELECT * FROM access_flow WHERE user_id=?", (uid,)).fetchone()

    def flow_start(self, uid: int, chat_id: int, phone: str, now: float) -> None:
        self._run("INSERT OR REPLACE INTO access_flow(user_id,step,phone,chat_id,updated) VALUES(?,?,?,?,?)",
                  (uid, "name", phone, chat_id, now))

    def flow_set(self, uid: int, now: float, **cols: Any) -> None:
        keys = [k for k in cols if k in ("step", "name", "tg_phone")]
        sets = ",".join(f"{k}=?" for k in keys)
        self._run(f"UPDATE access_flow SET {sets + ',' if sets else ''}updated=? WHERE user_id=?",
                  (*[cols[k] for k in keys], now, uid))

    def flow_del(self, uid: int) -> None:
        self._run("DELETE FROM access_flow WHERE user_id=?", (uid,))

    # заявки
    def req(self, rid: int) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.c.execute("SELECT * FROM access_req WHERE id=?", (rid,)).fetchone()

    def req_create(self, f: sqlite3.Row, username: str, tg_name: str, file_id: str, file_type: str, now: float) -> int:
        cur = self._run(
            "INSERT INTO access_req(user_id,chat_id,username,tg_name,phone,name,tg_phone,file_id,file_type,status,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,'new',?)",
            (f["user_id"], f["chat_id"], username, tg_name, f["phone"], f["name"], f["tg_phone"], file_id, file_type, now))
        return int(cur.lastrowid)

    def req_delete(self, rid: int) -> None:
        self._run("DELETE FROM access_req WHERE id=?", (rid,))

    def req_set(self, rid: int, **cols: Any) -> None:
        keys = [k for k in cols if k in ("status", "old_uid")]
        self._run(f"UPDATE access_req SET {','.join(f'{k}=?' for k in keys)} WHERE id=?", (*[cols[k] for k in keys], rid))

    def req_decide(self, rid: int, status: str, by: int, reason: Optional[str], now: float) -> None:
        self._run("UPDATE access_req SET status=?, decided_by=?, reason=?, decided=? WHERE id=?",
                  (status, by, reason, now, rid))

    def pending_of(self, uid: int, phone: str) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.c.execute("SELECT * FROM access_req WHERE user_id=? AND phone=? AND status='pending' LIMIT 1",
                                  (uid, phone)).fetchone()

    def recent_count(self, uid: int, since: float) -> int:
        with self.lock:
            return int(self.c.execute("SELECT COUNT(*) n FROM access_req WHERE user_id=? AND created>? AND status!='new'",
                                      (uid, since)).fetchone()["n"])

    def others_pending(self, phone: str, exclude: int) -> list[int]:
        with self.lock:
            return [r["id"] for r in self.c.execute(
                "SELECT id FROM access_req WHERE phone=? AND status='pending' AND id!=?", (phone, exclude)).fetchall()]

    def add_admin_msg(self, rid: int, admin_id: int, message_id: int) -> None:
        self._run("INSERT INTO access_admin_msgs VALUES(?,?,?)", (rid, admin_id, message_id))

    def admin_msgs(self, rid: int) -> list[sqlite3.Row]:
        with self.lock:
            return self.c.execute("SELECT * FROM access_admin_msgs WHERE req_id=?", (rid,)).fetchall()

    # служба поддержки: диалоги «пользователь ↔ админ»
    def sup(self, uid: int) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.c.execute("SELECT * FROM support WHERE uid=?", (uid,)).fetchone()

    def sup_open(self, uid: int, chat_id: int, name: str, username: str, phone: str) -> None:
        self._run(
            "INSERT INTO support(uid,chat_id,name,username,phone,state) VALUES(?,?,?,?,?,'open') "
            "ON CONFLICT(uid) DO UPDATE SET chat_id=excluded.chat_id, name=excluded.name, username=excluded.username, "
            "phone=CASE WHEN excluded.phone!='' THEN excluded.phone ELSE support.phone END, state='open', closed_sent=0",
            (uid, chat_id, name, username, phone))

    def sup_touch(self, uid: int, who: str, now: float) -> None:
        if who == "admin":
            self._run("UPDATE support SET last_admin=?, state='open', closed_sent=0 WHERE uid=?", (now, uid))
        else:
            self._run("UPDATE support SET last_user=? WHERE uid=?", (now, uid))

    def sup_due(self, now: float, wait: float) -> list[sqlite3.Row]:
        """Админ ответил, а пользователь молчит дольше wait секунд."""
        with self.lock:
            return self.c.execute(
                "SELECT * FROM support WHERE state='open' AND last_admin>0 AND last_user<last_admin "
                "AND closed_sent=0 AND ?-last_admin>=?", (now, wait)).fetchall()

    def sup_close(self, uid: int) -> None:
        self._run("UPDATE support SET state='closed', closed_sent=1 WHERE uid=?", (uid,))

    def sup_map_add(self, admin_id: int, message_id: int, uid: int) -> None:
        self._run("INSERT INTO support_msgs VALUES(?,?,?)", (admin_id, message_id, uid))

    def sup_map_get(self, admin_id: int, message_id: int) -> Optional[int]:
        with self.lock:
            r = self.c.execute("SELECT uid FROM support_msgs WHERE admin_id=? AND message_id=?",
                               (admin_id, message_id)).fetchone()
        return int(r["uid"]) if r else None

    def sec(self, rid: int) -> Optional[str]:
        with self.lock:
            r = self.c.execute("SELECT answer FROM sec_resp WHERE rid=?", (rid,)).fetchone()
        return r["answer"] if r else None

    def sec_set(self, rid: int, answer: str, now: float) -> None:
        self._run("INSERT OR REPLACE INTO sec_resp VALUES(?,?,?)", (rid, answer, now))


# ───────────────────────────── логика бота ─────────────────────────────
class PremiumBot:
    def __init__(self, cfg: Cfg, tg: Telegram, store: Store, fb, clock: Callable[[], float] = time.time):
        self.cfg, self.tg, self.store, self.fb, self.now = cfg, tg, store, fb, clock
        self._last_notice = 0.0

    # --- отправка ---
    def send(self, chat_id: int, text: str, markup: Optional[dict] = None) -> Optional[dict]:
        p: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup:
            p["reply_markup"] = markup
        return self.tg.call("sendMessage", **p)

    def answer(self, cq_id: str, text: str = "", alert: bool = False) -> None:
        self.tg.call("answerCallbackQuery", callback_query_id=cq_id, text=text, show_alert=alert)

    # --- тексты ---
    def requisites_text(self, pay, premium_until_ms: Optional[int]) -> str:
        c = self.cfg
        left = max(1, math.ceil((pay["expires"] - self.now()) / 60))
        extra = ""
        if premium_until_ms and premium_until_ms > self.now() * 1000:
            extra = (f"У вас уже есть Премиум до <b>{fmt_d(premium_until_ms)}</b>. "
                     f"Оплата продлит подписку ещё на 1 месяц.\n\n")
        return (
            f"💎 <b>Премиум на 1 месяц — {c.price} сомони</b>\n\n{extra}"
            f"Оплатите на карту:\n<code>{html.escape(c.card)}</code>\n"
            f"Получатель: <b>{html.escape(c.holder)}</b>\n"
            f"Сумма: <b>{c.price} сомони</b>\n"
            f"Комментарий к переводу: <code>{html.escape(c.note)}</code>\n\n"
            f"После оплаты отправьте сюда чек — фото или скриншот.\n"
            f"⏳ Время ожидания — <b>{left} мин.</b>")

    def admin_caption(self, pay, suffix: str = "") -> str:
        u = self.store.user(pay["user_id"])
        name = html.escape((u["name"] if u and u["name"] else "") or (u["tg_name"] if u else "") or "—")
        uname = f"@{html.escape(u['username'])}" if u and u["username"] else "без username"
        return (
            f"🧾 <b>Чек на подписку Премиум</b> · заявка #{pay['id']}\n"
            f"👤 Имя: <b>{name}</b>\n"
            f"📞 Номер: <b>{fmt_phone(u['phone'] if u else '')}</b>\n"
            f"✈️ Telegram: {uname} · ID <code>{pay['user_id']}</code> · "
            f"<a href=\"tg://user?id={pay['user_id']}\">открыть чат</a>\n"
            f"💰 {self.cfg.price} сомони · 1 месяц\n"
            f"🕒 {fmt_dt(pay['created'])}{suffix}")

    def retry_kb(self) -> dict:
        return kb([[("🔄 Повторить", "retry")]])

    # --- входящие ---
    def handle_update(self, u: dict) -> None:
        if "callback_query" in u:
            return self.on_callback(u["callback_query"])
        m = u.get("message")
        if m and m.get("chat", {}).get("type") == "private" and m.get("from"):
            self.on_message(m)

    def on_message(self, m: dict) -> None:
        frm, chat_id = m["from"], m["chat"]["id"]
        text = (m.get("text") or "").strip()
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            arg = arg.strip()
            if cmd == "/start":
                return self.cmd_start(m, arg)
            if cmd == "/status":
                return self.cmd_status(frm["id"], chat_id)
            if cmd == "/help":
                return self.send(chat_id, "Нажмите /start, чтобы оформить Премиум, или /status, чтобы узнать срок подписки.")
            if frm["id"] in self.cfg.admins:
                if cmd == "/grant":
                    return self.cmd_grant(chat_id, arg)
                if cmd == "/revoke":
                    return self.cmd_revoke(chat_id, arg)
                if cmd == "/pending":
                    return self.cmd_pending(chat_id)
            return
        if m.get("photo") or (m.get("document") or {}).get("mime_type", "").startswith(("image/", "application/pdf")):
            return self.on_receipt(m)
        if self.store.active_request(frm["id"], self.now()):
            self.send(chat_id, "Отправьте чек об оплате <b>фото или скриншотом</b>.")
        else:
            self.send(chat_id, "Чтобы оформить Премиум, нажмите /start.")

    def remember_user(self, frm: dict, phone: str = "", name: str = "") -> None:
        full = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        self.store.upsert_user(frm["id"], name, phone, frm.get("username") or "", full)

    def cmd_start(self, m: dict, payload: str) -> None:
        phone, name = parse_payload(payload)
        self.remember_user(m["from"], phone, name)
        self.offer(m["from"]["id"], m["chat"]["id"])

    def cmd_status(self, uid: int, chat_id: int) -> None:
        p = self.store.premium(uid)
        if p and p["until"] > self.now() * 1000:
            self.send(chat_id, f"✅ Премиум активен до <b>{fmt_d(p['until'])}</b>.",
                      kb([[("➕ Продлить на 1 месяц", "retry")]]))
        else:
            self.send(chat_id, "Премиум не активен.", kb([[("💎 Оформить подписку", "retry")]]))

    def offer(self, uid: int, chat_id: int) -> None:
        """Показать реквизиты (создать заявку на оплату, если активной ещё нет)."""
        if self.store.review_request(uid):
            return self._processing(chat_id)
        now = self.now()
        pay = self.store.active_request(uid, now)
        if not pay:
            pid = self.store.create_request(uid, chat_id, now, self.cfg.wait_min * 60)
            pay = self.store.payment(pid)
        prem = self.store.premium(uid)
        sent = self.send(chat_id, self.requisites_text(pay, prem["until"] if prem else None))
        if sent:
            self.store.set_prompt(pay["id"], sent["message_id"])

    def _processing(self, chat_id: int) -> None:
        self.send(chat_id, "⏳ Ваш платёж обрабатывается, пожалуйста, подождите.")

    # --- чек ---
    @staticmethod
    def _file_of(m: dict) -> tuple[str, str]:
        if m.get("photo"):
            return m["photo"][-1]["file_id"], "photo"
        return m["document"]["file_id"], "document"

    def _to_admins(self, pay, file_id: str, ftype: str, caption: str, markup: Optional[dict]) -> list[tuple[int, int]]:
        out = []
        for admin in self.cfg.admins:
            method, key = ("sendPhoto", "photo") if ftype == "photo" else ("sendDocument", "document")
            params: dict[str, Any] = {"chat_id": admin, key: file_id, "caption": caption, "parse_mode": "HTML"}
            if markup:
                params["reply_markup"] = markup
            r = self.tg.call(method, **params)
            if r:
                out.append((admin, r["message_id"]))
            else:
                log.error("Не удалось отправить чек админу %s (он должен нажать /start у этого бота)", admin)
        return out

    def _review_kb(self, pid: int) -> dict:
        return kb([[("✅ Подтвердить", f"ok:{pid}"), ("❌ Отказать", f"no:{pid}")]])

    def on_receipt(self, m: dict) -> None:
        uid, chat_id = m["from"]["id"], m["chat"]["id"]
        self.remember_user(m["from"])
        file_id, ftype = self._file_of(m)
        pay = self.store.active_request(uid, self.now())
        if pay:
            sent = self._to_admins(pay, file_id, ftype, self.admin_caption(pay), self._review_kb(pay["id"]))
            if not sent:
                self.send(chat_id, "⚠️ Не удалось передать чек администратору. Отправьте его ещё раз чуть позже.")
                return
            self.store.set_receipt(pay["id"], file_id, ftype)
            for admin, mid in sent:
                self.store.add_admin_msg(pay["id"], admin, mid)
            return self._processing(chat_id)
        rev = self.store.review_request(uid)
        if rev:  # дополнительный чек к уже поданной заявке
            if self.store.add_extra(rev["id"]) <= 3:
                self._to_admins(rev, file_id, ftype, f"📎 Дополнительный чек к заявке #{rev['id']}", None)
            return self._processing(chat_id)
        self.send(chat_id, "Запрос на подписку не найден или уже отменён. Если вы уже оплатили — нажмите «Повторить» "
                           "и отправьте чек ещё раз.", self.retry_kb())

    # --- кнопки ---
    def on_callback(self, cq: dict) -> None:
        data, frm = cq.get("data") or "", cq["from"]
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id", frm["id"])
        if data == "retry":
            self.answer(cq["id"])
            self.remember_user(frm)
            return self.offer(frm["id"], chat_id)
        m = re.fullmatch(r"(ok|no):(\d+)", data)
        if not m:
            return self.answer(cq["id"])
        if frm["id"] not in self.cfg.admins:
            return self.answer(cq["id"], "Нет доступа", True)
        self.decide(int(m.group(2)), m.group(1) == "ok", frm, cq["id"])

    def decide(self, pid: int, approve: bool, admin: dict, cq_id: str) -> None:
        pay = self.store.payment(pid)
        if not pay:
            return self.answer(cq_id, "Заявка не найдена", True)
        if pay["status"] != "review":
            labels = {"approved": "уже подтверждена", "rejected": "уже отклонена", "awaiting": "чек ещё не получен", "expired": "запрос отменён"}
            return self.answer(cq_id, f"Заявка #{pid}: {labels.get(pay['status'], pay['status'])}", True)
        uid, now = pay["user_id"], self.now()
        who = html.escape(admin.get("first_name") or admin.get("username") or str(admin["id"]))
        if approve:
            cur = self.store.premium(uid)
            base = max(int(now * 1000), cur["until"] if cur else 0)
            until = base + self.cfg.days * DAY_MS
            try:
                self.fb.set_premium(uid, until)
            except Exception:
                log.exception("Firebase: не удалось записать подписку %s", uid)
                return self.answer(cq_id, "❌ Не удалось записать подписку в Firebase. Проверьте настройки и нажмите ещё раз.", True)
            self.store.set_premium(uid, until)
            self.store.set_status(pid, "approved", admin["id"], now)
            self.answer(cq_id, "Подтверждено")
            markup = kb([[("📱 Открыть приложение", "url:" + self.cfg.app_url)]]) if self.cfg.app_url.startswith("http") else None
            self.send(pay["chat_id"],
                      f"🎉 <b>Подписка Премиум оформлена!</b>\nДействует до <b>{fmt_d(until)}</b>.\n"
                      f"Ваш профиль в приложении разблокирован — откройте ZINDAGI.", markup)
            self._mark_admin_cards(pid, f"\n\n✅ <b>Подтверждено</b> · {who} · {fmt_dt(now)}")
        else:
            self.store.set_status(pid, "rejected", admin["id"], now)
            self.answer(cq_id, "Отклонено")
            self.send(pay["chat_id"],
                      "❌ <b>Платёж не подтверждён.</b>\nПроверьте сумму и карту, затем нажмите «Повторить» и отправьте чек ещё раз.",
                      self.retry_kb())
            self._mark_admin_cards(pid, f"\n\n❌ <b>Отказано</b> · {who} · {fmt_dt(now)}")

    def _mark_admin_cards(self, pid: int, suffix: str) -> None:
        pay = self.store.payment(pid)
        caption = self.admin_caption(pay, suffix)
        for r in self.store.admin_msgs(pid):
            self.tg.call("editMessageCaption", chat_id=r["admin_id"], message_id=r["message_id"], caption=caption,
                         parse_mode="HTML", reply_markup={"inline_keyboard": []})

    # --- команды админа ---
    def cmd_grant(self, chat_id: int, arg: str) -> None:
        parts = arg.split()
        if not parts or not parts[0].isdigit():
            return self.send(chat_id, "Формат: /grant <telegram_id> [дней]")
        uid, days = int(parts[0]), int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else self.cfg.days
        cur = self.store.premium(uid)
        until = max(int(self.now() * 1000), cur["until"] if cur else 0) + days * DAY_MS
        try:
            self.fb.set_premium(uid, until)
        except Exception:
            log.exception("Firebase grant")
            return self.send(chat_id, "❌ Не удалось записать в Firebase, см. журнал бота.")
        self.store.set_premium(uid, until)
        self.send(uid, f"🎁 Вам активирован Премиум до <b>{fmt_d(until)}</b>. Откройте приложение ZINDAGI.")
        self.send(chat_id, f"✅ Премиум для <code>{uid}</code> до {fmt_d(until)}.")

    def cmd_revoke(self, chat_id: int, arg: str) -> None:
        if not arg.strip().isdigit():
            return self.send(chat_id, "Формат: /revoke <telegram_id>")
        uid = int(arg.strip())
        try:
            self.fb.clear_premium(uid)
        except Exception:
            log.exception("Firebase revoke")
            return self.send(chat_id, "❌ Не удалось удалить запись в Firebase, см. журнал бота.")
        self.store.del_premium(uid)
        self.send(chat_id, f"Премиум для <code>{uid}</code> отключён.")

    def cmd_pending(self, chat_id: int) -> None:
        rows = self.store.all_review()
        if not rows:
            return self.send(chat_id, "Заявок на проверке нет.")
        for pay in rows:
            r = self._to_admins_one(chat_id, pay)
            if not r:
                self.send(chat_id, f"Заявка #{pay['id']} (чек не удалось показать)")

    def _to_admins_one(self, admin_id: int, pay) -> bool:
        method, key = ("sendPhoto", "photo") if pay["file_type"] == "photo" else ("sendDocument", "document")
        r = self.tg.call(method, chat_id=admin_id, **{key: pay["file_id"]}, caption=self.admin_caption(pay),
                         parse_mode="HTML", reply_markup=self._review_kb(pay["id"]))
        if r:
            self.store.add_admin_msg(pay["id"], admin_id, r["message_id"])
        return bool(r)

    # --- таймеры ---
    def tick(self) -> None:
        now = self.now()
        for pay in self.store.expire_due(now):
            if pay["prompt_msg"]:
                self.tg.call("editMessageText", chat_id=pay["chat_id"], message_id=pay["prompt_msg"],
                             text="⌛ Запрос на оплату отменён.")
            self.send(pay["chat_id"],
                      "⌛ Время ожидания истекло. Ваш запрос на подписку премиума отменён.\n"
                      "Если вы уже оплатили — нажмите «Повторить» и отправьте чек.", self.retry_kb())
        if now - self._last_notice >= 60:
            self._last_notice = now
            self.subscription_notices()

    def subscription_notices(self) -> None:
        now_ms = int(self.now() * 1000)
        for r in self.store.expiring(now_ms, 3 * DAY_MS):
            self.send(r["user_id"], f"⏰ Ваш Премиум заканчивается <b>{fmt_d(r['until'])}</b>. "
                                    f"Продлите заранее — новый месяц добавится к текущему сроку.",
                      kb([[("➕ Продлить на 1 месяц", "retry")]]))
            self.store.mark(r["user_id"], "warned")
        for r in self.store.ended(now_ms):
            self.send(r["user_id"], "Срок вашего Премиума закончился. Функции приложения снова заблокированы.",
                      kb([[("💎 Оформить подписку", "retry")]]))
            self.store.mark(r["user_id"], "ended")
            try:
                self.fb.clear_premium(r["user_id"])
            except Exception:
                log.exception("Firebase clear")

    def poll_timeout(self) -> int:
        nxt = self.store.next_expiry()
        return 25 if nxt is None else max(1, min(25, math.ceil(nxt - self.now())))

    # --- главный цикл ---
    def run(self) -> None:
        me = self.tg.call("getMe")
        log.info("Бот запущен: @%s · админы: %s", (me or {}).get("username", "?"), self.cfg.admins)
        offset: Optional[int] = None
        while True:
            try:
                self.tick()
                for u in self.tg.get_updates(offset, self.poll_timeout()):
                    offset = u["update_id"] + 1
                    try:
                        self.handle_update(u)
                    except Exception:
                        log.exception("Ошибка при обработке обновления %s", u.get("update_id"))
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.error("Цикл опроса: %s", e)
                time.sleep(3)


# ───────────────────────────── бот входа ─────────────────────────────
# r|l + телефон (9 цифр у старой версии приложения, 11–12 с кодом страны у новой) + токен сессии
PAYLOAD_RE = re.compile(r"([rl])(\d{9,15})([0-9a-f]{32})")

# Причины отказа: (кнопка для админа, текст для пользователя)
REJECT_REASONS: list[tuple[str, str]] = [
    ("🪪 ФИО не совпадает", "ФИО не совпадает с данными аккаунта."),
    ("🧾 Чек не подходит", "Чек нечитаемый или не относится к этому аккаунту."),
    ("📵 Неверный номер Telegram", "Указан неверный номер Telegram, на который вы регистрировались."),
    ("📝 Недостаточно данных", "Недостаточно данных, чтобы подтвердить аккаунт."),
    ("🚫 Не удалось подтвердить владельца", "Не удалось подтвердить, что аккаунт принадлежит вам."),
]
ACCESS_FLOW_TTL = 3600          # диалог «нет доступа» сбрасывается, если человек молчит час
ACCESS_MAX_PER_DAY = 3          # не больше трёх заявок в сутки от одного Telegram


def mask_phone(p: str) -> str:
    """+992 90****123 — первые 2 и последние 3 цифры номера (код страны виден целиком)."""
    cc, n = split_phone(p)
    return f"+{cc} {n[:2]}****{n[-3:]}" if cc else ""


def wrap_token(token: str, code: str, secret: str) -> dict:
    """Шифрует Firebase-токен ключом из (токен сессии + код из Telegram): расшифровать его в приложении
    можно только введя верный код. Ключ = SHA-256("zk" + token + code), шифр AES-256-GCM."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = hashlib.sha256(("zk" + token + code).encode()).digest()
    iv = secrets.token_bytes(12)
    data = AESGCM(key).encrypt(iv, secret.encode(), None)
    return {"iv": base64.b64encode(iv).decode(), "d": base64.b64encode(data).decode()}


class AccessConflict(Exception):
    """Подключить номер нельзя — причина показывается админу."""


class LoginBot:
    """
    Бот входа/регистрации ZINDAGI. Вся логика — на сервере, токена бота в приложении больше нет.

    Приложение открывает t.me/<бот>?start=<r|l><телефон><токен>. Бот:
      • смотрит, к какому Telegram привязан этот номер (Firebase: phones/<номер> = tg-id; tgusers/<tg-id> = {...});
      • при регистрации привязывает номер к этому Telegram (один номер ↔ один Telegram);
      • шлёт код в чат и кладёт его хэш в sessions/<токен> — приложение читает этот узел и сверяет код;
      • если номер привязан к другому Telegram — сообщает об этом и показывает маску: +992 90****123,
        и даёт кнопку «Нет доступа к Telegram» (см. ниже).
    Номер хранится с кодом страны (992…, 998…, 7…). Старые таджикские ключи phones/<9 цифр> читаются как раньше.

    «Нет доступа к Telegram»: бот по очереди спрашивает ФИО, номер, на который регистрировался Telegram, и фото
    последнего платежа → заявка уходит админам с кнопками «Подключить» / «Отказ». «Подключить» переносит номер,
    подписку и профиль на новый Telegram (новый аккаунт не создаётся), «Отказ» — на выбор причины.
    Приложение просит отправить уведомления через outbox/ — бот пересылает их только зарегистрированным чатам.
    """
    CODE_TTL_MS = 5 * 60_000
    RESEND_MS = 30_000

    def __init__(self, cfg: Cfg, tg: Telegram, fb: Firebase, store: Optional[Store] = None,
                 access: Optional[AccessStore] = None, clock: Callable[[], float] = time.time):
        self.cfg, self.tg, self.fb, self.store, self.a, self.now = cfg, tg, fb, store, access, clock
        self._sent: dict[str, list[float]] = {}

    def send(self, chat_id: int, text: str, markup: Optional[dict] = None, html_mode: bool = False) -> Optional[dict]:
        p: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if markup:
            p["reply_markup"] = markup
        if html_mode:
            p["parse_mode"] = "HTML"
        return self.tg.call("sendMessage", **p)

    def answer(self, cq_id: str, text: str = "", alert: bool = False) -> None:
        self.tg.call("answerCallbackQuery", callback_query_id=cq_id, text=text, show_alert=alert)

    # --- кто владеет номером ---
    def owner_of(self, phone: str) -> tuple[Optional[int], str]:
        """→ (tg-id владельца | None, ключ узла phones/, где он лежит)."""
        for key in phone_keys(phone):
            v = self.fb.get(f"phones/{key}")
            if v is not None:
                return int(v), key
        return None, ""

    # --- решение: можно ли выдать код ---
    def decide(self, mode: str, phone: str, uid: int, frm: dict) -> tuple[str, str, bool]:
        """→ (status, hint, returning). status: ok | phone_taken | tg_has_other | not_found."""
        if self.fb.get(f"blocked/phone/{phone}") is not None or self.fb.get(f"blocked/uid/{uid}") is not None:
            return "blocked", "", False
        owner, _ = self.owner_of(phone)
        mine = self.fb.get(f"tgusers/{uid}") or {}
        my_phone = canon_phone(mine.get("phone"))
        if owner is not None and owner == uid:
            return "ok", "", True
        if owner is not None:
            return "phone_taken", mask_phone(my_phone), False
        if my_phone and my_phone != phone:
            return "tg_has_other", mask_phone(my_phone), False
        if mode == "l":
            return "not_found", "", False
        if not self.fb.claim(f"phones/{phone}", uid):  # кто-то успел раньше
            return "phone_taken", "", False
        full = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        self.fb.set(f"tgusers/{uid}", {"phone": phone, "name": full, "username": frm.get("username") or "",
                                       "chat": uid, "ts": int(time.time() * 1000)})
        who = f"{full} · {fmt_phone(phone)}" + (f" · @{frm['username']}" if frm.get("username") else "")
        for admin in self.cfg.admins:
            self.send(admin, f"🆕 Новая регистрация: {who} · id {uid}")
        return "ok", "", False

    def on_start(self, m: dict, payload: str) -> None:
        frm, chat = m["from"], m["chat"]["id"]
        uid = frm["id"]
        if self.a:
            self.a.flow_del(uid)  # /start из приложения прерывает незаконченную заявку
        mt = PAYLOAD_RE.fullmatch(payload or "")
        if not mt:
            self.send(chat, "👋 Это бот входа ZINDAGI. Откройте приложение и нажмите «Продолжить» или «Войти» — код придёт сюда.")
            return
        mode, raw_phone, token = mt.groups()
        phone = canon_phone(raw_phone)
        if not phone:
            self.send(chat, "Номер указан неверно. Вернитесь в приложение и введите его ещё раз.")
            return
        key = f"sessions/{token}"
        now = int(time.time() * 1000)
        cur = self.fb.get(key)
        if isinstance(cur, dict):
            if cur.get("tgId") not in (None, uid):
                self.send(chat, "Эта ссылка уже использована другим пользователем. Вернитесь в приложение и начните заново.")
                return
            if cur.get("status") == "ok" and now < int(cur.get("resendAt") or 0):
                return  # защита от двойного нажатия Start
        status, hint, returning = self.decide(mode, phone, uid, frm)
        base = {"status": status, "hint": hint, "tgId": uid, "chatId": chat, "tgName": frm.get("first_name") or "",
                "tgUser": frm.get("username") or "", "returning": returning, "ts": now}
        if status != "ok":
            self.fb.set(key, base)
            markup = None
            if status == "phone_taken":
                text = f"❌ Номер {mask_phone(phone)} уже зарегистрирован на другом Telegram-аккаунте.\nВойдите с того Telegram."
                if hint:
                    text += f"\nЭтот Telegram привязан к номеру {hint}."
                elif self.a:
                    text += "\n\nЕсли у вас больше нет доступа к тому Telegram — нажмите кнопку ниже."
                    markup = kb([[("🚫 Нет доступа к Telegram", f"na:{phone}")]])
            elif status == "blocked":
                text = ("🔒 Аккаунт ZINDAGI заблокирован по запросу владельца.\n"
                        "Чтобы разобраться и вернуть доступ, нажмите кнопку ниже.")
                markup = kb([[("📞 Связаться со службой поддержки", "sp:go")]])
            elif status == "tg_has_other":
                text = f"❌ Этот Telegram уже привязан к номеру {hint}. Укажите именно этот номер."
            else:
                text = f"Аккаунт ZINDAGI с номером {mask_phone(phone)} не найден. Вернитесь в приложение и зарегистрируйтесь."
            self.send(chat, text, markup)
            return
        code = str(secrets.randbelow(900_000) + 100_000)
        u = self.fb.get(f"tgusers/{uid}") or {}
        base.update({"codeHash": hashlib.sha256((token + code).encode()).hexdigest(),
                     "expiresAt": now + self.CODE_TTL_MS, "resendAt": now + self.RESEND_MS,
                     "name": u.get("name") or frm.get("first_name") or ""})
        try:
            base["ct"] = wrap_token(token, code, self.fb.custom_token(uid))
        except Exception:
            log.exception("custom token не создан — синхронизация данных с облаком недоступна")
        name = frm.get("first_name") or ""
        sent = self.send(chat, f"👋 {name + ', здравствуйте!' if name else 'Здравствуйте!'}\n"
                               f"🔐 Ваш код для входа в ZINDAGI: {code}\n\nДействует 5 минут. Никому не сообщайте.")
        if sent:
            self.fb.set(key, base)

    # ───────────── «Нет доступа к Telegram»: заявка в поддержку ─────────────
    def _cancel_kb(self) -> dict:
        return kb([[("✖️ Отмена", "nac")]])

    def start_access_flow(self, cq: dict, phone: str) -> None:
        frm = cq["from"]
        uid = frm["id"]
        chat = (cq.get("message") or {}).get("chat", {}).get("id", uid)
        if not self.a:
            return self.answer(cq["id"])
        # кнопка работает только пока номер и правда занят ДРУГИМ Telegram, а этот Telegram свободен
        owner, _ = self.owner_of(phone)
        mine = canon_phone((self.fb.get(f"tgusers/{uid}") or {}).get("phone"))
        if owner is None or owner == uid or (mine and mine != phone):
            self.answer(cq["id"], "Эта заявка сейчас недоступна. Вернитесь в приложение и начните заново.", True)
            return
        if self.a.pending_of(uid, phone):
            self.answer(cq["id"])
            self.send(chat, "⏳ Ваша заявка уже отправлена в службу поддержки. Ожидайте ответа.")
            return
        if self.a.recent_count(uid, self.now() - 86400) >= ACCESS_MAX_PER_DAY:
            self.answer(cq["id"])
            self.send(chat, "Вы уже отправляли несколько заявок за сутки. Попробуйте позже или дождитесь ответа поддержки.")
            return
        self.answer(cq["id"])
        self.a.flow_start(uid, chat, phone, self.now())
        self.send(chat, "🔐 Подтвердите, что это вы. Я задам 3 вопроса по очереди: ФИО, номер Telegram, на который "
                        "вы регистрировались, и фото последнего платежа.")
        self.send(chat, "1️⃣ Отправьте ваше ФИО (фамилия, имя, отчество).", self._cancel_kb())

    def cancel_access_flow(self, cq: dict) -> None:
        if self.a:
            self.a.flow_del(cq["from"]["id"])
        self.answer(cq["id"], "Отменено")
        self.send((cq.get("message") or {}).get("chat", {}).get("id", cq["from"]["id"]),
                  "Заявка отменена. Откройте приложение, если захотите попробовать снова.")

    @staticmethod
    def _valid_name(text: str) -> bool:
        words = text.split()
        letters = len(re.findall(r"[^\W\d_]", text))
        return 2 <= len(words) <= 6 and letters >= 4 and not re.search(r"\d", text) and len(text) <= 100

    def on_flow_message(self, m: dict, flow: sqlite3.Row) -> None:
        uid, chat = m["from"]["id"], m["chat"]["id"]
        now = self.now()
        if now - flow["updated"] > ACCESS_FLOW_TTL:
            self.a.flow_del(uid)
            return self.send(chat, "Время ожидания истекло. Откройте приложение и начните заново.")
        text = (m.get("text") or "").strip()
        step = flow["step"]
        if step == "name":
            if not self._valid_name(text):
                return self.send(chat, "Пожалуйста, отправьте ФИО текстом — минимум фамилия и имя, без цифр.",
                                 self._cancel_kb())
            self.a.flow_set(uid, now, name=" ".join(text.split()), step="tgphone")
            return self.send(chat, "2️⃣ Отправьте номер телефона, на который был зарегистрирован ваш Telegram "
                                   "(с кодом страны, например +992 90 123 45 67).", self._cancel_kb())
        if step == "tgphone":
            phone = parse_any_phone(text)
            if not phone:
                return self.send(chat, "Не похоже на номер телефона. Отправьте его с кодом страны, "
                                       "например +992 90 123 45 67.", self._cancel_kb())
            self.a.flow_set(uid, now, tg_phone=phone, step="receipt")
            return self.send(chat, "3️⃣ Отправьте фото чека последнего платежа.", self._cancel_kb())
        if step == "receipt":
            doc = m.get("document") or {}
            if m.get("photo"):
                file_id, ftype = m["photo"][-1]["file_id"], "photo"
            elif doc.get("mime_type", "").startswith(("image/", "application/pdf")):
                file_id, ftype = doc["file_id"], "document"
            else:
                return self.send(chat, "Отправьте именно фото (или скриншот) чека последнего платежа.", self._cancel_kb())
            self.finish_access_flow(m["from"], flow, file_id, ftype)

    def _owner_summary(self, phone: str) -> tuple[Optional[int], str]:
        """Для карточки админа: кто сейчас владеет номером и есть ли у него подписка."""
        try:
            owner, _ = self.owner_of(phone)
            if owner is None:
                return None, "—"
            until = int((self.fb.get(f"premium/{owner}") or {}).get("until") or 0)
            prem = f"💎 Премиум до {fmt_d(until)}" if until > self.now() * 1000 else "без подписки"
            return owner, prem
        except Exception:
            log.exception("Firebase: владелец номера")
            return None, "—"

    def access_caption(self, req: sqlite3.Row, suffix: str = "") -> str:
        owner, prem = self._owner_summary(req["phone"])
        uname = f"@{html.escape(req['username'])}" if req["username"] else "без username"
        others = self.a.others_pending(req["phone"], req["id"])
        warn = (f"\n⚠️ Есть другие заявки на этот номер: {', '.join('#' + str(i) for i in others)}" if others else "")
        return (
            f"🆘 <b>Нет доступа к Telegram</b> · заявка #{req['id']}\n"
            f"👤 ФИО: <b>{html.escape(req['name'] or '—')}</b>\n"
            f"📞 Номер в приложении: <b>{fmt_phone(req['phone'])}</b>\n"
            f"📱 Telegram был зарегистрирован на: <b>{html.escape(req['tg_phone'] or '—')}</b>\n"
            f"✈️ Новый Telegram: {uname} · ID <code>{req['user_id']}</code> · "
            f"<a href=\"tg://user?id={req['user_id']}\">открыть чат</a>\n"
            f"🔗 Сейчас номер привязан к ID <code>{owner if owner is not None else '—'}</code> · {prem}"
            f"{warn}\n🕒 {fmt_dt(req['created'])}{suffix}")

    @staticmethod
    def _decision_kb(rid: int) -> dict:
        return kb([[("✅ Подключить", f"ap:{rid}"), ("❌ Отказ", f"rj:{rid}")]])

    def finish_access_flow(self, frm: dict, flow: sqlite3.Row, file_id: str, ftype: str) -> None:
        uid, chat = frm["id"], flow["chat_id"]
        full = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        rid = self.a.req_create(flow, frm.get("username") or "", full, file_id, ftype, self.now())
        req = self.a.req(rid)
        method, key = ("sendPhoto", "photo") if ftype == "photo" else ("sendDocument", "document")
        caption = self.access_caption(req)
        delivered = 0
        for admin in self.cfg.admins:
            r = self.tg.call(method, chat_id=admin, **{key: file_id}, caption=caption, parse_mode="HTML",
                             reply_markup=self._decision_kb(rid))
            if r:
                self.a.add_admin_msg(rid, admin, r["message_id"])
                delivered += 1
            else:
                log.error("Не удалось отправить заявку админу %s (он должен нажать /start у бота входа)", admin)
        if not delivered:
            self.a.req_delete(rid)
            return self.send(chat, "⚠️ Не удалось передать заявку в службу поддержки. Отправьте фото чека ещё раз чуть позже.",
                             self._cancel_kb())
        self.a.req_set(rid, status="pending")
        self.a.flow_del(uid)
        self.send(chat, "✅ Ваша заявка принята и отправлена в службу поддержки.\n"
                        "Ожидайте ответа службы поддержки. Спасибо за обращение!")

    # --- решение админа ---
    def _mark_cards(self, rid: int, suffix: str) -> None:
        caption = self.access_caption(self.a.req(rid), suffix)
        for r in self.a.admin_msgs(rid):
            self.tg.call("editMessageCaption", chat_id=r["admin_id"], message_id=r["message_id"], caption=caption,
                         parse_mode="HTML", reply_markup={"inline_keyboard": []})

    def connect_phone(self, req: sqlite3.Row) -> int:
        """Привязать номер из заявки к новому Telegram. Возвращает tg-id прежнего владельца (0 — не было).

        Новый аккаунт не создаётся: тот же номер → тот же профиль и та же подписка, только Telegram другой.
        Можно безопасно повторить после сбоя: прежний владелец запоминается в заявке."""
        phone, new_uid, rid = req["phone"], req["user_id"], req["id"]
        mine = self.fb.get(f"tgusers/{new_uid}") or {}
        if canon_phone(mine.get("phone")) not in ("", phone):
            raise AccessConflict("Этот Telegram уже привязан к другому номеру.")
        old_uid = req["old_uid"]
        if old_uid is None:
            owner, _ = self.owner_of(phone)
            old_uid = owner if owner not in (None, new_uid) else 0
            self.a.req_set(rid, old_uid=old_uid)
        old = (self.fb.get(f"tgusers/{old_uid}") or {}) if old_uid else {}
        now_ms = int(self.now() * 1000)

        # подписка: переносим на новый Telegram (если на новом уже есть — берём более позднюю дату)
        until = 0
        if old_uid:
            until = max(int((self.fb.get(f"premium/{old_uid}") or {}).get("until") or 0),
                        self.store.premium_until(old_uid) if self.store else 0)
        until = max(until, int((self.fb.get(f"premium/{new_uid}") or {}).get("until") or 0),
                    self.store.premium_until(new_uid) if self.store else 0)
        if until > now_ms:
            self.fb.set_premium(new_uid, until)
        else:
            until = 0
        if old_uid:
            self.fb.clear_premium(old_uid)
        if self.store and old_uid:
            self.store.move_premium(old_uid, new_uid, until)

        # номер и профиль
        self.fb.set(f"phones/{phone}", new_uid)
        for key in phone_keys(phone)[1:]:           # старый 9-значный ключ больше не нужен
            self.fb.delete(f"phones/{key}")
        self.fb.set(f"tgusers/{new_uid}", {
            "phone": phone, "name": old.get("name") or req["name"] or req["tg_name"] or "",
            "username": req["username"] or "", "chat": new_uid, "ts": now_ms, "movedFrom": old_uid or None})
        if old_uid:
            self.fb.delete(f"tgusers/{old_uid}")     # старый Telegram больше не получает уведомлений аккаунта
            # данные приложения (users/<tg-id>) переезжают вместе с номером — на новом Telegram всё будет на месте
            saved = self.fb.get(f"users/{old_uid}")
            if saved and self.fb.get(f"users/{new_uid}") is None:
                self.fb.set(f"users/{new_uid}", saved)
            self.fb.delete(f"users/{old_uid}")
        return int(old_uid or 0)

    def approve_access(self, rid: int, admin: dict, cq_id: str) -> None:
        req = self.a.req(rid)
        if not req:
            return self.answer(cq_id, "Заявка не найдена", True)
        if req["status"] != "pending":
            return self.answer(cq_id, f"Заявка #{rid} уже обработана", True)
        try:
            old_uid = self.connect_phone(req)
        except AccessConflict as e:
            return self.answer(cq_id, f"{e} Отклоните заявку.", True)
        except Exception:
            log.exception("Подключение номера по заявке #%s", rid)
            return self.answer(cq_id, "❌ Не удалось записать в Firebase. Проверьте настройки и нажмите ещё раз.", True)
        now = self.now()
        self.a.req_decide(rid, "approved", admin["id"], None, now)
        self.answer(cq_id, "Подключено")
        markup = kb([[("📱 Открыть приложение", "url:" + self.cfg.app_url)]]) if self.cfg.app_url.startswith("http") else None
        self.send(req["chat_id"], f"✅ Ваш аккаунт подтверждён!\nЗаходите заново в приложение ZINDAGI.\n\n"
                                  f"Номер вашего аккаунта: {fmt_phone(req['phone'])}", markup)
        if old_uid and old_uid != req["user_id"]:
            self.send(old_uid, f"⚠️ Аккаунт ZINDAGI {mask_phone(req['phone'])} перенесён на другой Telegram по заявке "
                               f"в службу поддержки. Если это были не вы — срочно обратитесь в поддержку.",
                      kb([[("✅ Это я", f"me:{rid}"), ("🚫 Это не я", f"ne:{rid}")]]))
        who = html.escape(admin.get("first_name") or admin.get("username") or str(admin["id"]))
        self._mark_cards(rid, f"\n\n✅ <b>Подключено</b> · {who} · {fmt_dt(now)}")

    def ask_reason(self, rid: int, admin: dict, cq: dict) -> None:
        req = self.a.req(rid)
        if not req or req["status"] != "pending":
            return self.answer(cq["id"], "Заявка уже обработана", True)
        rows = [[(label, f"rr:{rid}:{i}")] for i, (label, _) in enumerate(REJECT_REASONS)]
        rows.append([("↩️ Назад", f"rb:{rid}")])
        msg = cq.get("message") or {}
        self.answer(cq["id"], "Выберите причину отказа")
        self.tg.call("editMessageReplyMarkup", chat_id=msg.get("chat", {}).get("id", admin["id"]),
                     message_id=msg.get("message_id"), reply_markup=kb(rows))

    def back_from_reason(self, rid: int, cq: dict) -> None:
        msg = cq.get("message") or {}
        self.answer(cq["id"])
        self.tg.call("editMessageReplyMarkup", chat_id=msg.get("chat", {}).get("id", cq["from"]["id"]),
                     message_id=msg.get("message_id"), reply_markup=self._decision_kb(rid))

    def reject_access(self, rid: int, idx: int, admin: dict, cq_id: str) -> None:
        req = self.a.req(rid)
        if not req:
            return self.answer(cq_id, "Заявка не найдена", True)
        if req["status"] != "pending":
            return self.answer(cq_id, f"Заявка #{rid} уже обработана", True)
        if not 0 <= idx < len(REJECT_REASONS):
            return self.answer(cq_id)
        label, text = REJECT_REASONS[idx]
        now = self.now()
        self.a.req_decide(rid, "rejected", admin["id"], text, now)
        self.answer(cq_id, "Отказано")
        self.send(req["chat_id"], f"❌ Заявка отклонена.\nПричина: {text}\n\n"
                                  f"Если вы считаете, что это ошибка, откройте приложение и подайте заявку заново.")
        who = html.escape(admin.get("first_name") or admin.get("username") or str(admin["id"]))
        self._mark_cards(rid, f"\n\n❌ <b>Отказ</b> · {who} · {fmt_dt(now)}\nПричина: {html.escape(text)}")

    def on_callback(self, cq: dict) -> None:
        data, frm = cq.get("data") or "", cq["from"]
        if (mt := re.fullmatch(r"na:(\d{11,12})", data)):
            return self.start_access_flow(cq, mt.group(1))
        if data == "nac":
            return self.cancel_access_flow(cq)
        if data == "sp:go":
            self.answer(cq["id"])
            return self.start_support((cq.get("message") or {}).get("chat", {}).get("id", frm["id"]), frm)
        if (mt := re.fullmatch(r"(me|ne):(\d+)", data)) and self.a:
            return self.security_answer(cq, mt.group(1) == "me", int(mt.group(2)))
        if (mt := re.fullmatch(r"(ub|rv):(\d+)", data)) and self.a:
            if frm["id"] not in self.cfg.admins:
                return self.answer(cq["id"], "Нет доступа", True)
            return self.block_admin_action(cq, mt.group(1), int(mt.group(2)))
        mt = re.fullmatch(r"(ap|rj|rb|rr):(\d+)(?::(\d+))?", data)
        if not mt or not self.a:
            return self.answer(cq["id"])
        if frm["id"] not in self.cfg.admins:
            return self.answer(cq["id"], "Нет доступа", True)
        kind, rid = mt.group(1), int(mt.group(2))
        if kind == "ap":
            self.approve_access(rid, frm, cq["id"])
        elif kind == "rj":
            self.ask_reason(rid, frm, cq)
        elif kind == "rb":
            self.back_from_reason(rid, cq)
        else:
            self.reject_access(rid, int(mt.group(3) or -1), frm, cq["id"])

    # ───────────── «Это я / Это не я»: защита от угона аккаунта ─────────────
    def block_account(self, req: sqlite3.Row) -> None:
        now_ms = int(self.now() * 1000)
        info = {"rid": req["id"], "by": req["old_uid"], "ts": now_ms}
        self.fb.set(f"blocked/phone/{req['phone']}", {**info, "uid": req["user_id"]})
        self.fb.set(f"blocked/uid/{req['user_id']}", {**info, "phone": req["phone"]})
        try:
            self.fb.revoke(req["user_id"])
        except Exception:
            log.exception("Не удалось завершить сессии Firebase пользователя %s", req["user_id"])

    def unblock_account(self, req: sqlite3.Row) -> None:
        self.fb.delete(f"blocked/phone/{req['phone']}")
        self.fb.delete(f"blocked/uid/{req['user_id']}")

    def security_answer(self, cq: dict, yes: bool, rid: int) -> None:
        frm, req = cq["from"], self.a.req(rid)
        if not req or not req["old_uid"] or frm["id"] != int(req["old_uid"]):
            return self.answer(cq["id"], "Недоступно", True)
        if self.a.sec(rid):
            return self.answer(cq["id"], "Вы уже ответили", True)
        self.a.sec_set(rid, "yes" if yes else "no", self.now())
        msg = cq.get("message") or {}
        chat = msg.get("chat", {}).get("id", frm["id"])
        self.answer(cq["id"])
        self.tg.call("editMessageReplyMarkup", chat_id=chat, message_id=msg.get("message_id"))  # убрать кнопки
        if yes:
            return self.send(chat, "✅ Спасибо, подтвердили. Если понадобится помощь — команда /support.")
        try:
            self.block_account(req)
        except Exception:
            log.exception("Блокировка по заявке #%s", rid)
            return self.send(chat, "⚠️ Не удалось заблокировать автоматически. Мы уже передали сигнал в службу поддержки.")
        full = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        self.a.sup_open(frm["id"], chat, full, frm.get("username") or "", req["phone"])
        self.send(chat, "🔒 Не переживайте! Вашего аккаунта я заблокировал и отправил запрос в службу поддержки.\n"
                        "В ближайшее время мы свяжемся с вами и поможем вернуть доступ.\n\n"
                        "Можете сразу написать сюда подробности — сообщения получит служба поддержки.")
        old_n = f"@{frm['username']}" if frm.get("username") else "без username"
        new_n = f"@{req['username']}" if req["username"] else "без username"
        text = (f"🚨 <b>«Это не я»</b> · заявка #{rid}\n"
                f"📞 Номер: <b>{fmt_phone(req['phone'])}</b>\n"
                f"👤 Пожаловался (прежний владелец): {html.escape(full or '—')} · {old_n} · ID <code>{frm['id']}</code> · "
                f"<a href=\"tg://user?id={frm['id']}\">чат</a>\n"
                f"✈️ Новый Telegram: {html.escape(req['tg_name'] or '—')} · {new_n} · ID <code>{req['user_id']}</code> · "
                f"<a href=\"tg://user?id={req['user_id']}\">чат</a>\n"
                f"🔒 Аккаунт заблокирован: вход и облако закрыты.\n\n"
                f"↩️ Ответьте <b>реплаем</b> на это сообщение — ответ уйдёт прежнему владельцу, и диалог начнётся.")
        mk = kb([[("🔓 Разблокировать", f"ub:{rid}"), ("↩️ Вернуть владельцу", f"rv:{rid}")]])
        for admin in self.cfg.admins:
            r = self.tg.call("sendMessage", chat_id=admin, text=text, parse_mode="HTML",
                             disable_web_page_preview=True, reply_markup=mk)
            if r:
                self.a.sup_map_add(admin, r["message_id"], frm["id"])

    def revert_phone(self, req: sqlite3.Row) -> None:
        """Вернуть номер, профиль, данные и подписку прежнему владельцу."""
        old_uid, new_uid, phone = int(req["old_uid"]), int(req["user_id"]), req["phone"]
        now_ms = int(self.now() * 1000)
        cur = self.fb.get(f"tgusers/{new_uid}") or {}
        until = max(int((self.fb.get(f"premium/{new_uid}") or {}).get("until") or 0),
                    self.store.premium_until(new_uid) if self.store else 0)
        self.fb.set(f"phones/{phone}", old_uid)
        self.fb.set(f"tgusers/{old_uid}", {"phone": phone, "name": cur.get("name") or "", "username": "",
                                           "chat": old_uid, "ts": now_ms})
        self.fb.delete(f"tgusers/{new_uid}")
        saved = self.fb.get(f"users/{new_uid}")
        if saved and self.fb.get(f"users/{old_uid}") is None:
            self.fb.set(f"users/{old_uid}", saved)
        self.fb.delete(f"users/{new_uid}")
        if until > now_ms:
            self.fb.set_premium(old_uid, until)
        else:
            until = 0
        self.fb.clear_premium(new_uid)
        if self.store:
            self.store.move_premium(new_uid, old_uid, until)
        self.unblock_account(req)

    def block_admin_action(self, cq: dict, kind: str, rid: int) -> None:
        req = self.a.req(rid)
        if not req or not req["old_uid"]:
            return self.answer(cq["id"], "Заявка не найдена", True)
        msg = cq.get("message") or {}
        try:
            if kind == "ub":
                self.unblock_account(req)
                note, done = "🔓 Разблокировано", "🔓 <b>Разблокировано</b>"
            else:
                self.revert_phone(req)
                note, done = "↩️ Возвращено владельцу", "↩️ <b>Номер возвращён прежнему владельцу</b>"
                markup = kb([[("📱 Открыть приложение", "url:" + self.cfg.app_url)]]) if self.cfg.app_url.startswith("http") else None
                self.send(int(req["old_uid"]), "✅ Доступ к вашему аккаунту ZINDAGI возвращён. Заходите в приложение — все данные на месте.", markup)
                self.send(int(req["user_id"]), "ℹ️ Аккаунт ZINDAGI возвращён прежнему владельцу по решению службы поддержки.")
        except Exception:
            log.exception("Действие %s по заявке #%s", kind, rid)
            return self.answer(cq["id"], "❌ Не удалось выполнить. Проверьте Firebase и повторите.", True)
        self.answer(cq["id"], note)
        who = html.escape(cq["from"].get("first_name") or str(cq["from"]["id"]))
        self.tg.call("editMessageText", chat_id=msg.get("chat", {}).get("id", cq["from"]["id"]), message_id=msg.get("message_id"),
                     text=(msg.get("text") or "") + f"\n\n{re.sub('<[^>]+>', '', done)} · {who}", disable_web_page_preview=True)

    # ───────────── Служба поддержки ─────────────
    SUPPORT_WAIT = 600  # 10 минут тишины после ответа админа → «разговор завершён»

    def start_support(self, chat: int, frm: dict) -> None:
        uid = frm["id"]
        if not self.a:
            return
        u = self.fb.get(f"tgusers/{uid}") or {}
        full = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        self.a.sup_open(uid, chat, full, frm.get("username") or "", canon_phone(u.get("phone")) or "")
        self.send(chat, "💬 Вы на связи со службой поддержки.\n"
                        "Напишите сообщение (можно приложить фото) — мы передадим его специалисту, ответ придёт сюда.\n"
                        "Завершить диалог: /cancel")

    def _sup_head(self, s: sqlite3.Row) -> str:
        who = f"@{s['username']}" if s["username"] else "без username"
        return (f"💬 <b>Служба поддержки</b>\n👤 {html.escape(s['name'] or '—')} · {who} · ID <code>{s['uid']}</code>"
                + (f"\n📞 {fmt_phone(s['phone'])}" if s["phone"] else "") + "\n↩️ Ответьте реплаем на это сообщение.")

    def user_to_support(self, m: dict) -> None:
        uid, chat = m["from"]["id"], m["chat"]["id"]
        s = self.a.sup(uid)
        if not s or s["state"] != "open":
            return self.send(chat, "Чтобы написать в службу поддержки, нажмите кнопку ниже.",
                             kb([[("📞 Связаться", "sp:go")]]))
        head, text, delivered = self._sup_head(s), m.get("text"), 0
        for admin in self.cfg.admins:
            if text:
                sent = [self.tg.call("sendMessage", chat_id=admin, parse_mode="HTML",
                                     text=head + "\n\n" + html.escape(text[:3500]))]
            else:
                sent = [self.tg.call("sendMessage", chat_id=admin, parse_mode="HTML", text=head),
                        self.tg.call("copyMessage", chat_id=admin, from_chat_id=chat, message_id=m["message_id"])]
            for r in sent:
                if r:
                    self.a.sup_map_add(admin, r["message_id"], uid)
                    delivered += 1
        if not delivered:
            return self.send(chat, "⚠️ Не удалось передать сообщение. Попробуйте чуть позже.")
        self.a.sup_touch(uid, "user", self.now())

    def admin_to_user(self, m: dict, uid: int) -> None:
        s = self.a.sup(uid)
        if not s:
            return self.send(m["chat"]["id"], "Диалог с этим пользователем не найден.")
        chat = s["chat_id"]
        if m.get("text"):
            ok = self.send(chat, "💬 <b>Служба поддержки:</b>\n" + html.escape(m["text"]), html_mode=True)
        else:
            self.send(chat, "💬 Служба поддержки отправила вам файл:")
            ok = self.tg.call("copyMessage", chat_id=chat, from_chat_id=m["chat"]["id"], message_id=m["message_id"])
        if not ok:
            return self.send(m["chat"]["id"], "⚠️ Не доставлено: пользователь закрыл бота или заблокировал его.")
        self.a.sup_touch(uid, "admin", self.now())
        self.tg.call("sendMessage", chat_id=m["chat"]["id"], reply_to_message_id=m["message_id"],
                     text="✅ Доставлено. Если пользователь не ответит 10 минут — диалог закроется сам.")

    def support_loop(self) -> None:
        while True:
            try:
                for s in self.a.sup_due(self.now(), self.SUPPORT_WAIT):
                    self.a.sup_close(s["uid"])
                    self.send(s["chat_id"], "✅ Разговор завершён.\nДля обратной связи со службой поддержки нажмите кнопку ниже.",
                              kb([[("📞 Связаться", "sp:go")]]))
            except Exception:
                log.exception("support_loop")
            time.sleep(15)

    # --- уведомления из приложения ---
    def drain_outbox(self) -> None:
        box = self.fb.get("outbox")
        if not isinstance(box, dict):
            return
        now = time.time()
        for k, item in box.items():
            self.fb.delete(f"outbox/{k}")
            if not isinstance(item, dict):
                continue
            chat, text = str(item.get("chat", "")), str(item.get("text", ""))[:500]
            if not chat.isdigit() or not text.strip() or self.fb.get(f"tgusers/{chat}") is None:
                continue  # писать можно только зарегистрированным
            hist = [t for t in self._sent.get(chat, []) if now - t < 3600]
            if len(hist) >= 30:
                continue
            hist.append(now)
            self._sent[chat] = hist
            p: dict[str, Any] = {"chat_id": int(chat), "text": text, "disable_web_page_preview": True}
            btn = item.get("btn")
            app = self.cfg.app_url
            if isinstance(btn, dict) and app.startswith("http"):   # кнопка только на наше приложение
                label = str(btn.get("t", ""))[:40].strip()
                if label:
                    p["reply_markup"] = kb([[(label, "url:" + app)]])
            self.tg.call("sendMessage", **p)

    def cleanup(self) -> None:
        sess = self.fb.get("sessions")
        if isinstance(sess, dict):
            limit = int((time.time() - 86400) * 1000)
            for k, v in sess.items():
                if not isinstance(v, dict) or int(v.get("ts") or 0) < limit:
                    self.fb.delete(f"sessions/{k}")

    def outbox_loop(self) -> None:
        last_clean = 0.0
        while True:
            try:
                self.drain_outbox()
                if time.time() - last_clean > 600:
                    last_clean = time.time()
                    self.cleanup()
            except Exception:
                log.exception("outbox")
            time.sleep(2)

    # --- главный цикл ---
    def handle(self, u: dict) -> None:
        if "callback_query" in u:
            return self.on_callback(u["callback_query"])
        m = u.get("message")
        if not m or m.get("chat", {}).get("type") != "private" or not m.get("from"):
            return
        text = (m.get("text") or "").strip()
        if text.startswith("/start"):
            self.on_start(m, text.partition(" ")[2].strip())
            return
        uid = m["from"]["id"]
        flow = self.a.flow(uid) if self.a else None
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if cmd == "/support" and self.a:
            if flow:
                self.a.flow_del(uid)
            self.start_support(m["chat"]["id"], m["from"])
        elif cmd == "/cancel":
            if flow:
                self.a.flow_del(uid)
                self.send(m["chat"]["id"], "Заявка отменена.")
            elif self.a and (s := self.a.sup(uid)) and s["state"] == "open":
                self.a.sup_close(uid)
                self.send(m["chat"]["id"], "Диалог со службой поддержки завершён. Чтобы написать снова — /support.")
        elif cmd:
            self.send(m["chat"]["id"], "Откройте приложение ZINDAGI — код входа придёт сюда.\n"
                                       "Связаться со службой поддержки: /support")
        elif flow:
            self.on_flow_message(m, flow)
        elif self.a and uid in self.cfg.admins:
            rt = m.get("reply_to_message")
            target = self.a.sup_map_get(uid, rt["message_id"]) if rt else None
            if target:
                self.admin_to_user(m, target)
            else:
                self.send(m["chat"]["id"], "Чтобы ответить пользователю, ответьте реплаем на его сообщение.")
        elif self.a:
            self.user_to_support(m)

    # --- недельный отчёт: понедельник, 09:00 (Таджикистан) ---
    CAT_NAMES = {"food": "Еда", "transport": "Транспорт", "shopping": "Покупки", "home": "Дом",
                 "communication": "Связь", "technology": "Техника", "education": "Учёба",
                 "health": "Здоровье", "entertainment": "Досуг", "other": "Другое"}

    def read_section(self, uid: str, key: str) -> Any:
        """Раздел приложения из облака: users/<uid>/d/<key>/c/<n> — JSON, разрезанный на куски."""
        node = self.fb.get(f"users/{uid}/d/{key}")
        if not isinstance(node, dict):
            return None
        c = node.get("c")
        n = int(node.get("n") or 0)
        if isinstance(c, list):
            parts = c
        elif isinstance(c, dict):
            parts = [c.get(str(i)) for i in range(n)]
        else:
            return None
        if not parts or any(not isinstance(x, str) for x in parts):
            return None
        return json.loads("".join(parts))

    def week_report_text(self, uid: str) -> Optional[str]:
        if self.read_section(uid, "notif") is False:
            return None                              # человек отключил уведомления
        exp = self.read_section(uid, "expenses") or []
        inc = self.read_section(uid, "incomes") or []
        now = int(time.time() * 1000)
        w0, w1 = now - 7 * DAY_MS, now - 14 * DAY_MS
        cur = [e for e in exp if isinstance(e, dict) and int(e.get("ts") or 0) >= w0]
        prev = [e for e in exp if isinstance(e, dict) and w1 <= int(e.get("ts") or 0) < w0]
        if not cur and not prev:
            return None
        amt = lambda L: sum(float(e.get("amount") or 0) for e in L)
        tot, ptot = amt(cur), amt(prev)
        earned = amt([e for e in inc if isinstance(e, dict) and int(e.get("ts") or 0) >= w0])
        money = lambda v: f"{int(round(v)):,}".replace(",", " ") + " с."
        lines = ["📊 Итоги недели", f"Потрачено: {money(tot)}"]
        if ptot > 0:
            d = round((tot - ptot) / ptot * 100)
            lines.append(("📈 на " if d > 0 else "📉 на ") + f"{abs(d)}% {'больше' if d > 0 else 'меньше'}, чем на прошлой неделе ({money(ptot)})"
                         if d else f"Столько же, сколько на прошлой неделе ({money(ptot)})")
        if earned:
            lines.append(f"Доходы: +{money(earned)}")
        by: dict[str, float] = {}
        for e in cur:
            k = str(e.get("category") or "other")
            by[k] = by.get(k, 0) + float(e.get("amount") or 0)
        top = sorted(by.items(), key=lambda x: -x[1])[:3]
        if top:
            lines.append("\nБольше всего ушло на:")
            lines += [f"{i}. {self.CAT_NAMES.get(k, 'Другое')} — {money(v)}" for i, (k, v) in enumerate(top, 1)]
        # бюджеты по категориям: предупредим, если за месяц уже 80%+
        budgets = self.read_section(uid, "budgets") or {}
        if isinstance(budgets, dict) and budgets:
            ms = datetime.now(TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            m0 = int(ms.timestamp() * 1000)
            spent: dict[str, float] = {}
            for e in exp:
                if isinstance(e, dict) and int(e.get("ts") or 0) >= m0:
                    k = str(e.get("category") or "other")
                    spent[k] = spent.get(k, 0) + float(e.get("amount") or 0)
            warn = [f"⚠️ {self.CAT_NAMES.get(k, k)}: {round(spent.get(k, 0) / lim * 100)}% бюджета"
                    for k, lim in budgets.items() if isinstance(lim, (int, float)) and lim > 0 and spent.get(k, 0) >= lim * 0.8]
            if warn:
                lines.append("\nБюджеты месяца:")
                lines += warn
        return "\n".join(lines)

    def weekly_reports(self) -> None:
        now = datetime.now(TZ)
        if now.weekday() != 0 or now.hour < 9:
            return
        week = now.strftime("%G-W%V")
        for uid in self.fb.keys("tgusers"):
            if not uid.isdigit() or self.fb.get(f"reports/{uid}/last") == week:
                continue
            try:
                if self.fb.get(f"blocked/uid/{uid}") is not None:
                    continue
                text = self.week_report_text(uid)
                self.fb.set(f"reports/{uid}/last", week)   # отметка ставится всегда: раз в неделю
                if text:
                    self.send(int(uid), text)
                    time.sleep(0.1)
            except Exception:
                log.exception("недельный отчёт %s", uid)

    def report_loop(self) -> None:
        while True:
            try:
                self.weekly_reports()
            except Exception:
                log.exception("report_loop")
            time.sleep(600)

    # --- AI через OpenRouter: приложение кладёт запрос в aireq/<uid>/<id>, ответ — в airesp/<uid>/<id> ---
    AI_SYSTEM = ("Ты — ZINDAGI AI, личный финансовый помощник в приложении. Валюта по умолчанию — сомони (с.). "
                 "Всегда отвечай на том языке, на котором написан или сказан последний вопрос пользователя (русский, таджикский, узбекский, английский и т. д.), дружелюбно и коротко: не больше 5 предложений, без списков, markdown и эмодзи — ответ будет озвучен. "
                 "Опирайся только на данные из блока «Данные пользователя», ничего не выдумывай: если данных не хватает — скажи об этом. "
                 "Давай конкретные расчёты и практичные советы. Ты не юрист и не финансовый консультант; не обещай доходность.")
    RECEIPT_PROMPT = ("На фото кассовый чек. Верни ТОЛЬКО JSON без пояснений: "
                      '{"amount": итоговая сумма числом, "currency": "TJS|RUB|USD|UZS", '
                      '"category": одно из food|transport|shopping|home|communication|technology|education|health|entertainment|other, '
                      '"note": краткое описание покупки до 40 символов}. Если это не чек или сумму не видно — {"amount": 0}.')

    TTS_VOICES = ("nova", "shimmer", "alloy", "onyx", "echo", "fable", "coral", "sage", "ash")

    def _or_headers(self) -> dict:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise RuntimeError("no_key")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "HTTP-Referer": self.cfg.app_url or "https://zindagi.app", "X-Title": "ZINDAGI"}

    def llm(self, messages: list, model: str, max_tokens: int = 500, temperature: float = 0.4) -> str:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise RuntimeError("no_key")
        r = requests.post("https://openrouter.ai/api/v1/chat/completions", timeout=60,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                   "HTTP-Referer": self.cfg.app_url or "https://zindagi.app", "X-Title": "ZINDAGI"},
                          json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature})
        r.raise_for_status()
        return str(r.json()["choices"][0]["message"]["content"] or "").strip()

    def ai_handle(self, uid: str, rid: str, req: dict) -> None:
        out: dict[str, Any] = {"ok": False, "err": "error"}
        try:
            now = time.time()
            prem = self.fb.get(f"premium/{uid}")
            if not (isinstance(prem, dict) and int(prem.get("until") or 0) > now * 1000):
                out = {"ok": False, "err": "Нужна активная подписка."}
            elif self.fb.get(f"blocked/uid/{uid}") is not None:
                out = {"ok": False, "err": "Доступ закрыт."}
            else:
                kind0 = req.get("kind")
                bucket = uid if kind0 in ("chat", "receipt") else f"{uid}:{kind0}"
                limit = {"stt": 80, "tts": 200}.get(kind0, 40)
                hist = [t for t in self._ai_hist.get(bucket, []) if now - t < 3600]
                if len(hist) >= limit:
                    out = {"ok": False, "err": "Слишком много запросов, попробуйте через час."}
                else:
                    hist.append(now)
                    self._ai_hist[bucket] = hist
                    model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
                    kind = req.get("kind")
                    if kind == "chat":
                        msgs = [{"role": "system", "content": self.AI_SYSTEM}]
                        for m in (req.get("hist") or [])[-8:]:
                            if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
                                msgs.append({"role": m["role"], "content": str(m.get("content", ""))[:400]})
                        msgs.append({"role": "user", "content":
                                     f"Данные пользователя: {str(req.get('ctx', ''))[:1800]}\n\nВопрос: {str(req.get('q', ''))[:500]}"})
                        out = {"ok": True, "text": self.llm(msgs, model)[:1500]}
                    elif kind == "stt":
                        data = str(req.get("audio", ""))
                        if len(data) < 200:
                            raise ValueError("bad_audio")
                        r = requests.post("https://openrouter.ai/api/v1/audio/transcriptions", timeout=60,
                                          headers=self._or_headers(),
                                          json={"model": os.environ.get("OPENROUTER_STT_MODEL", "openai/whisper-large-v3"),
                                                "input_audio": {"data": data, "format": "wav"}})
                        r.raise_for_status()
                        text = str((r.json() or {}).get("text") or "").strip()
                        out = ({"ok": True, "text": text[:500]} if text
                               else {"ok": False, "err": "Не расслышал, повторите, пожалуйста."})
                    elif kind == "tts":
                        txt = str(req.get("text", "")).strip()[:600]
                        if not txt:
                            raise ValueError("empty")
                        voice = req.get("voice") if req.get("voice") in self.TTS_VOICES else "nova"
                        r = requests.post("https://openrouter.ai/api/v1/audio/speech", timeout=60,
                                          headers=self._or_headers(),
                                          json={"model": os.environ.get("OPENROUTER_TTS_MODEL", "openai/gpt-4o-mini-tts-2025-12-15"),
                                                "input": txt, "voice": voice, "response_format": "mp3",
                                                "instructions": "Speak naturally and warmly, like a friendly assistant. "
                                                                "Speak in the same language as the text, with a natural accent."})
                        r.raise_for_status()
                        out = {"ok": True, "audio": base64.b64encode(r.content).decode()}
                    elif kind == "receipt":
                        img = str(req.get("img", ""))
                        if not img.startswith("data:image/"):
                            raise ValueError("bad_image")
                        raw = self.llm([{"role": "user", "content": [
                            {"type": "text", "text": self.RECEIPT_PROMPT},
                            {"type": "image_url", "image_url": {"url": img}}]}],
                            os.environ.get("OPENROUTER_VISION_MODEL", model), max_tokens=200, temperature=0)
                        m = re.search(r"\{.*\}", raw, re.S)
                        d = json.loads(m.group(0)) if m else {}
                        amount = float(d.get("amount") or 0)
                        cat = d.get("category") if d.get("category") in self.CAT_NAMES else "other"
                        cur = d.get("currency") if d.get("currency") in ("TJS", "RUB", "USD", "UZS") else "TJS"
                        out = ({"ok": True, "amount": round(amount, 2), "category": cat, "currency": cur,
                                "note": str(d.get("note") or "")[:40]} if amount > 0
                               else {"ok": False, "err": "Не удалось прочитать чек. Сфотографируйте ближе и при хорошем свете."})
        except RuntimeError as e:
            out = {"ok": False, "err": "AI ещё не настроен администратором." if str(e) == "no_key" else "Ошибка AI."}
        except Exception:
            log.exception("ai_handle")
            out = {"ok": False, "err": "AI сейчас недоступен. Попробуйте позже."}
        try:
            self.fb.set(f"airesp/{uid}/{rid}", out)
        except Exception:
            log.exception("airesp")

    def ai_loop(self) -> None:
        while True:
            try:
                for uid in self.fb.keys("aireq"):
                    reqs = self.fb.get(f"aireq/{uid}")
                    if isinstance(reqs, dict):
                        for rid, r in reqs.items():
                            self.fb.delete(f"aireq/{uid}/{rid}")
                            if uid.isdigit() and isinstance(r, dict) and int(r.get("ts") or 0) > (time.time() - 120) * 1000:
                                threading.Thread(target=self.ai_handle, args=(uid, rid, r), daemon=True).start()
                    else:
                        self.fb.delete(f"aireq/{uid}")
            except Exception:
                log.exception("ai_loop")
            time.sleep(0.8)

    def run(self) -> None:
        me = self.tg.call("getMe")
        self.tg.call("deleteWebhook", drop_pending_updates=False)
        log.info("Бот входа запущен: @%s", (me or {}).get("username", "?"))
        threading.Thread(target=self.outbox_loop, daemon=True).start()
        threading.Thread(target=self.report_loop, daemon=True).start()
        self._ai_hist: dict[str, list[float]] = {}
        threading.Thread(target=self.ai_loop, daemon=True).start()
        if self.a:
            threading.Thread(target=self.support_loop, daemon=True).start()
            self.tg.call("setMyCommands", commands=[
                {"command": "support", "description": "Связаться со службой поддержки"},
                {"command": "cancel", "description": "Отменить заявку или закрыть диалог"}])
        offset: Optional[int] = None
        while True:
            try:
                for u in self.tg.get_updates(offset, 25):
                    offset = u["update_id"] + 1
                    try:
                        self.handle(u)
                    except Exception:
                        log.exception("Ошибка входа в обновлении %s", u.get("update_id"))
            except Exception as e:
                log.error("Цикл бота входа: %s", e)
                time.sleep(3)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    cfg = Cfg()
    cfg.check()
    if not cfg.cred_json and not os.path.exists(cfg.cred_path):
        raise SystemExit(f"Нет ключа Firebase: задайте FIREBASE_CREDENTIALS_JSON или положите файл {cfg.cred_path} (см. README).")
    fb = Firebase(cfg.db_url, cfg.cred_path, cfg.cred_json)
    store = Store(cfg.sqlite)
    bot = PremiumBot(cfg, Telegram(cfg.token), store, fb)
    if cfg.login_token:
        if cfg.login_token == cfg.token:
            raise SystemExit("LOGIN_BOT_TOKEN должен отличаться от BOT_TOKEN: это два разных бота.")
        login = LoginBot(cfg, Telegram(cfg.login_token), fb, store, AccessStore(cfg.access_sqlite))
        threading.Thread(target=login.run, daemon=True).start()
    else:
        log.warning("LOGIN_BOT_TOKEN не задан — бот входа не запущен, вход в приложение работать не будет.")
    try:
        bot.run()
    except KeyboardInterrupt:
        print("Остановлено.", file=sys.stderr)


if __name__ == "__main__":
    main()
