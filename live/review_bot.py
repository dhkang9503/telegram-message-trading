from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from telethon import Button, TelegramClient, events

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Environment variable '{name}' is required.")
    return value


def optional_int_env(name: str) -> Optional[int]:
    value = (os.getenv(name) or "").strip()
    return int(value) if value else None


def resolve_path(env_name: str, default_relative: str) -> Path:
    raw = (os.getenv(env_name) or default_relative).strip()
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


API_ID = int(require_env("TELEGRAM_API_ID"))
API_HASH = require_env("TELEGRAM_API_HASH")
BOT_TOKEN = require_env("TELEGRAM_REVIEW_BOT_TOKEN")
REVIEW_CHAT_ID = optional_int_env("TELEGRAM_REVIEW_CHAT_ID")
ALLOWED_USER_ID = optional_int_env("TELEGRAM_REVIEW_ALLOWED_USER_ID")
SESSION_NAME = (os.getenv("TELEGRAM_REVIEW_SESSION_NAME") or "telegram_review_bot").strip()

LIVE_EVENTS_PATH = resolve_path("LIVE_EVENTS_PATH", "logs/live_events.jsonl")
MISTAKES_PATH = resolve_path("MISTAKES_PATH", "data/mistakes.jsonl")
DB_PATH = resolve_path("REVIEW_DB_PATH", "state/review_bot.sqlite3")
MODEL_VERSION = (os.getenv("MODEL_VERSION") or "unknown").strip()
START_MODE = (os.getenv("REVIEW_START_MODE") or "end").strip().lower()
POLL_INTERVAL_SECONDS = float(os.getenv("REVIEW_POLL_INTERVAL_SECONDS", "0.5"))
SEND_RETRY_SECONDS = float(os.getenv("REVIEW_SEND_RETRY_SECONDS", "5"))
MAX_TEXT_CHARS = int(os.getenv("REVIEW_MAX_TEXT_CHARS", "3200"))

if START_MODE not in {"end", "beginning"}:
    raise RuntimeError("REVIEW_START_MODE must be 'end' or 'beginning'")

for path in (LIVE_EVENTS_PATH, MISTAKES_PATH, DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
LIVE_EVENTS_PATH.touch(exist_ok=True)


def stable_event_id(record: dict[str, Any]) -> str:
    seed = json.dumps(
        {
            "telegram_message_id": record.get("telegram_message_id"),
            "received_at": record.get("received_at"),
            "message": record.get("message"),
            "raw_output": record.get("raw_output"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def extract_actions_from_raw(raw_output: Any) -> Optional[list[dict[str, Any]]]:
    if not isinstance(raw_output, str) or not raw_output.strip():
        return None
    cleaned = raw_output.replace("[end of text]", "").strip()
    match = re.search(r'\{\s*"actions"\s*:\s*\[.*?\]\s*\}', cleaned, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    actions = obj.get("actions")
    return actions if isinstance(actions, list) else None


def model_actions_for(record: dict[str, Any]) -> list[dict[str, Any]]:
    # live_bot.py may overwrite record["actions"] after applying a fallback.
    parsed = extract_actions_from_raw(record.get("raw_output"))
    if parsed is not None:
        return parsed
    actions = record.get("actions")
    return actions if isinstance(actions, list) else []


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)] + "\n…(truncated)"


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def format_review_message(record: dict[str, Any], *, saved: bool = False) -> str:
    message = str(record.get("message") or "")
    model_actions = model_actions_for(record)
    effective_actions = record.get("actions")
    fallback = record.get("fallback")
    error = record.get("error")

    parts = [
        "<b>📌 방장 메시지</b>",
        f"<pre>{html.escape(truncate(message, 900))}</pre>",
        "<b>🤖 모델 추론</b>",
        f"<pre>{html.escape(truncate(pretty_json({'actions': model_actions}), 800))}</pre>",
    ]

    if fallback or (isinstance(effective_actions, list) and effective_actions != model_actions):
        parts.extend(
            [
                "<b>⚙️ 실제 적용 액션</b>",
                f"<pre>{html.escape(truncate(pretty_json({'actions': effective_actions or []}), 600))}</pre>",
            ]
        )
    if fallback:
        parts.append(f"<b>Fallback:</b> <code>{html.escape(str(fallback))}</code>")
    if error:
        parts.append(f"<b>오류:</b> <code>{html.escape(truncate(str(error), 250))}</code>")

    parts.append(
        f"ID: <code>{html.escape(str(record.get('telegram_message_id')))}</code>"
        f" · {html.escape(str(record.get('received_at') or ''))}"
        f" · model: <code>{html.escape(MODEL_VERSION)}</code>"
    )
    if saved:
        parts.append("<b>✅ 오추론 후보로 저장됨</b>")
    return "\n\n".join(parts)


def build_mistake_payload(event_id: str, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mistake_id": event_id,
        "collected_at": now_iso(),
        "model_version": MODEL_VERSION,
        "source": {
            "telegram_message_id": record.get("telegram_message_id"),
            "message_date": record.get("message_date"),
            "received_at": record.get("received_at"),
            "post_author": record.get("post_author"),
            "message": record.get("message"),
        },
        "model": {
            "raw_output": record.get("raw_output"),
            "actions": model_actions_for(record),
            "inference_seconds": record.get("inference_seconds"),
        },
        "effective_actions": record.get("actions"),
        "fallback": record.get("fallback"),
        "executions": record.get("executions"),
        "live_event_ok": record.get("ok"),
        "live_event_error": record.get("error"),
        "label": {"status": "pending", "correct_actions": None},
    }


def append_jsonl_locked(path: Path, data: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class ReviewDB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS review_events (
                event_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                notification_chat_id INTEGER,
                notification_message_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS mistakes (
                mistake_id TEXT PRIMARY KEY,
                event_id TEXT UNIQUE NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                written INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def add_event(self, event_id: str, record: dict[str, Any]) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO review_events(event_id, payload_json, created_at) VALUES(?, ?, ?)",
            (event_id, json.dumps(record, ensure_ascii=False), now_iso()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def unsent_events(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT event_id, payload_json FROM review_events "
                "WHERE notification_message_id IS NULL ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        )

    def mark_sent(self, event_id: str, chat_id: int, message_id: int) -> None:
        self.conn.execute(
            "UPDATE review_events SET notification_chat_id = ?, notification_message_id = ? WHERE event_id = ?",
            (chat_id, message_id, event_id),
        )
        self.conn.commit()

    def get_event(self, event_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT payload_json FROM review_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def enqueue_mistake(self, event_id: str, payload: dict[str, Any]) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO mistakes(mistake_id, event_id, payload_json, created_at, written) "
            "VALUES(?, ?, ?, ?, 0)",
            (event_id, event_id, json.dumps(payload, ensure_ascii=False), now_iso()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def unwritten_mistakes(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT mistake_id, payload_json FROM mistakes WHERE written = 0 "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        )

    def mark_mistake_written(self, mistake_id: str) -> None:
        self.conn.execute("UPDATE mistakes SET written = 1 WHERE mistake_id = ?", (mistake_id,))
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        def count(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])
        return {
            "events": count("SELECT COUNT(*) FROM review_events"),
            "pending_send": count("SELECT COUNT(*) FROM review_events WHERE notification_message_id IS NULL"),
            "mistakes": count("SELECT COUNT(*) FROM mistakes"),
            "unwritten_mistakes": count("SELECT COUNT(*) FROM mistakes WHERE written = 0"),
        }


class EventTailer:
    def __init__(self, path: Path, db: ReviewDB):
        self.path = path
        self.db = db

    def initialize_cursor(self) -> tuple[int, int]:
        stat = self.path.stat()
        stored_inode = self.db.get_meta("cursor_inode")
        stored_offset = self.db.get_meta("cursor_offset")
        if stored_inode is None or stored_offset is None:
            offset = 0 if START_MODE == "beginning" else stat.st_size
            self.db.set_meta("cursor_inode", str(stat.st_ino))
            self.db.set_meta("cursor_offset", str(offset))
            print(json.dumps({"event": "CURSOR_INITIALIZED", "mode": START_MODE, "offset": offset}, ensure_ascii=False))
            return stat.st_ino, offset
        return int(stored_inode), int(stored_offset)

    async def run(self) -> None:
        inode, offset = self.initialize_cursor()
        while True:
            try:
                stat = self.path.stat()
                if stat.st_ino != inode:
                    inode, offset = stat.st_ino, 0
                    self.db.set_meta("cursor_inode", str(inode))
                    self.db.set_meta("cursor_offset", "0")
                    print(json.dumps({"event": "EVENT_LOG_ROTATED"}, ensure_ascii=False))
                elif stat.st_size < offset:
                    offset = 0
                    self.db.set_meta("cursor_offset", "0")
                    print(json.dumps({"event": "EVENT_LOG_TRUNCATED"}, ensure_ascii=False))

                new_offset = offset
                with self.path.open("rb") as f:
                    f.seek(offset)
                    while True:
                        line_start = f.tell()
                        line = f.readline()
                        if not line:
                            break
                        if not line.endswith(b"\n"):
                            f.seek(line_start)
                            break
                        new_offset = f.tell()
                        text = line.decode("utf-8", errors="replace").strip()
                        if not text:
                            continue
                        try:
                            record = json.loads(text)
                        except json.JSONDecodeError as exc:
                            print(json.dumps({"event": "INVALID_EVENT_JSON_SKIPPED", "offset": line_start, "error": str(exc)}, ensure_ascii=False))
                            continue
                        if not isinstance(record, dict) or not record.get("message"):
                            continue
                        if record.get("skipped") == "duplicate_message_id":
                            continue
                        self.db.add_event(stable_event_id(record), record)

                if new_offset != offset:
                    offset = new_offset
                    self.db.set_meta("cursor_offset", str(offset))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(json.dumps({"event": "TAILER_ERROR", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


def authorized(chat_id: Optional[int], sender_id: Optional[int]) -> bool:
    if REVIEW_CHAT_ID is not None and chat_id != REVIEW_CHAT_ID:
        return False
    if ALLOWED_USER_ID is not None and sender_id != ALLOWED_USER_ID:
        return False
    return True


async def flush_unwritten_mistakes(db: ReviewDB) -> None:
    for row in db.unwritten_mistakes():
        append_jsonl_locked(MISTAKES_PATH, json.loads(row["payload_json"]))
        db.mark_mistake_written(str(row["mistake_id"]))


async def mistake_writer_loop(db: ReviewDB) -> None:
    while True:
        try:
            await flush_unwritten_mistakes(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(json.dumps({"event": "MISTAKE_WRITE_ERROR", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        await asyncio.sleep(1.0)


async def sender_loop(client: TelegramClient, db: ReviewDB) -> None:
    while True:
        try:
            if REVIEW_CHAT_ID is None:
                await asyncio.sleep(SEND_RETRY_SECONDS)
                continue
            rows = db.unsent_events()
            if not rows:
                await asyncio.sleep(0.5)
                continue
            for row in rows:
                event_id = str(row["event_id"])
                record = json.loads(row["payload_json"])
                sent = await client.send_message(
                    REVIEW_CHAT_ID,
                    format_review_message(record),
                    buttons=[[Button.inline("❌ 오추론 저장", data=f"mistake:{event_id}".encode())]],
                    parse_mode="html",
                    link_preview=False,
                )
                db.mark_sent(event_id, int(REVIEW_CHAT_ID), int(sent.id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(json.dumps({"event": "REVIEW_SEND_ERROR", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
            await asyncio.sleep(SEND_RETRY_SECONDS)


async def run_bot() -> None:
    db = ReviewDB(DB_PATH)
    client = TelegramClient(str(BASE_DIR / SESSION_NAME), API_ID, API_HASH)

    @client.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
    async def on_start(event: Any) -> None:
        await event.respond(
            "Review bot is alive.\n"
            f"chat_id={getattr(event, 'chat_id', None)}\n"
            f"sender_id={getattr(event, 'sender_id', None)}\n\n"
            "이 값을 .env의 TELEGRAM_REVIEW_CHAT_ID / TELEGRAM_REVIEW_ALLOWED_USER_ID에 넣고 재시작하면 됨."
        )

    @client.on(events.NewMessage(pattern=r"^/status(?:@\w+)?$"))
    async def on_status(event: Any) -> None:
        if not authorized(getattr(event, "chat_id", None), getattr(event, "sender_id", None)):
            return
        counts = db.counts()
        await event.respond(
            "Review bot status\n"
            f"- events: {counts['events']}\n"
            f"- pending send: {counts['pending_send']}\n"
            f"- mistakes: {counts['mistakes']}\n"
            f"- unwritten mistakes: {counts['unwritten_mistakes']}\n"
            f"- event log: {LIVE_EVENTS_PATH}\n"
            f"- mistakes: {MISTAKES_PATH}"
        )

    @client.on(events.CallbackQuery(pattern=rb"^mistake:[0-9a-f]{20}$"))
    async def on_mistake(event: Any) -> None:
        if not authorized(getattr(event, "chat_id", None), getattr(event, "sender_id", None)):
            await event.answer("권한 없음", alert=True)
            return
        event_id = event.data.decode().split(":", 1)[1]
        record = db.get_event(event_id)
        if record is None:
            await event.answer("원본 이벤트를 찾지 못함", alert=True)
            return
        inserted = db.enqueue_mistake(event_id, build_mistake_payload(event_id, record))
        await flush_unwritten_mistakes(db)
        await event.answer("오추론 후보로 저장함" if inserted else "이미 저장된 항목임")
        try:
            await event.edit(format_review_message(record, saved=True), buttons=None, parse_mode="html", link_preview=False)
        except Exception:
            pass

    tasks: list[asyncio.Task[Any]] = []
    try:
        await client.start(bot_token=BOT_TOKEN)
        me = await client.get_me()
        print(
            json.dumps(
                {
                    "event": "REVIEW_BOT_STARTED",
                    "bot": getattr(me, "username", None),
                    "review_chat_id": REVIEW_CHAT_ID,
                    "allowed_user_id": ALLOWED_USER_ID,
                    "event_log": str(LIVE_EVENTS_PATH),
                    "mistakes": str(MISTAKES_PATH),
                    "database": str(DB_PATH),
                    "start_mode": START_MODE,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        tailer = EventTailer(LIVE_EVENTS_PATH, db)
        tasks = [
            asyncio.create_task(tailer.run(), name="event-tailer"),
            asyncio.create_task(sender_loop(client, db), name="review-sender"),
            asyncio.create_task(mistake_writer_loop(db), name="mistake-writer"),
        ]
        await client.run_until_disconnected()
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await client.disconnect()
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "review_chat_id": REVIEW_CHAT_ID,
                    "allowed_user_id": ALLOWED_USER_ID,
                    "event_log": str(LIVE_EVENTS_PATH),
                    "mistakes": str(MISTAKES_PATH),
                    "database": str(DB_PATH),
                    "start_mode": START_MODE,
                    "model_version": MODEL_VERSION,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
