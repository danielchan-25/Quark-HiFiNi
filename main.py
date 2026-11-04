from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import websocket
from dotenv import load_dotenv

from ai_review import ReviewConfig, ShadowReviewer
import notifications


ROOT = Path(__file__).resolve().parent
USER_AGENT = "QuarkHiFiNi/1.0"
DAILY_LIMIT = 1
MAX_FAILURES = 3
PREVIEW_URL = "https://example.invalid/dry-run-no-share"
SCHEMA_VERSION = 3
IS_WINDOWS = os.name == "nt"
BEIJING = timezone(timedelta(hours=8))


def beijing_day_bounds() -> tuple[str, str]:
    """SQLite timestamps are UTC; the publishing quota always follows Beijing."""
    midnight = datetime.now(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    return tuple(moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                 for moment in (midnight, midnight + timedelta(days=1)))


def curl_binary() -> str:
    return os.getenv("CURL_BIN") or ("curl.exe" if IS_WINDOWS else "curl")


class RunLock:
    """OS-owned lock: a crash releases ownership without deleting stale files."""
    def __enter__(self) -> "RunLock":
        path = ROOT / "data" / "run.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0"); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise FlowError("已有任务正在运行，请等待其结束") from exc
        return self

    def __exit__(self, *args: Any) -> None:
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


def execute(command: list[str], *, timeout: int, operation: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Bound subprocess lifetime, including children, without leaking command secrets."""
    input_text = kwargs.pop("input", None)
    process = subprocess.Popen(command, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                               start_new_session=os.name != "nt", **kwargs)
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                           timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
        process.kill()
        process.communicate(timeout=5)
        if sys.exc_info()[0] is KeyboardInterrupt:
            raise
        raise FlowError(f"{operation}超过 {timeout} 秒，已终止子进程") from None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if re.search(r"password|secret|cookie|(?:^|_)token$|webhook|api_key|authorization", key, re.I)
                      else redact(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for key in ("HIFINI_PASSWORD", "FEISHU_SECRET", "FEISHU_WEBHOOK", "TYPESAFE_API_KEY", "AI_API_KEY"):
            secret = os.getenv(key)
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"https://[^\s\"']*/open-apis/bot/v2/hook/[^\s\"']+", "[WEBHOOK]", value)
        value = re.sub(r"(?i)(password|secret|access_token|bbs_token|api_key)([=:]\s*)[^\s&,;]+", r"\1\2[REDACTED]", value)
    return value


class FlowError(RuntimeError):
    """An expected operational failure that should be logged and alerted."""


class DuplicateTitleError(FlowError):
    """HiFiNi rejected the post because its title is already occupied."""


class BitrateProbeError(FlowError):
    """An MP3 bitrate could not be verified reliably."""


class NoLyricsError(FlowError):
    """A verified Baidu Baike song entry has no usable lyrics."""


class BaikeLookupError(FlowError):
    """Baidu Baike could not be searched or verified reliably this run."""


class LyricsLookupError(FlowError):
    """The selected lyrics provider could not return a verifiable match."""


class SubmissionRejectedError(FlowError):
    """The forum explicitly rejected the request; it did not create a post."""


@dataclass(frozen=True)
class Settings:
    cdp_url: str
    quark_dir: Path
    git_bash: Path
    feishu_webhook: str | None
    feishu_secret: str | None
    hifini_username: str | None
    hifini_password: str | None
    typesafe: ReviewConfig = field(default_factory=ReviewConfig)

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        try:
            review_config = ReviewConfig.load()
        except ValueError as exc:
            raise FlowError('AI 配置无效：' + str(exc)) from exc
        return cls(
            cdp_url=os.getenv("HIFINI_CDP_URL", "http://127.0.0.1:9501").rstrip("/"),
            quark_dir=Path(os.getenv("QUARK_SKILL_DIR", str(Path.home() / ".codex" / "skills" / "quarkclouddrive"))).expanduser(),
            git_bash=Path(os.getenv("BASH_PATH") or os.getenv("GIT_BASH") or shutil.which("bash") or "/bin/bash").expanduser(),
            feishu_webhook=os.getenv("FEISHU_WEBHOOK") or None,
            feishu_secret=os.getenv("FEISHU_SECRET") or None,
            hifini_username=os.getenv("HIFINI_USERNAME") or None,
            hifini_password=os.getenv("HIFINI_PASSWORD") or None,
            typesafe=review_config,
        )


class JsonLogger:
    """All structured logs are durable in SQLite; console reports progress."""
    def __init__(self, run_id: str, store: "Store") -> None:
        self.run_id, self.store = run_id, store

    def event(self, level: str, name: str, **data: Any) -> None:
        row = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "run_id": self.run_id,
               "level": level, "event": name, **data}
        row = redact(row)
        self.store.event(self.run_id, level, name, json.dumps(row, ensure_ascii=False))
        print(json.dumps(row, ensure_ascii=True), flush=True)


class Store:
    def __init__(self, path: Path | str | None = None) -> None:
        path = path or ROOT / "data" / "quark-hifini.db"
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self.db.close()
            raise FlowError("数据库版本高于当前程序，禁止降级打开")
        if str(path) != ":memory:" and version < SCHEMA_VERSION:
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='resources'").fetchone():
                backup = sqlite3.connect(str(path) + f".before-v{SCHEMA_VERSION}-{uuid.uuid4().hex[:8]}.db")
                try: self.db.backup(backup)
                finally: backup.close()
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS resources (
          id INTEGER PRIMARY KEY, folder_fid TEXT NOT NULL UNIQUE, folder_name TEXT NOT NULL,
          artist TEXT, song TEXT, category TEXT, share_url TEXT,
          status TEXT NOT NULL DEFAULT 'DISCOVERED', failure_count INTEGER NOT NULL DEFAULT 0,
          hifini_url TEXT, detail TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, mode TEXT NOT NULL, status TEXT NOT NULL, detail TEXT,
          started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, ended_at TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, resource_id INTEGER REFERENCES resources(id),
          level TEXT NOT NULL, event_type TEXT NOT NULL, detail TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_resources_status ON resources(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
        CREATE TABLE IF NOT EXISTS drafts (
          id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, folder_name TEXT NOT NULL,
          title TEXT NOT NULL, category TEXT NOT NULL, lyrics_html TEXT NOT NULL,
          share_url TEXT NOT NULL, share_passcode TEXT, files_json TEXT NOT NULL,
          bitrates_json TEXT NOT NULL, preview_only INTEGER NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS submission_attempts (
          id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, resource_id INTEGER NOT NULL,
          title TEXT NOT NULL, status TEXT NOT NULL, detail TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, ended_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_attempts_status ON submission_attempts(status, created_at);
        CREATE TABLE IF NOT EXISTS alerts (
          id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL,
          fingerprint TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_fingerprint ON alerts(fingerprint, updated_at);
        CREATE TABLE IF NOT EXISTS imported_logs (source_key TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS scans (
          id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, parent_fid TEXT NOT NULL,
          items_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ai_judgments (
          id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, folder_name TEXT NOT NULL, stage TEXT NOT NULL,
          mode TEXT NOT NULL CHECK(mode='shadow'), applied INTEGER NOT NULL DEFAULT 0 CHECK(applied=0),
          prompt_version TEXT NOT NULL, requested_model TEXT NOT NULL, response_model TEXT,
          cache_key TEXT NOT NULL, cached_from INTEGER REFERENCES ai_judgments(id),
          state_json TEXT NOT NULL, questions_json TEXT NOT NULL, local_result_json TEXT NOT NULL,
          response_json TEXT, status TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          elapsed_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ai_cache ON ai_judgments(cache_key,status,created_at);
        CREATE INDEX IF NOT EXISTS idx_ai_run ON ai_judgments(run_id,id);
        CREATE TABLE IF NOT EXISTS uploads (
          id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, local_path TEXT NOT NULL,
          song_key TEXT, sha256 TEXT, size INTEGER, probe_json TEXT,
          status TEXT NOT NULL, parent_fid TEXT, remote_fid TEXT, detail TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_upload_song ON uploads(song_key,status);
        CREATE TABLE IF NOT EXISTS task_slots (
          kind TEXT NOT NULL, slot_date TEXT NOT NULL, run_id TEXT NOT NULL,
          status TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(kind,slot_date)
        );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(resources)")}
        for name, definition in {
            "submitted_at": "TEXT",
            "audit_status": "TEXT",
            "audit_checked_at": "TEXT",
            "audit_alerted_at": "TEXT",
            "share_passcode": "TEXT",
            "failure_stage": "TEXT",
        }.items():
            if name not in columns: self.db.execute(f"ALTER TABLE resources ADD COLUMN {name} {definition}")
        self._deduplicate_test_records()
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def import_logs(self) -> int:
        count = 0
        for path in sorted((ROOT / "logs").glob("*.jsonl")):
            with path.open(encoding="utf-8-sig") as source:
                for number, line in enumerate(source, 1):
                    key = f"{path.name}:{number}:{hashlib.sha256(line.encode()).hexdigest()}"
                    if self.db.execute("SELECT 1 FROM imported_logs WHERE source_key=?", (key,)).fetchone(): continue
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict): raise ValueError("not an object")
                        row = redact(row)
                    except (ValueError, TypeError):
                        row = {"level": "WARNING", "event": "legacy_log_invalid", "source": path.name, "line": number}
                    self.db.execute("INSERT INTO events(run_id,level,event_type,detail,created_at) VALUES(?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP))",
                                    (row.get("run_id", "legacy"), row.get("level", "INFO"), row.get("event", "legacy"),
                                     json.dumps(row, ensure_ascii=False), row.get("time")))
                    self.db.execute("INSERT INTO imported_logs VALUES(?)", (key,)); count += 1
        self.db.commit()
        return count

    def _deduplicate_test_records(self) -> None:
        # Folder names are unique in Share/Media. Keep prior test rows for audit
        # history but prevent them from acting as another live resource.
        names = self.db.execute("SELECT folder_name FROM resources GROUP BY folder_name HAVING COUNT(*) > 1").fetchall()
        priority = {"SUBMISSION_UNKNOWN": 20, "DELETED": 10, "REJECTED": 9, "SKIPPED_TITLE_CONFLICT": 8,
                    "SKIPPED_NO_LYRICS": 7, "SKIPPED_BAIKE_LOOKUP": 6,
                    "SKIPPED_BITRATE_FAILURE": 6, "PUBLISHED": 5, "PENDING_REVIEW": 4,
                    "SUBMITTED": 3, "DRAFT_READY": 2}
        for (name,) in names:
            rows = self.db.execute("SELECT id,status FROM resources WHERE folder_name=? AND status!='SUPERSEDED'", (name,)).fetchall()
            if len(rows) < 2: continue
            keeper = max(rows, key=lambda row: (priority.get(row["status"], 1), -row["id"]))
            for row in rows:
                if row["id"] != keeper["id"]:
                    self.db.execute("UPDATE resources SET status='SUPERSEDED', detail='同名测试记录已合并' WHERE id=?", (row["id"],))

    def start_run(self, run_id: str, mode: str) -> None:
        # Only called while holding RunLock, so no live owner can be marked aborted.
        self.recover_interrupted()
        self.db.execute("INSERT INTO runs(id, mode, status) VALUES(?,?,?)", (run_id, mode, "RUNNING")); self.db.commit()

    def recover_interrupted(self) -> int:
        rows = self.db.execute("SELECT id FROM runs WHERE status='RUNNING'").fetchall()
        self.db.execute("UPDATE runs SET status='ABORTED', detail='上次进程中断，未正常结束', ended_at=CURRENT_TIMESTAMP WHERE status='RUNNING'")
        self.db.execute("UPDATE alerts SET status='UNKNOWN', last_error='进程中断，发送结果未知' WHERE status='SENDING'")
        self.db.execute("UPDATE ai_judgments SET status='INTERRUPTED', error='process interrupted', finished_at=CURRENT_TIMESTAMP WHERE status='RUNNING'")
        self.db.commit()
        for row in rows:
            self.event(row["id"], "WARNING", "run_recovered", "上次进程中断；若存在未知提交则继续保持保护状态")
        return len(rows)

    def finish_run(self, run_id: str, status: str, detail: str = "") -> None:
        self.db.execute("UPDATE runs SET status=?, detail=?, ended_at=CURRENT_TIMESTAMP WHERE id=?", (status, detail, run_id)); self.db.commit()

    def event(self, run_id: str, level: str, event_type: str, detail: str, resource_id: int | None = None) -> None:
        self.db.execute("INSERT INTO events(run_id,resource_id,level,event_type,detail) VALUES(?,?,?,?,?)", (run_id, resource_id, level, event_type, redact(detail))); self.db.commit()

    def upsert_resource(self, item: dict[str, Any]) -> sqlite3.Row:
        # Quark's opaque FID contains a session-scoped capability suffix. The
        # artist/song pair is the stable identity.  Folder names can change
        # when the configured naming convention changes, so preserve the
        # prior publication/deletion state instead of creating a new record.
        current = self.db.execute("SELECT * FROM resources WHERE folder_fid=?", (item["fid"],)).fetchone()
        if not current:
            current = self.db.execute("SELECT * FROM resources WHERE folder_name=? AND status!='SUPERSEDED' ORDER BY id LIMIT 1", (item["filename"],)).fetchone()
        parsed = parse_folder(item["filename"])
        if not current and parsed:
            artist, song = parsed
            candidates = self.db.execute(
                "SELECT * FROM resources WHERE status!='SUPERSEDED' AND artist IS NOT NULL AND song IS NOT NULL ORDER BY id"
            ).fetchall()
            current = next((row for row in candidates
                            if normalize_identity(row["artist"]) == normalize_identity(artist)
                            and normalize_identity(row["song"]) == normalize_identity(song)), None)
        if current:
            self.db.execute("UPDATE resources SET folder_fid=?, folder_name=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (item["fid"], item["filename"], current["id"]))
        else:
            self.db.execute("INSERT INTO resources(folder_fid,folder_name,status) VALUES(?,?, 'DISCOVERED')", (item["fid"], item["filename"]))
        self.db.commit()
        return self.db.execute("SELECT * FROM resources WHERE folder_fid=?", (item["fid"],)).fetchone()

    def update(self, resource_id: int, status: str, **fields: Any) -> None:
        allowed = {row[1] for row in self.db.execute("PRAGMA table_info(resources)")} - {"id", "status", "created_at", "updated_at"}
        if fields.keys() - allowed: raise ValueError("无效资源字段")
        columns = ["status=?", "updated_at=CURRENT_TIMESTAMP"]; values: list[Any] = [status]
        for key, value in fields.items(): columns.append(f"{key}=?"); values.append(value)
        values.append(resource_id)
        self.db.execute(f"UPDATE resources SET {', '.join(columns)} WHERE id=?", values); self.db.commit()

    def fail(self, resource_id: int, detail: str, stage: str = "flow") -> int:
        self.db.execute("UPDATE resources SET failure_count=CASE WHEN failure_stage=? THEN failure_count+1 ELSE 1 END, failure_stage=?, status='FAILED', detail=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (stage, stage, redact(detail), resource_id)); self.db.commit()
        return int(self.db.execute("SELECT failure_count FROM resources WHERE id=?", (resource_id,)).fetchone()[0])

    def submitted_today(self) -> int:
        start, end = beijing_day_bounds()
        return int(self.db.execute("""SELECT COUNT(*) FROM (
            SELECT id FROM resources WHERE submitted_at>=? AND submitted_at<?
            UNION SELECT resource_id FROM submission_attempts WHERE status IN ('UNKNOWN','CONFIRMED')
            AND created_at>=? AND created_at<?)""", (start, end, start, end)).fetchone()[0])

    def begin_submission(self, run_id: str, resource_id: int, title: str) -> int:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.submitted_today() >= DAILY_LIMIT: raise FlowError("达到每日发布上限")
            if self.db.execute("SELECT 1 FROM submission_attempts WHERE status='UNKNOWN'").fetchone():
                raise FlowError("存在提交结果未知的记录，需先核实，禁止自动重发")
            cursor = self.db.execute("INSERT INTO submission_attempts(run_id,resource_id,title,status) VALUES(?,?,?,'UNKNOWN')", (run_id, resource_id, title))
            self.db.execute("UPDATE resources SET status='SUBMISSION_UNKNOWN' WHERE id=?", (resource_id,))
            return int(cursor.lastrowid)

    def reject_submission(self, attempt_id: int) -> None:
        self.db.execute("UPDATE submission_attempts SET status='REJECTED', detail='明确标题冲突', ended_at=CURRENT_TIMESTAMP WHERE id=?", (attempt_id,)); self.db.commit()

    def save_draft(self, run_id: str, folder_name: str, title: str, category: str, lyrics: str,
                   url: str, passcode: str | None, files: list[dict[str, Any]], bitrates: dict[str, int], preview: bool) -> None:
        self.db.execute("""INSERT INTO drafts(run_id,folder_name,title,category,lyrics_html,share_url,
                        share_passcode,files_json,bitrates_json,preview_only) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (run_id, folder_name, title, category, lyrics, url, passcode,
                         json.dumps(files, ensure_ascii=False), json.dumps(bitrates), int(preview)))
        self.db.commit()

    def audit_candidates(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT *, CAST((julianday('now')-julianday(submitted_at))*86400 AS INTEGER) AS pending_seconds FROM resources WHERE status IN ('SUBMITTED','PENDING_REVIEW','PUBLISHED') AND hifini_url IS NOT NULL").fetchall()

    def mark_submitted(self, resource_id: int, url: str) -> None:
        with self.db:
            self.db.execute("UPDATE resources SET status='SUBMITTED', hifini_url=?, submitted_at=COALESCE((SELECT MIN(created_at) FROM submission_attempts WHERE resource_id=? AND status='UNKNOWN'),CURRENT_TIMESTAMP), failure_count=0, failure_stage=NULL, audit_status='UNVERIFIED', updated_at=CURRENT_TIMESTAMP WHERE id=?", (url, resource_id, resource_id))
            self.db.execute("UPDATE submission_attempts SET status='CONFIRMED', ended_at=CURRENT_TIMESTAMP WHERE resource_id=? AND status='UNKNOWN'", (resource_id,))

    def audit(self, resource_id: int, status: str, detail: str) -> None:
        state = {"PENDING_REVIEW": "PENDING_REVIEW", "APPROVED": "PUBLISHED", "REJECTED": "REJECTED", "DELETED": "DELETED"}[status]
        self.db.execute("UPDATE resources SET status=?, audit_status=?, audit_checked_at=CURRENT_TIMESTAMP, detail=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (state, status, detail, resource_id)); self.db.commit()


class Feishu:
    def __init__(self, settings: Settings, log: JsonLogger, enabled: bool = True) -> None:
        self.settings, self.log, self.enabled = settings, log, enabled
        self.last_delivery: dict[str, Any] | None = None

    def retry_pending(self) -> None:
        if not self.enabled or not self.settings.feishu_webhook: return
        rows = self.log.store.db.execute("SELECT * FROM alerts WHERE status IN ('FAILED','NOT_CONFIGURED') AND title NOT IN ('每日总结','歌词查询失败','未找到可用歌词，素材已跳过') ORDER BY id LIMIT 100").fetchall()
        retried = 0
        for row in rows:
            if not notifications.retryable(row): continue
            if retried >= 10: break
            # Claim the old record before starting another request; retain its evidence.
            self.log.store.db.execute("UPDATE alerts SET status='RETRIED', updated_at=CURRENT_TIMESTAMP WHERE id=?", (row['id'],))
            self.log.store.db.commit()
            self.log.event('INFO', 'notification_retry_started', original_alert_id=row['id'])
            self.alert(row['title'], row['detail'])
            retried += 1

    def alert(self, title: str, detail: str, *, raw_text: bool = False, max_attempts: int = 3) -> bool:
        db = self.log.store.db
        fingerprint = hashlib.sha256(f"{title}\n{detail}".encode()).hexdigest()
        status = "SENDING" if self.enabled else "SUPPRESSED"
        if not self.settings.feishu_webhook:
            status = "NOT_CONFIGURED" if self.enabled else "SUPPRESSED"
        if status == "SENDING" and db.execute(
            "SELECT 1 FROM alerts WHERE fingerprint=? AND status='SENT' AND updated_at>datetime('now','-1 day')", (fingerprint,)
        ).fetchone(): status = "DEDUPLICATED"
        if status == 'SENDING' and db.execute(
            "SELECT 1 FROM alerts WHERE fingerprint=? AND status IN ('SENDING','UNKNOWN') LIMIT 1", (fingerprint,)
        ).fetchone(): status = 'BLOCKED_UNKNOWN'
        cursor = db.execute("INSERT INTO alerts(run_id,title,detail,fingerprint,status) VALUES(?,?,?,?,?)",
                            (self.log.run_id, title, redact(detail), fingerprint, status))
        alert_id = cursor.lastrowid; db.commit()
        if status != "SENDING":
            self.last_delivery = {'delivery_version': 1, 'status': status, 'error': None}
            self.log.event("WARNING" if status == "NOT_CONFIGURED" else "INFO", "feishu_skipped", reason=status, title=title)
            return status == "DEDUPLICATED"
        for attempt in range(1, max_attempts + 1):
            delivery = notifications.send_text(self.settings.feishu_webhook, self.settings.feishu_secret,
                detail if raw_text else f'【Quark-HiFiNi {title}】\n{detail}', execute=execute,
                curl=curl_binary(), sanitize=redact, windows=IS_WINDOWS)
            self.last_delivery = delivery
            state = delivery['status']
            status = 'SENT' if state == 'SENT' else ('UNKNOWN' if state == 'UNKNOWN' else 'FAILED')
            db.execute('UPDATE alerts SET status=?,attempts=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (status, attempt, None if state == 'SENT' else json.dumps(delivery, ensure_ascii=False), alert_id)); db.commit()
            if state == 'SENT':
                self.log.event('INFO', 'feishu_sent', title=title, attempt=attempt)
                return True
            self.log.event('WARNING', 'feishu_delivery_failed', title=title, attempt=attempt, delivery=delivery)
            if state != 'NOT_SENT' or attempt >= max_attempts: break
            time.sleep(attempt)
        self.log.event('ERROR', 'feishu_failed', delivery=self.last_delivery, title=title)
        return False


class Quark:
    def __init__(self, settings: Settings, log: JsonLogger, session_input: str | None = None,
                 session_id: str | None = None) -> None:
        self.s, self.log = settings, log
        self.session_input = session_input
        self.session_id = session_id
        self._environment_checked = False

    def _run(self, arguments: list[str], *, timeout: int = 90) -> list[dict[str, Any]]:
        install = self.s.quark_dir / "scripts" / "install.sh"; cli = self.s.quark_dir / "scripts" / "quark-drive.cjs"
        if not self.s.git_bash.is_file() or not install.is_file() or not cli.is_file(): raise FlowError("夸克 Skill 或 Bash 缺失")
        # A stale localhost proxy prevents the Skill update check (and can
        # make an otherwise healthy Quark login look unavailable).  Keep all
        # other environment settings, but force these local operations direct.
        direct_env = os.environ.copy()
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            direct_env.pop(key, None)
        direct_env["NODE_OPTIONS"] = (direct_env.get("NODE_OPTIONS", "") + " --dns-result-order=ipv4first").strip()
        # Git Bash curl otherwise follows the intermittently unreachable IPv6
        # address returned for Quark's configuration endpoint on this host.
        install_command = ('curl(){ command curl -4 --local-port "$QUARK_CURL_LOCAL_PORT" "$@"; }; export -f curl; "$1"'
                           if IS_WINDOWS else 'curl(){ command curl -4 "$@"; }; export -f curl; "$1"')
        check: subprocess.CompletedProcess[str] | None = subprocess.CompletedProcess([], 0, '', '') if self._environment_checked else None
        for attempt in (() if self._environment_checked else range(1, 4)):
            self.log.event("INFO", "quark_environment_check", command=arguments[0], attempt=attempt)
            # Redirects to the download CDN need a fresh local port while the
            # configuration connection may still be in TIME_WAIT.
            first_port = 40000 + uuid.uuid4().int % 5000
            if IS_WINDOWS:
                direct_env["QUARK_CURL_LOCAL_PORT"] = f"{first_port}-{first_port + 50}"
            try:
                check = execute([str(self.s.git_bash), "-c", install_command, "_", str(install).replace('\\', '/')], cwd=self.s.quark_dir, timeout=45, operation="夸克环境自检", env=direct_env)
            except FlowError as exc:
                self.log.event("WARNING", "quark_environment_timeout", attempt=attempt, error=str(exc))
                if attempt == 3: raise
                time.sleep(attempt)
                continue
            if check.returncode == 0: break
            self.log.event("WARNING", "quark_install_check_retry", attempt=attempt)
            if attempt < 3: time.sleep(1)
        assert check is not None
        if check.returncode:
            lines = ((check.stderr or "") + "\n" + (check.stdout or "")).splitlines()
            detail = next((line.strip() for line in lines if "[error]" in line.lower()), "未知原因")
            raise FlowError(f"夸克 Skill 环境自检失败：{detail}")
        self._environment_checked = True
        tracked_arguments = list(arguments)
        install_results = []
        for line in check.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("type") == "install_result": install_results.append(item)
        if install_results and install_results[-1].get("success") is True and install_results[-1].get("updated") is True:
            tracked_arguments.append("--skill-updated")
        if self.session_input and self.session_id:
            tracked_arguments.extend(["--session-input", self.session_input, "--session-id", self.session_id])
        self.log.event("INFO", "quark_command_started", command=arguments[0])
        result = execute(["node", str(cli), *tracked_arguments], cwd=ROOT, timeout=timeout, operation=f"夸克 {arguments[0]}", env=direct_env)
        try:
            rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")]
            results = [row for row in rows if isinstance(row, dict) and row.get("type") == "result"]
            if results and isinstance(results[-1].get("notice"), dict) and results[-1]["notice"].get("text"):
                self.log.event("INFO", "quark_notice", text=results[-1]["notice"]["text"])
            errors = [row for row in results if row.get("code") not in (0, "0")]
            if arguments[0] == "upload":
                self.log.event("INFO", "quark_upload_result", response=[row for row in rows if row.get("type") != "progress"])
            if result.returncode or not results or errors:
                raise FlowError(errors[0].get("msg", "夸克命令失败") if errors else "夸克未返回成功结果")
        except (ValueError, TypeError) as exc:
            raise FlowError("夸克返回了无效结果") from exc
        self.log.event("INFO", "quark_command_finished", command=arguments[0])
        return rows

    def browse(self, parent_fid: str) -> list[dict[str, Any]]:
        rows = self._run(["browse", "--parent-fid", parent_fid, "--all"])
        artifact = next((r for r in rows if r.get("type") == "artifact"), None)
        if not artifact: raise FlowError("夸克浏览未返回完整结果")
        try:
            items = [json.loads(line) for line in Path(artifact["data"]["file_path"]).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
            if any(not isinstance(item, dict) or not item.get("fid") or not item.get("filename") for item in items):
                raise ValueError("missing file identity")
            self.log.store.db.execute("INSERT INTO scans(run_id,parent_fid,items_json) VALUES(?,?,?)",
                                      (self.log.run_id, parent_fid, json.dumps(items, ensure_ascii=False)))
            self.log.store.db.commit()
            return items
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FlowError("夸克完整结果文件缺失或格式无效") from exc

    def media_directory(self) -> dict[str, Any]:
        shares = [x for x in self.browse("0") if x.get("filename") == "Share" and x.get("category") == 0]
        if len(shares) != 1: raise FlowError("Share 目录缺失或不唯一")
        media = [x for x in self.browse(shares[0]["fid"]) if x.get("filename") == "Media" and x.get("category") == 0]
        if len(media) != 1: raise FlowError("Share/Media 目录缺失或不唯一")
        return media[0]

    def media_folders(self) -> list[dict[str, Any]]:
        return [x for x in self.browse(self.media_directory()["fid"]) if x.get("category") == 0]

    def create_folder(self, parent_fid: str, name: str) -> str:
        rows = self._run(["create-folder", "--dir-path", name, "--parent-fid", parent_fid])
        fid = next((r.get("data", {}).get("fid") for r in rows if r.get("type") == "result"), None)
        if not fid: raise FlowError("创建目录结果不明，须先重新浏览核实")
        return str(fid)

    def upload_file(self, path: Path, parent_fid: str) -> list[dict[str, Any]]:
        return self._run(["upload", str(path), "--parent-fid", parent_fid], timeout=1800)

    def public_share(self, fids: str | list[str], title: str) -> tuple[str, str | None]:
        selected = [fids] if isinstance(fids, str) else fids
        if not selected: raise FlowError("没有可用于创建分享的文件")
        row = next(r for r in self._run(["share", *selected, "--title", title, "--url-type", "1", "--expired-type", "1"]) if r.get("type") == "result")
        url = row.get("data", {}).get("share_url")
        if not isinstance(url, str) or urlparse(url).hostname != "pan.quark.cn" or not urlparse(url).path.startswith("/s/"):
            raise FlowError("创建分享未返回有效链接，请在网盘核实结果后重试")
        return url, row["data"].get("passcode") or None

    def share_is_valid(self, url: str, expected_names: set[str]) -> bool:
        try:
            row = next(r for r in self._run(["share-detail", "--url", url]) if r.get("type") == "result")
        except FlowError as exc:
            if "41007" in str(exc) or "share not exist" in str(exc).lower(): return False
            raise  # A network/auth failure is not evidence that the share is invalid.
        except StopIteration as exc:
            raise FlowError("分享详情缺少结果") from exc
        data = row.get("data", {})
        files = data.get("files")
        if not isinstance(files, list): raise FlowError("分享详情缺少文件列表，无法校验内容")
        actual = {str(item.get("filename") or "") for item in files}
        total = data.get("share_info", {}).get("file_num")
        if total is not None and int(total) != len(files): return False
        return bool(actual) and expected_names == actual and len(files) == len(expected_names)

    def read_file(self, fid: str, output_dir: Path) -> Path:
        # CLI 1.0.20 exposes this operation as `download`; the file is only a
        # short-lived probe input and is removed with its private temp folder.
        rows = self._run(["download", "--fid", fid, "--output-dir", str(output_dir)])
        item = next((row for row in reversed(rows)
                     if row.get("code") == 0 and row.get("data", {}).get("filePath")), None)
        if not item or not item.get("data", {}).get("filePath"): raise FlowError("MP3 文件读取失败")
        path = Path(item["data"]["filePath"]).resolve()
        if not path.is_relative_to(output_dir.resolve()) or not path.is_file():
            raise FlowError("下载结果不在指定临时目录中")
        return path


class TaskTab:
    """One disposable CDP page for one task; no task opens additional tabs."""
    def __init__(self, endpoint: str) -> None:
        parsed_endpoint = urlparse(endpoint)
        if parsed_endpoint.scheme != "http" or parsed_endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise FlowError("Chrome CDP 必须使用本机 HTTP 地址")
        self.endpoint = endpoint
        target = self._direct_json("PUT", f"{endpoint}/json/new?about:blank")
        self.target_id = target["id"]
        ws_url = target["webSocketDebuggerUrl"]
        parsed = urlparse(ws_url)
        stream: socket.socket | None = None
        start = 2 + uuid.uuid4().int % 200
        for offset in range(3):
            family = socket.AF_INET6 if ":" in (parsed.hostname or "") else socket.AF_INET
            candidate = socket.socket(family, socket.SOCK_STREAM)
            try:
                if IS_WINDOWS and family == socket.AF_INET:
                    candidate.bind((f"127.0.0.{2 + (start + offset) % 250}", 50000 + uuid.uuid4().int % 10000))
                candidate.settimeout(5)
                candidate.connect((parsed.hostname or "127.0.0.1", parsed.port or 80))
                stream = candidate
                break
            except OSError:
                candidate.close()
        try:
            if stream is None: raise FlowError("无法建立 Chrome CDP WebSocket 直连")
            self.ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=15, socket=stream,
                                                   http_no_proxy=["127.0.0.1", "localhost"])
            self.ident = 0
            self.call("Page.enable")
            self.call("Emulation.setDeviceMetricsOverride", {
                "width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False,
            })
        except Exception:
            if stream: stream.close()
            try: self._direct_http("GET", f"{endpoint}/json/close/{self.target_id}")
            except Exception: pass
            raise

    @staticmethod
    def _direct_http(method: str, url: str) -> str:
        parsed = urlparse(url)
        last_error: Exception | None = None
        for _ in range(3):
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=5,
                                                    source_address=(f"127.0.0.{2 + uuid.uuid4().int % 250}", 0) if IS_WINDOWS else None)
            try:
                path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                connection.request(method, path, headers={"Connection": "close"})
                response = connection.getresponse()
                if response.status != 200: raise FlowError(f"Chrome CDP HTTP 响应异常：{response.status}")
                return response.read().decode("utf-8")
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
            finally:
                connection.close()
        raise FlowError(f"Chrome CDP HTTP 直连失败：{last_error}")

    @classmethod
    def _direct_json(cls, method: str, url: str) -> dict[str, Any]:
        try: return json.loads(cls._direct_http(method, url))
        except json.JSONDecodeError as exc: raise FlowError("Chrome CDP 返回了无效响应") from exc

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + 20
        try:
            self.ident += 1; self.ws.send(json.dumps({"id": self.ident, "method": method, "params": params or {}}))
            while time.monotonic() < deadline:
                self.ws.settimeout(max(0.1, deadline - time.monotonic()))
                result = json.loads(self.ws.recv())
                if result.get("id") == self.ident:
                    if "error" in result: raise FlowError(result["error"].get("message", "CDP 调用失败"))
                    return result["result"]
        except (OSError, websocket.WebSocketException, ValueError, KeyError) as exc:
            raise FlowError(f"浏览器连接异常（{method}）：{type(exc).__name__}") from exc
        raise FlowError(f"浏览器调用超时：{method}")

    def evaluate(self, expression: str) -> Any:
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            message = details.get("exception", {}).get("description") or details.get("text") or "网页脚本失败"
            raise FlowError(message)
        return result["result"].get("value")

    def go(self, url: str) -> None:
        previous = self.call("Page.getFrameTree")["frameTree"]["frame"]
        navigation = self.call("Page.navigate", {"url": url})
        if navigation.get("errorText"): raise FlowError(f"页面导航失败：{navigation['errorText']}")
        deadline = time.monotonic() + 20
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            time.sleep(0.25)
            frame = self.call("Page.getFrameTree")["frameTree"]["frame"]
            if frame.get("id") != navigation.get("frameId"): continue
            # A site's automatic verification can reload the document. Accept
            # its new loader, but never mistake the previous page for this visit.
            if navigation.get("loaderId") and frame.get("loaderId") == previous.get("loaderId"): continue
            try:
                state = self.evaluate("""({ready:document.readyState,body:!!document.body,host:location.hostname,guard:!!document.querySelector('script[src*="/_guard/auto.js"]')})""")
                if state.get("ready") == "complete" and state.get("body") and state.get("host") == urlparse(url).hostname and not state.get("guard"):
                    return
            except FlowError as exc:
                if "context" not in str(exc).lower(): raise
        raise FlowError(f"页面加载超时或跳转至其他站点（主机={state.get('host', '未知')}，状态={state.get('ready', '未知')}，自动验证={bool(state.get('guard'))}）")

    def wait_for(self, expression: str, error: str) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(0.25)
            if self.evaluate(expression): return
        raise FlowError(error)

    def close(self, keep_open: bool) -> None:
        self.ws.close()
        if not keep_open:
            self._direct_http("GET", f"{self.endpoint}/json/close/{self.target_id}")


class HiFiNi:
    def __init__(self, settings: Settings, log: JsonLogger) -> None:
        self.tab, self.log, self.settings = TaskTab(settings.cdp_url), log, settings

    def close(self, keep_open: bool) -> None: self.tab.close(keep_open)
    def _eval(self, url: str, expression: str) -> Any:
        self.tab.go(url); return self.tab.evaluate(expression)

    def my_threads(self) -> list[dict[str, Any]]:
        expr = """JSON.stringify({logged:!!document.querySelector('a[href*=logout],a[href*=user-logout]'),blocked:/安全验证|访问过于频繁|验证码|502 Bad Gateway|503 Service/.test(document.body.innerText),valid:!!document.querySelector('.threadlist')||/没有更多|暂无主题|没有主题/.test(document.body.innerText),items:[...document.querySelectorAll('.media-body')].map(x=>{const a=x.querySelector('.subject a');return a?{title:a.innerText.trim(),url:new URL(a.getAttribute('href'),location.href).href,pending:!!x.querySelector('i.icon-coffee')}:null}).filter(Boolean),next:(document.querySelector('.pagination a[rel=next]')||document.querySelector('.pagination .active + .page-item:not(.disabled) a')||[...document.querySelectorAll('.pagination a')].find(a=>/下一页|›|»/.test(a.innerText)&&!a.closest('.disabled')))?.href||null})"""
        url: str | None = "https://www.hifini.net/my-thread.htm"
        visited: set[str] = set(); items: dict[str, dict[str, Any]] = {}
        while url:
            if url in visited or len(visited) >= 100: raise FlowError("我的帖子分页循环或超过安全上限，无法完成查重")
            if urlparse(url).hostname != "www.hifini.net" or not re.fullmatch(r"/my-thread(?:-\d+)?\.htm", urlparse(url).path):
                raise FlowError("我的帖子分页地址异常")
            visited.add(url)
            result = json.loads(self._eval(url, expr))
            if result.get("blocked"): raise FlowError("HiFiNi 要求验证或暂时不可用")
            if not result["logged"]: raise FlowError("HiFiNi 未登录")
            if not result.get("valid") and not result["items"]: raise FlowError("无法识别我的帖子页面")
            for item in result["items"]: items[item["url"]] = item
            url = result.get("next")
        return list(items.values())

    def ensure_logged_in(self) -> list[dict[str, Any]]:
        try:
            items = self.my_threads()
            self.log.event("INFO", "hifini_login_ok")
            return items
        except FlowError as exc:
            if str(exc) != "HiFiNi 未登录": raise
        if not self.settings.hifini_username or not self.settings.hifini_password:
            raise FlowError("HiFiNi 未登录，且未配置账号密码")
        self.tab.go("https://www.hifini.net/user-login.htm")
        submitted = self.tab.evaluate(f"""(()=>{{const set=(s,v)=>{{const e=document.querySelector(s);if(!e)throw Error(s);const d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');d.set.call(e,v);e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));}};set('#email',{json.dumps(self.settings.hifini_username)});set('#password',{json.dumps(self.settings.hifini_password)});const form=document.querySelector('form');if(!form)throw Error('未找到登录表单');form.requestSubmit();return true}})()""")
        if not submitted: raise FlowError("HiFiNi 登录表单提交失败")
        time.sleep(3)
        items = self.my_threads()
        self.log.event("INFO", "hifini_login_restored")
        return items

    def duplicate(self, title: str, artist: str | None = None, song: str | None = None) -> dict[str, Any] | None:
        wanted = (normalize_identity(artist or ""), normalize_identity(song or ""))
        return next((item for item in self.my_threads()
                     if item["title"].strip() == title.strip()
                     or (all(wanted) and post_identity(item["title"]) == wanted)), None)

    def draft(self, title: str, artist: str, category: str, lyrics_html: str, share_url: str,
              share_passcode: str | None = None) -> None:
        validate_lyrics_layout(lyrics_html)
        expected_lyric_paragraphs = len(re.findall(r"<p>", lyrics_html))
        passcode_text = share_passcode or "无"
        self.tab.go("https://www.hifini.net/thread-create-0.htm")
        self.tab.wait_for("!!document.querySelector('#subject') && !!window.tinymce?.get('message')?.initialized", "发帖编辑器未就绪")
        has_netdisk_button = bool(self.tab.evaluate("!![...document.querySelectorAll('svg path')].find(x=>x.getAttribute('d')?.startsWith('M5 16h14'))"))
        if has_netdisk_button:
            self._draft_with_toolbar(title, artist, category, lyrics_html, share_url, passcode_text)
            self.log.event("INFO", "netdisk_toolbar_used")
            return
        category_id = {"华语": "1", "日韩": "2", "欧美": "3"}.get(category)
        if not category_id: raise FlowError(f"不支持自动选择的分区：{category}")
        # HiFiNi can declare xzlink in TinyMCE settings without successfully
        # registering its button. This is the plugin's equivalent [post]
        # markup, preserving reply visibility without weakening the draft.
        download_html = (f"<h5>下载链接：</h5>[post]<a target='_blank' rel='noopener' "
                         f"href='{escape(share_url, quote=True)}'>{escape(share_url)}</a>[/post]"
                         f"<h5>提取码：</h5>[post]{escape(passcode_text)}[/post]")
        self.tab.evaluate(rf"""(()=>{{const set=(s,v)=>{{const e=document.querySelector(s);if(!e)throw Error(s);const proto=e instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(e,v);e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));}};set('#subject',{json.dumps(title)});set('#fox_tag',{json.dumps(artist)});set('select[name=fid]',{json.dumps(category_id)});const ed=tinymce.get('message');if(!ed)throw Error('编辑器未就绪');ed.setContent({json.dumps(lyrics_html + download_html)});ed.fire('change');const final=ed.getContent(),i=final.indexOf({json.dumps(share_url)}),pcount=(final.slice(0,i).match(/<p(?:\s[^>]*)?>/g)||[]).length;if(i<0||pcount<{expected_lyric_paragraphs}||!/<p>&nbsp;<\/p>\s*<p>&nbsp;<\/p>\s*<h5>/.test(final.slice(0,i))||!final.includes('[post]')||/<(?:span|font)\b|style=/i.test({json.dumps(lyrics_html)}))throw Error('正文布局或歌词格式校验失败');return true}})()""")
        self.log.event("WARNING", "netdisk_toolbar_fallback", reason="xzlink_not_registered")

    def _draft_with_toolbar(self, title: str, artist: str, category: str, lyrics_html: str,
                            share_url: str, passcode_text: str) -> None:
        validate_lyrics_layout(lyrics_html)
        expected_lyric_paragraphs = len(re.findall(r"<p>", lyrics_html))
        # The add-netdisk plugin is intentionally exercised exactly as the UI does:
        # set its hidden link fields, choose Reply-visible, then reassert two blank lines.
        category_id = {"华语": "1", "日韩": "2", "欧美": "3"}.get(category)
        if not category_id: raise FlowError(f"不支持自动选择的分区：{category}")
        expression = f"""(()=>{{const set=(s,v)=>{{const e=document.querySelector(s);if(!e)throw Error(s);const proto=e instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(e,v);e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));}};set('#subject',{json.dumps(title)});set('#fox_tag',{json.dumps(artist)});set('select[name=fid]',{json.dumps(category_id)});const ed=tinymce.get('message');if(!ed)throw Error('编辑器未就绪');ed.setContent({json.dumps(lyrics_html)});ed.focus();ed.selection.select(ed.getBody(),true);ed.selection.collapse(false);const path=[...document.querySelectorAll('svg path')].find(x=>x.getAttribute('d')?.startsWith('M5 16h14'));if(!path)throw Error('未找到添加网盘按钮');path.closest('button').click();const setPlugin=(p,v)=>{{const x=[...document.querySelectorAll('input')].find(i=>i.placeholder===p);if(!x)throw Error('网盘字段缺失');const d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');d.set.call(x,v);x.dispatchEvent(new Event('input',{{bubbles:true}}));x.dispatchEvent(new Event('change',{{bubbles:true}}));}};setPlugin('请输入网盘下载链接',{json.dumps(share_url)});setPlugin('请输入提取码',{json.dumps(passcode_text)});const reply=[...document.querySelectorAll('button')].find(x=>x.offsetParent!==null&&x.innerText.trim()==='回复可见');if(!reply)throw Error('未找到回复可见选项');reply.click();let c=ed.getContent();c=c.replace('<p>&nbsp;</p>\\n<h5>','<p>&nbsp;</p>\\n<p>&nbsp;</p>\\n<h5>');ed.setContent(c);ed.fire('change');const final=ed.getContent(),i=final.indexOf({json.dumps(share_url)}),pcount=(final.slice(0,i).match(/<p(?:\\s[^>]*)?>/g)||[]).length;if(i<0||pcount<{expected_lyric_paragraphs}||!/<p>&nbsp;<\\/p>\\n<p>&nbsp;<\\/p>\\n<h5>/.test(final.slice(0,i))||/<(?:span|font)\\b|style=/i.test({json.dumps(lyrics_html)}))throw Error('正文布局或歌词格式校验失败');return 'draft-ready'}})()"""
        # TinyMCE collapses the netdisk control behind “更多” on narrower
        # windows. Expand the overflow row before declaring the control absent.
        expression = expression.replace("const path=", "let path=").replace(
            "if(!path)throw Error('未找到添加网盘按钮');",
            "if(!path){const more=[...document.querySelectorAll('.tox-toolbar button')].find(x=>x.getAttribute('aria-label')==='更多'||x.innerText.trim()==='更多');more?.click();path=[...document.querySelectorAll('svg path')].find(x=>x.getAttribute('d')?.startsWith('M5 16h14'));}if(!path)throw Error('未找到添加网盘按钮');",
        )
        self.tab.evaluate(expression)

    def submit_and_verify(self, title: str, artist: str, song: str) -> dict[str, Any]:
        # This is only called after the command line explicitly opts in to --publish.
        self.tab.evaluate("""(()=>{window.__publishResponse=null;window.__publishAlert=null;
        const original=$.xpost;$.xpost=function(url,data,callback,...rest){return original.call(this,url,data,function(code,message,...args){window.__publishResponse={code:code,message:typeof message==='string'?message:''};return callback.call(this,code,message,...args)},...rest)};
        window.alert=function(message){window.__publishAlert=String(message)};
        const b=[...document.querySelectorAll('button')].find(x=>x.offsetParent!==null&&x.innerText.trim()==='发表主题');if(!b)throw Error('未找到发表主题按钮');b.click();return true})()""")
        result = None
        for wait in (2, 4, 8):
            time.sleep(wait)
            page = self.tab.evaluate("({text:document.body.innerText,response:window.__publishResponse||null,alert:window.__publishAlert||null})")
            page_text = str(page.get('text', '')) if isinstance(page, dict) else str(page)
            response = page.get('response') if isinstance(page, dict) else None
            if isinstance(response, dict):
                self.log.event('INFO', 'submission_response', code=response.get('code'), message=response.get('message'))
                message = str(response.get('message') or '')
                if '该标题已经被使用' in message: raise DuplicateTitleError(message)
                code = response.get('code')
                explicit_field_error = code in {'subject', 'message', 'fid', 'fox_tag', 'tag', 'doctype'}
                if (isinstance(code, (int, float)) and not isinstance(code, bool) and code > 0) or (isinstance(code, str) and (explicit_field_error or (code.isdigit() and int(code) > 0))):
                    raise SubmissionRejectedError('HiFiNi 拒绝提交：' + message)
            if "该标题已经被使用，请换其他标题试试" in page_text:
                raise DuplicateTitleError("HiFiNi 提示标题已被使用")
        # Keep the submission page intact while its asynchronous response settles.
        for wait in (0, 2, 4):
            time.sleep(wait)
            result = self.duplicate(title, artist, song)
            if result: break
        if not result: raise FlowError("提交后未在我的帖子中找到同名帖子")
        return result

    def audit_thread(self, url: str) -> str:
        page = self._eval(url, "({text:document.body.innerText,url:location.href,thread:!!document.querySelector('.card-thread .message, .thread .message'),logged:!!document.querySelector('a[href*=logout],a[href*=user-logout]')})")
        text = page["text"]
        if not page.get("logged") or re.search(r"安全验证|验证码|访问过于频繁|502 Bad Gateway|503 Service", text):
            raise FlowError("审核页面未登录、要求验证或暂时不可用")
        if urlparse(page.get("url", "")).path != urlparse(url).path:
            raise FlowError("审核页面发生非预期跳转")
        if "该主题正在由管理员审核中" in text: return "PENDING_REVIEW"
        if any(token in text for token in ("主题不存在", "主题已被删除")): return "DELETED"
        if "审核未通过" in text: return "REJECTED"
        if "无权查看" in text: raise FlowError("当前账号无权查看，不能判定为审核拒绝")
        if page.get("thread"): return "APPROVED"
        raise FlowError("无法识别主题正文，保留原审核状态")


def classify(artist: str, song: str) -> str:
    if re.search(r"[ぁ-んァ-ヶ가-힣]", artist): return "日韩"
    return "华语" if any("\u4e00" <= c <= "\u9fff" for c in artist) else "欧美"


def parse_folder(name: str) -> tuple[str, str] | None:
    if " x " in name.lower(): return None
    for separator in (" - ", "-"):
        if separator in name:
            left, right = (part.strip() for part in name.split(separator, 1))
            if left and right: return left, right  # Project convention: artist-song.
    return None


def normalize_identity(value: str) -> str:
    """Compare human names without case, punctuation, spacing or accents."""
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in decomposed if char.isalnum() and not unicodedata.combining(char))


def post_identity(title: str) -> tuple[str, str] | None:
    bracketed = re.match(r"^\s*(.*?)\s*(?:-\s*)?《([^》]+)》", title)
    if bracketed:
        artist, song = bracketed.groups()
        return normalize_identity(artist), normalize_identity(song)
    if " - " in title:
        artist, song = (part.strip() for part in title.split(" - ", 1))
        song = re.split(r"\s*\[(?:FLAC|MP3|WAV|APE|ALAC|AAC)", song, maxsplit=1, flags=re.I)[0]
        if artist and song: return normalize_identity(artist), normalize_identity(song)
    return None


def special_version_reason(items: list[dict[str, Any]], folder_name: str) -> str | None:
    pattern = re.compile(
        r"(?i)(?<![a-z])(?:live|remix|cover|dj|acoustic|instrumental|demo|version|edit)(?![a-z])|现场|翻唱|伴奏(?:版)?|纯音乐"
    )
    audio_suffixes = {".flac", ".wav", ".ape", ".alac", ".mp3", ".m4a", ".aac"}
    audio_names = [str(item.get("filename") or "") for item in items
                   if Path(str(item.get("filename") or "")).suffix.lower() in audio_suffixes]
    marked = next((name for name in audio_names if pattern.search(name)), None)
    if marked: return f"检测到特殊版本文件：{marked}"
    parsed = parse_folder(folder_name)
    if len(audio_names) > 1 and parsed:
        song_key = normalize_identity(parsed[1])
        bases: set[str] = set()
        for name in audio_names:
            stem = re.sub(r"(?i)[\[（(]\s*(?:128|192|256|320)\s*k(?:bps)?\s*[\]）)]", "", Path(name).stem)
            key = normalize_identity(stem)
            expected = {song_key, normalize_identity(parsed[0] + parsed[1]), normalize_identity(parsed[1] + parsed[0])}
            bases.add(song_key if key in expected else key)
        if len(bases) > 1:
            return "同一目录内存在基础名称不一致的音频文件：" + "、".join(audio_names)
    return None


def mp3_bitrate_label(kbps: float | int | None) -> str:
    if kbps is None: raise BitrateProbeError("无法确认 MP3 码率")
    value = float(kbps)
    if not math.isfinite(value) or not 8 <= value <= 321:
        raise BitrateProbeError("MP3 音频流码率无效")
    # VBR averages must not be rounded up to an advertised CBR tier.
    return f"MP3-{min(320, round(value))}K"


def ffprobe_path() -> str:
    configured = os.getenv("FFPROBE_BIN")
    found = shutil.which(configured or "ffprobe")
    if found: return found
    raise BitrateProbeError("未安装 ffprobe，无法确认 MP3 码率")


def probe_local_mp3(path: Path) -> dict[str, Any]:
    result = execute([ffprobe_path(), "-v", "error", "-select_streams", "a",
                      "-show_entries", "stream=codec_name,bit_rate,sample_rate,channels,duration",
                      "-of", "json", str(path)], timeout=60, operation="本地 MP3 音频流检查")
    if result.returncode or result.stderr.strip(): raise BitrateProbeError("MP3 音频流检查失败")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if len(streams) != 1 or streams[0].get("codec_name") != "mp3":
        raise BitrateProbeError("文件必须包含且仅包含一个 MP3 音频流")
    stream = streams[0]
    kbps = int(stream.get("bit_rate") or 0) / 1000
    mp3_bitrate_label(kbps)
    duration = float(stream.get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0: raise BitrateProbeError("音频时长无效")
    return {"stream": stream, "kbps": round(kbps), "label": mp3_bitrate_label(kbps)}


def probe_mp3_bitrate(quark: Quark, item: dict[str, Any], log: JsonLogger) -> int | None:
    """Read one MP3, inspect its audio stream, and remove the temporary copy."""
    with tempfile.TemporaryDirectory(prefix="quark-hifini-") as temp_name:
        local_path = quark.read_file(str(item["fid"]), Path(temp_name))
        kbps = probe_local_mp3(local_path)["kbps"]
        log.event("INFO", "mp3_bitrate_probed", file=item.get("filename"), kbps=kbps)
        return kbps


def inspect_mp3_bitrates(quark: Quark, items: list[dict[str, Any]], log: JsonLogger) -> dict[str, int]:
    measured: dict[str, int] = {}
    for item in items:
        name = str(item.get("filename") or item.get("file_name") or item.get("name") or "")
        if Path(name).suffix.lower() != ".mp3" or not item.get("fid"): continue
        try:
            bitrate = probe_mp3_bitrate(quark, item, log)
            if bitrate is not None: measured[str(item["fid"])] = bitrate
        except (FlowError, OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            log.event("WARNING", "mp3_bitrate_probe_failed", file=name, error=str(exc))
            raise BitrateProbeError(f"{name}：{exc}") from exc
    return measured


def audio_format_label(items: list[dict[str, Any]], measured_bitrates: dict[str, int] | None = None) -> str:
    """Build the HiFiNi title suffix from the audio files in one folder."""
    labels: set[str] = set()
    lossless = {".flac": "FLAC", ".wav": "WAV", ".ape": "APE", ".alac": "ALAC"}
    for item in items:
        name = str(item.get("filename") or item.get("file_name") or item.get("name") or "")
        suffix = Path(name).suffix.lower()
        if suffix in lossless:
            labels.add(lossless[suffix])
            continue
        if suffix == ".mp3":
            bitrate = (measured_bitrates or {}).get(str(item.get("fid")))
            labels.add(mp3_bitrate_label(bitrate))
        elif suffix == ".aac":
            labels.add("AAC")
        elif suffix == ".m4a":
            raise FlowError("M4A 可能包含 AAC 或 ALAC，需人工确认编码后处理")
    if not labels: raise FlowError("素材目录中没有可识别的音频格式")
    order = {"FLAC": 0, "WAV": 1, "APE": 2, "ALAC": 3, "AAC": 4, "MP3-320K": 5,
             "MP3-256K": 6, "MP3-192K": 7, "MP3-128K": 8, "MP3": 9}
    return "/".join(sorted(labels, key=lambda label: (order.get(label, 99), label)))


def post_title(artist: str, song: str, format_label: str) -> str:
    return f"{artist}《{song}》[{format_label}]"


def share_passcode_from_url(url: str) -> str | None:
    values = parse_qs(urlparse(url).query)
    return next((values[key][0] for key in ("pwd", "passcode", "code") if values.get(key)), None)


def plain_lyrics_html(lyric_text: str) -> str:
    """Represent every source lyric line as its own paragraph, without source styling."""
    lines = [re.sub(r"\s*\[\d+(?:[-–]\d+)?\]\s*$", "", line).strip() for line in lyric_text.splitlines()]
    rows = [f"<p>{escape(line)}</p>" if line else "<p><br></p>" for line in lines]
    result = "".join(rows) + "<p>&nbsp;</p><p>&nbsp;</p>"
    validate_lyrics_layout(result)
    return result


def validate_lyrics_layout(lyrics_html: str) -> None:
    """Fail closed unless each lyric line occupies a separate plain paragraph."""
    paragraphs = re.findall(r"<p>(.*?)</p>", lyrics_html, flags=re.DOTALL)
    if (len(paragraphs) <= 2 or "".join(f"<p>{line}</p>" for line in paragraphs) != lyrics_html
            or paragraphs[-2:] != ["&nbsp;", "&nbsp;"]):
        raise FlowError("歌词必须逐行分段，并保留下载区前的两个空行")
    if any("\n" in line or "\r" in line or ("<" in line and line != "<br>") for line in paragraphs[:-2]):
        raise FlowError("歌词行包含未分段文本或非纯文本标签")


def is_latin_release(artist: str, song: str) -> bool:
    """Route releases whose names use Latin-script letters to LRCLIB."""
    letters = [char for char in artist + song if char.isalpha()]
    return bool(letters) and all("LATIN" in unicodedata.name(char, "") for char in letters)


def lrclib_lyrics(artist: str, song: str) -> tuple[str, str]:
    """Find lyrics on LRCLIB only when both returned artist and title match."""
    query = urlencode({"artist_name": artist, "track_name": song})
    url = f"https://lrclib.net/api/search?{query}"
    request = Request(url, headers={"User-Agent": "QuarkHiFiNi/1.0 (lyrics lookup)", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            results = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise LyricsLookupError(f"LRCLIB 查询失败：{type(exc).__name__}: {exc}") from exc
    if not isinstance(results, list):
        raise LyricsLookupError("LRCLIB 返回格式异常")
    for item in results:
        if not isinstance(item, dict):
            continue
        if normalize_identity(str(item.get("trackName") or "")) != normalize_identity(song):
            continue
        if normalize_identity(str(item.get("artistName") or "")) != normalize_identity(artist):
            continue
        lyrics = str(item.get("plainLyrics") or "").strip()
        if not lyrics:
            synced = str(item.get("syncedLyrics") or "")
            lyrics = "\n".join(re.sub(r"^\[\d{2}:\d{2}(?:\.\d{2,3})?\]\s*", "", line).strip()
                                  for line in synced.splitlines())
        if lyrics.strip():
            return lyrics.strip(), url
    raise LyricsLookupError("LRCLIB 未找到歌手和歌名均精确匹配且含歌词的记录")


def lyrics_html(browser: HiFiNi, folder_name: str, reviewer: ShadowReviewer | None = None) -> tuple[str, str, str]:
    if " x " in folder_name.lower(): raise FlowError("混合曲目录，等待人工处理")
    parsed = parse_folder(folder_name)
    if not parsed: raise FlowError("目录名不含歌手/歌曲分隔符")
    artist, song = parsed
    if is_latin_release(artist, song):
        lyric_text, source_url = lrclib_lyrics(artist, song)
        if reviewer is not None:
            page = {"h1": song, "intro": f"{artist} — {song}", "lyrics": lyric_text}
            reviewer.page(folder_name, artist, song, page, source_url, True)
        browser.log.event("INFO", "lyrics_verified", resource=folder_name, source_url=source_url,
                          provider="lrclib", characters=len(lyric_text))
        return artist, song, plain_lyrics_html(lyric_text)
    candidate_urls: list[str] = []
    search_errors: list[str] = []
    for keyword in (f"{artist} - {song}", song):
        try:
            search = json.loads(browser._eval(
                f"https://baike.baidu.com/search?word={quote(keyword)}",
                """(()=>{const body=document.body.innerText;if(/百度安全验证|请输入验证码|访问异常/.test(body))throw Error('百度百科要求安全验证');return JSON.stringify([...document.querySelectorAll('a')].map(a=>({text:(a.innerText||'').trim(),href:a.href||''})).filter(a=>a.href.includes('/item/')))})()""",
            ))
            candidate_urls.extend([item["href"] for item in search
                                   if normalize_identity(song) in normalize_identity(item.get("text", ""))
                                   and urlparse(item["href"]).hostname == "baike.baidu.com"][:5])
        except (FlowError, ValueError, TypeError, KeyError) as exc:
            search_errors.append(str(exc))
    # Search pages sometimes expose no conventional /item/ anchor despite an
    # exact lemma existing.  The canonical song URL is the final candidate.
    candidate_urls.append(f"https://baike.baidu.com/item/{quote(song)}?fromModule=lemma_search-box")
    matched_song_and_artist = False
    loaded_pages = 0
    for url in dict.fromkeys(candidate_urls):
        try:
            text = browser._eval(url, """(()=>{const body=document.body.innerText;if(/百度安全验证|请输入验证码|访问异常/.test(body))throw Error('百度百科要求安全验证');const h1=document.querySelector('h1')?.innerText?.trim()||'',start=body.indexOf(h1),h=[...document.querySelectorAll('h2')].find(x=>x.innerText.trim()==='歌曲歌词');let node=h?.parentElement?.nextElementSibling,parts=[];while(node&&!node.querySelector('h2')&&!node.className?.toString().includes('paraTitle')){const value=node.innerText?.trim();if(value)parts.push(value);node=node.nextElementSibling;}return JSON.stringify({h1,intro:start>=0?body.slice(start,start+1200):body.slice(0,1200),lyrics:parts.join('\\n')})})()""")
            page = json.loads(text)
        except (FlowError, json.JSONDecodeError):
            continue
        if not page.get("h1"): continue
        loaded_pages += 1
        lyric_text = str(page["lyrics"]).strip()
        intro_key, artist_key = normalize_identity(page["intro"]), normalize_identity(artist)
        singer_at = intro_key.find(artist_key)
        singer_confirmed = singer_at >= 0 and any(
            marker in intro_key[singer_at:singer_at + len(artist_key) + 30] for marker in ("演唱", "主唱")
        )
        local_match = normalize_identity(page["h1"]) == normalize_identity(song) and singer_confirmed
        if reviewer is not None:
            reviewer.page(folder_name, artist, song, page, url, local_match)
        if not local_match:
            continue
        matched_song_and_artist = True
        if not lyric_text.strip(): continue
        browser.log.event("INFO", "lyrics_verified", resource=folder_name, source_url=url, characters=len(lyric_text))
        return artist, song, plain_lyrics_html(lyric_text)
    if matched_song_and_artist:
        raise NoLyricsError("百度百科已匹配歌手和歌曲，但词条缺少可用的“歌曲歌词”章节")
    detail = "已打开候选词条，但无法可靠匹配歌手和歌曲" if loaded_pages else "未找到可验证的歌曲词条"
    raise LyricsLookupError(f"百度百科查询失败：{detail}" + (f"；搜索异常：{search_errors[0]}" if search_errors else ""))


def wait_text(seconds: int | None) -> str:
    if not seconds or seconds < 60: return "不足 1 分钟"
    days, rest = divmod(seconds, 86400); hours, rest = divmod(rest, 3600); minutes = rest // 60
    return (f"{days} 天 {hours} 小时" if days else f"{hours} 小时 {minutes} 分钟")


def review_existing_posts(store: Store, hifini: HiFiNi, notify: Feishu, log: JsonLogger, run_id: str) -> bool:
    """Recheck every known post; deleted/rejected states are permanent skips."""
    healthy = True
    for item in store.audit_candidates():
        try:
            status = hifini.audit_thread(item["hifini_url"])
            if status == "PENDING_REVIEW":
                store.audit(item["id"], status, "主题仍在管理员审核中")
                due = store.db.execute("SELECT audit_alerted_at IS NULL OR audit_alerted_at<=datetime('now','-1 day') FROM resources WHERE id=?", (item["id"],)).fetchone()[0]
                if due and notify.alert("主题仍在审核中", f"{item['artist']} - {item['song']}\n等待：{wait_text(item['pending_seconds'])}\n{item['hifini_url']}"):
                    store.db.execute("UPDATE resources SET audit_alerted_at=CURRENT_TIMESTAMP WHERE id=?", (item["id"],)); store.db.commit()
                log.event("INFO", "audit_pending", resource=item["folder_name"], wait_seconds=item["pending_seconds"])
            elif status == "APPROVED":
                store.audit(item["id"], status, "主题页可正常访问，审核已通过")
                log.event("INFO", "audit_approved", resource=item["folder_name"])
            elif status == "DELETED":
                store.audit(item["id"], status, "HiFiNi 显示主题不存在或已被删除；永久跳过")
                notify.alert("已发布主题被删除", f"{item['artist']} - {item['song']}\n已标记为永久跳过，不会重新发布。\n{item['hifini_url']}")
                log.event("ERROR", "audit_deleted", resource=item["folder_name"], url=item["hifini_url"])
            else:
                store.audit(item["id"], status, "主题页显示审核失败、删除或无权访问")
                notify.alert("主题审核失败", f"{item['artist']} - {item['song']}\n{item['hifini_url']}")
                log.event("ERROR", "audit_rejected", resource=item["folder_name"])
        except FlowError as exc:
            healthy = False
            # Do not call an unavailable page a rejection: retain its prior state.
            store.event(run_id, "ERROR", "audit_check_failed", str(exc), item["id"])
            notify.alert("审核状态检查异常", f"{item['artist']} - {item['song']}：{exc}")
            log.event("ERROR", "audit_check_failed", resource=item["folder_name"], error=str(exc))
    return healthy


def run(args: argparse.Namespace) -> int:
    try:
        with RunLock():
            settings = Settings.load()
            durable = Store()
            store = durable
            try:
                imported = durable.import_logs()
                run_id = uuid.uuid4().hex
                mode = "publish" if args.publish else "dry_run"
                durable.start_run(run_id, mode)
                log = JsonLogger(run_id, durable)
                log.event("INFO", "run_started", mode=mode, imported_logs=imported)
                if not args.publish:
                    store = Store(":memory:")
                    durable.db.backup(store.db)
                return run_workflow(args, settings, store, log)
            finally:
                if store is not durable: store.close()
                durable.close()
    except (OSError, sqlite3.Error, FlowError) as exc:
        print(f"任务无法启动或记录数据：{redact(str(exc))}", file=sys.stderr, flush=True)
        return 1


def run_workflow(args: argparse.Namespace, settings: Settings, store: Store, log: JsonLogger) -> int:
    run_id = log.run_id
    durable = log.store
    notify = Feishu(settings, log, enabled=args.publish)
    reviewer = ShadowReviewer(settings.typesafe, log, redact)
    hifini: HiFiNi | None = None
    try:
        notify.retry_pending()
        quark_session_id = getattr(args, "session_id", None) or f"{int(time.time())}-{run_id[:6]}"
        quark = Quark(settings, log, getattr(args, "session_input", None), quark_session_id)
        hifini = HiFiNi(settings, log)
        hifini.ensure_logged_in()
        audit_healthy = review_existing_posts(store, hifini, notify, log, run_id)
        unresolved = store.db.execute("SELECT r.*, a.title FROM resources r JOIN submission_attempts a ON a.resource_id=r.id WHERE a.status='UNKNOWN'").fetchall()
        for item in unresolved:
            match = hifini.duplicate(item["title"])
            if match:
                store.mark_submitted(item["id"], match["url"])
                log.event("INFO", "submission_reconciled", resource_id=item["id"])
            else:
                notify.alert("提交结果待核实", f"{item['folder_name']}：上次提交结果未知，禁止自动重发，请检查我的帖子。")
                raise FlowError("存在无法核实的提交，已阻止继续发布")
        if args.publish and store.submitted_today() >= DAILY_LIMIT:
            detail = "今日已提交 1 篇，达到每日发布上限"
            status = "SUCCESS" if audit_healthy else "PARTIAL_FAILURE"
            durable.finish_run(run_id, status, detail)
            log.event("INFO", "run_finished", status=status, reason="daily_limit")
            return 0 if audit_healthy else 1
        folders = quark.media_folders(); log.event("INFO", "media_scanned", count=len(folders))
        outcome = "SUCCESS" if audit_healthy else "PARTIAL_FAILURE"
        prepared = 0
        for folder in folders:
            record = store.upsert_resource(folder)
            if record["status"] in {"PUBLISHED", "PENDING_REVIEW", "SUBMITTED", "SKIPPED_TOO_MANY_FAILURES",
                                    "SKIPPED_NO_LYRICS", "SKIPPED_BAIKE_LOOKUP",
                                    "SKIPPED_BITRATE_FAILURE", "SKIPPED_TITLE_CONFLICT", "SKIPPED_MANUAL_REVIEW",
                                    "SUBMISSION_UNKNOWN", "REJECTED", "DELETED", "SUPERSEDED"}:
                log.event("INFO", "resource_already_handled", resource_id=record["id"], status=record["status"])
                continue
            if record["failure_count"] >= MAX_FAILURES:
                store.update(record["id"], "SKIPPED_TOO_MANY_FAILURES", detail="连续失败达到 3 次"); continue
            prior_rejection = store.db.execute(
                "SELECT detail FROM submission_attempts WHERE resource_id=? AND status='REJECTED' "
                "AND detail LIKE '%HiFiNi 拒绝提交%' ORDER BY id DESC LIMIT 1", (record['id'],)).fetchone()
            if prior_rejection:
                store.update(record['id'], 'SKIPPED_MANUAL_REVIEW', detail=prior_rejection['detail'])
                log.event('WARNING', 'resource_rejected_needs_review', resource_id=record['id'], reason=prior_rejection['detail'])
                continue
            if not parse_folder(folder["filename"]):
                store.update(record["id"], "SKIPPED_MANUAL_REVIEW", detail="目录命名无效或混合歌名，按规则跳过")
                notify.alert("素材需要人工检查", folder["filename"])
                log.event("WARNING", "resource_skipped", resource=folder["filename"], reason="mixed_title")
                continue
            try:
                artist, song = parse_folder(folder["filename"])
                duplicate = hifini.duplicate("", artist, song)
                if duplicate:
                    store.update(record["id"], "PENDING_REVIEW" if duplicate["pending"] else "PUBLISHED", artist=artist, song=song, category=classify(artist, song), hifini_url=duplicate["url"], detail="HiFiNi 已存在同名帖子")
                    log.event("INFO", "existing_post_found", resource_id=record["id"])
                    continue
                folder_items = quark.browse(folder["fid"])
                special_reason = special_version_reason(folder_items, folder["filename"])
                reviewer.versions(folder["filename"], folder_items, special_reason)
                if special_reason:
                    detail = special_reason
                    store.update(record["id"], "SKIPPED_MANUAL_REVIEW", detail=detail)
                    store.event(run_id, "WARNING", "resource_skipped_special_version", detail, record["id"])
                    notify.alert("特殊版本素材已跳过", f"{folder['filename']}\n{detail}")
                    log.event("WARNING", "resource_skipped", resource=folder["filename"],
                              reason="special_version", detail=detail)
                    continue
                artist, song, lyrics = lyrics_html(hifini, folder["filename"], reviewer)
                measured_bitrates = inspect_mp3_bitrates(quark, folder_items, log)
                format_label = audio_format_label(folder_items, measured_bitrates)
                title = post_title(artist, song, format_label)
                share_url = record["share_url"]
                share_passcode = record["share_passcode"] or (share_passcode_from_url(share_url) if share_url else None)
                validation_url = share_url + ("&" if "?" in share_url else "?") + urlencode({"pwd": share_passcode}) if share_url and share_passcode else share_url
                if share_url and not quark.share_is_valid(validation_url, {folder["filename"]}):
                    log.event("WARNING", "share_invalid_recreated", resource=folder["filename"])
                    share_url = None
                    share_passcode = None
                if not share_url:
                    if args.publish:
                        share_url, share_passcode = quark.public_share(folder["fid"], title)
                        # Save immediately so a later editor failure does not create another share.
                        store.update(record["id"], "PREPARING", share_url=share_url, share_passcode=share_passcode)
                    else:
                        share_url = PREVIEW_URL
                        log.event("INFO", "share_preview_placeholder", resource_id=record["id"])
                category = classify(artist, song)
                store.update(record["id"], "PREPARING", artist=artist, song=song, category=category,
                             share_url=share_url, share_passcode=share_passcode)
                durable.save_draft(run_id, folder["filename"], title, category, lyrics, share_url, share_passcode,
                                   folder_items, measured_bitrates, not args.publish)
                hifini.draft(title, artist, category, lyrics, share_url, share_passcode)
                store.update(record["id"], "DRAFT_READY", failure_count=0, failure_stage=None)
                prepared += 1
                log.event("INFO", "draft_ready", resource=folder["filename"], title=title,
                          format_label=format_label, has_passcode=bool(share_passcode))
                if args.publish:
                    attempt_id = store.begin_submission(run_id, record["id"], title)
                    try:
                        visible = hifini.submit_and_verify(title, artist, song)
                    except DuplicateTitleError:
                        store.reject_submission(attempt_id)
                        store.update(record["id"], "DRAFT_READY")
                        mp3_items = [item for item in folder_items if Path(str(item.get("filename") or "")).suffix.lower() == ".mp3"]
                        if "FLAC" not in format_label or not mp3_items:
                            raise
                        mp3_label = audio_format_label(mp3_items, measured_bitrates)
                        fallback_title = post_title(artist, song, mp3_label)
                        log.event("WARNING", "duplicate_title_fallback", original_title=title,
                                  fallback_title=fallback_title, shared_files=len(mp3_items))
                        share_url, share_passcode = quark.public_share(
                            [str(item["fid"]) for item in mp3_items], fallback_title,
                        )
                        store.update(record["id"], "DRAFT_READY", share_url=share_url,
                                     share_passcode=share_passcode,
                                     detail="原始标题已占用，已切换为仅含 MP3 的分享")
                        hifini.draft(fallback_title, artist, category, lyrics, share_url, share_passcode)
                        durable.save_draft(run_id, folder["filename"], fallback_title, category, lyrics, share_url,
                                           share_passcode, mp3_items, measured_bitrates, False)
                        attempt_id = store.begin_submission(run_id, record["id"], fallback_title)
                        try:
                            visible = hifini.submit_and_verify(fallback_title, artist, song)
                        except DuplicateTitleError:
                            store.reject_submission(attempt_id)
                            raise
                        title = fallback_title
                    # Immediate success is only “visible in 我的帖子”; audit outcome is
                    # intentionally deferred until a later task because it needs time.
                    store.mark_submitted(record["id"], visible["url"])
                    log.event("INFO", "post_visible", title=title, pending_marker=visible["pending"])
                break
            except NoLyricsError as exc:
                store.update(record["id"], "SKIPPED_NO_LYRICS", detail=str(exc))
                store.event(run_id, "WARNING", "resource_skipped_no_lyrics", str(exc), record["id"])
                log.event("WARNING", "resource_skipped", resource=folder["filename"], reason="no_lyrics")
                continue
            except LyricsLookupError as exc:
                outcome = "PARTIAL_FAILURE"
                failures = store.fail(record["id"], str(exc), "lyrics")
                terminal = failures >= 3
                if terminal: store.update(record["id"], "SKIPPED_BAIKE_LOOKUP", detail=f"连续失败 {failures} 次：{exc}")
                store.event(run_id, "WARNING", "lyrics_lookup_failed", str(exc), record["id"])
                log.event("WARNING", "resource_skipped", resource=folder["filename"],
                          reason="lyrics_lookup_failed", failures=failures, terminal=terminal)
                continue
            except BitrateProbeError as exc:
                outcome = "PARTIAL_FAILURE"
                failures = store.fail(record["id"], str(exc), "bitrate")
                terminal = failures >= 3
                if terminal: store.update(record["id"], "SKIPPED_BITRATE_FAILURE", detail=f"连续失败 {failures} 次：{exc}")
                store.event(run_id, "WARNING", "bitrate_probe_failed", str(exc), record["id"])
                notify.alert("MP3 码率确认失败", f"{folder['filename']}：{exc}（失败 {failures}/3 次）\n" +
                             ("已停止自动处理。" if terminal else "本次跳过，下次任务重试。"))
                log.event("WARNING", "resource_skipped", resource=folder["filename"],
                          reason="bitrate_probe_failed", failures=failures, terminal=terminal)
                continue
            except SubmissionRejectedError as exc:
                if args.publish:
                    durable.db.execute("UPDATE submission_attempts SET status='REJECTED',detail=?,ended_at=CURRENT_TIMESTAMP WHERE run_id=? AND resource_id=? AND status='UNKNOWN'", (redact(str(exc)), run_id, record['id']))
                    durable.db.commit()
                store.fail(record['id'], str(exc), 'submission_rejected')
                store.update(record['id'], 'SKIPPED_MANUAL_REVIEW', detail=str(exc))
                log.event('ERROR', 'resource_failed', resource_id=record['id'], error=str(exc))
                notify.alert('发布被网站拒绝', f"{folder['filename']}：{exc}")
                outcome = 'PARTIAL_FAILURE'
                break
            except DuplicateTitleError as exc:
                store.update(record["id"], "SKIPPED_TITLE_CONFLICT", detail=str(exc))
                store.event(run_id, "WARNING", "title_conflict_skipped", str(exc), record["id"])
                notify.alert("HiFiNi 标题重复，素材已停止发布", f"{folder['filename']}：{exc}\n已永久跳过。")
                log.event("WARNING", "resource_skipped", resource=folder["filename"], reason="title_conflict")
                continue
            except Exception as exc:
                unknown = store.db.execute("SELECT 1 FROM submission_attempts WHERE resource_id=? AND status='UNKNOWN'", (record["id"],)).fetchone()
                if unknown:
                    log.event("ERROR", "submission_unknown", resource_id=record["id"], error=str(exc), traceback=traceback.format_exc())
                    notify.alert("提交结果待核实", f"{folder['filename']}：{exc}\n已阻止自动重发，请核对我的帖子。")
                    outcome = "FAILED"; break
                failures = store.fail(record["id"], str(exc))
                if failures >= MAX_FAILURES: store.update(record["id"], "SKIPPED_TOO_MANY_FAILURES")
                log.event("ERROR", "resource_failed", resource_id=record["id"], failures=failures, error=str(exc), traceback=traceback.format_exc())
                notify.alert("素材处理失败", f"{folder['filename']}：{exc}（失败 {failures} 次）")
                outcome = "PARTIAL_FAILURE"
        durable.finish_run(run_id, outcome, f"扫描 {len(folders)} 项，生成草稿 {prepared} 篇")
        log.event("INFO" if outcome == "SUCCESS" else "ERROR", "run_finished", status=outcome, scanned=len(folders), drafts=prepared)
        return 0 if outcome == "SUCCESS" else 1
    except KeyboardInterrupt:
        durable.finish_run(run_id, "ABORTED", "用户中断")
        log.event("WARNING", "run_aborted")
        return 130
    except Exception as exc:
        durable.finish_run(run_id, "FAILED", redact(str(exc)))
        log.event("ERROR", "run_finished", status="FAILED", error=str(exc), traceback=traceback.format_exc())
        notify.alert("任务异常", str(exc))
        return 1
    finally:
        if hifini:
            try: hifini.close(keep_open=not args.publish)
            except Exception as exc: log.event("WARNING", "browser_cleanup_failed", error=str(exc))


def test_feishu() -> int:
    with RunLock():
        settings = Settings.load(); store = Store(); run_id = uuid.uuid4().hex
        try:
            store.start_run(run_id, "test_feishu")
            log = JsonLogger(run_id, store)
            delivered = Feishu(settings, log).alert("告警通道测试", "Quark-HiFiNi 飞书告警通道测试。")
            store.finish_run(run_id, "SUCCESS" if delivered else "FAILED")
            return 0 if delivered else 1
        finally: store.close()


def check_local() -> int:
    """Local-only migration and health report; never logs in or sends notifications."""
    with RunLock():
        settings = Settings.load(); store = Store()
        try:
            imported = store.import_logs()
            recovered = store.recover_interrupted()
            integrity = store.db.execute("PRAGMA quick_check").fetchone()[0]
            report = {
                "database": integrity, "imported_logs": imported, "recovered_runs": recovered,
                "dependencies": {name: bool(shutil.which(name)) for name in ("node", curl_binary())},
                "ffprobe": ffprobe_path(),
                "uploads": dict(store.db.execute("SELECT status,COUNT(*) FROM uploads GROUP BY status").fetchall()),
                "git_bash": settings.git_bash.is_file(),
                "bash": settings.git_bash.is_file(),
                "quark_cli": (settings.quark_dir / "scripts" / "quark-drive.cjs").is_file(),
                "alerts_configured": bool(settings.feishu_webhook),
                "typesafe": {"active": settings.typesafe.provider == 'typesafe', "mode": settings.typesafe.mode,
                             "configured": settings.typesafe.provider == 'typesafe' and bool(settings.typesafe.api_key),
                             "model": settings.typesafe.model, "max_calls_per_run": settings.typesafe.max_calls,
                             "judgments": dict(store.db.execute("SELECT status,COUNT(*) FROM ai_judgments GROUP BY status").fetchall())},
                "ai": {"provider": settings.typesafe.provider, "mode": settings.typesafe.mode,
                       "model": settings.typesafe.model, "configured": bool(settings.typesafe.api_url) if settings.typesafe.provider == 'compatible' else bool(settings.typesafe.api_key),
                       "timeout": settings.typesafe.timeout, "max_calls_per_run": settings.typesafe.max_calls},
                "resources": dict(store.db.execute("SELECT status,COUNT(*) FROM resources GROUP BY status").fetchall()),
                "runs": dict(store.db.execute("SELECT status,COUNT(*) FROM runs GROUP BY status").fetchall()),
                "unresolved_submissions": store.db.execute("SELECT COUNT(*) FROM submission_attempts WHERE status='UNKNOWN'").fetchone()[0],
                "unresolved_attempt_ids": [row[0] for row in store.db.execute("SELECT id FROM submission_attempts WHERE status='UNKNOWN'")],
                "pending_alerts": store.db.execute("SELECT COUNT(*) FROM alerts WHERE status IN ('FAILED','UNKNOWN','NOT_CONFIGURED')").fetchone()[0],
            }
            print(json.dumps(report, ensure_ascii=True, indent=2))
            return 0 if integrity == "ok" and all(report["dependencies"].values()) and report["git_bash"] and report["quark_cli"] and not report["unresolved_submissions"] else 1
        finally: store.close()


def test_ai() -> int:
    """One real evidence-only AI request; no cloud-drive or forum actions."""
    with RunLock():
        settings = Settings.load()
        if settings.typesafe.mode == 'off': raise FlowError('AI_MODE 已关闭')
        store = Store()
        try:
            run_id = uuid.uuid4().hex
            store.start_run(run_id, 'ai_test')
            log = JsonLogger(run_id, store)
            reviewer = ShadowReviewer(settings.typesafe, log, redact, use_cache=False)
            reviewer.versions('Test Artist - Test Song', [{'filename': 'Test Artist - Test Song.mp3'},
                              {'filename': 'Test Artist - Test Song (Live).mp3'}], None)
            row = store.db.execute('SELECT status,response_model,error,elapsed_ms,input_tokens,output_tokens FROM ai_judgments WHERE run_id=? ORDER BY id DESC LIMIT 1', (run_id,)).fetchone()
            success = bool(row and row['status'] in ('SUCCESS', 'CACHED'))
            result = {'run_id': run_id, 'provider': settings.typesafe.provider, 'requested_model': settings.typesafe.model,
                      **(dict(row) if row else {'status': 'NO_RESULT'})}
            store.finish_run(run_id, 'SUCCESS' if success else 'FAILED', json.dumps(redact(result), ensure_ascii=False))
            print(json.dumps(redact(result), ensure_ascii=False))
            return 0 if success else 1
        finally: store.close()


def resolve_submission(attempt_id: int, url: str | None) -> int:
    """Record an operator's explicit decision; this never posts anything."""
    if url and (urlparse(url).scheme != "https" or urlparse(url).hostname != "www.hifini.net"
                or not re.fullmatch(r"/thread-\d+\.htm", urlparse(url).path)):
        raise FlowError("请提供 HiFiNi 主题的完整 HTTPS 地址")
    with RunLock():
        store = Store()
        try:
            row = store.db.execute("SELECT * FROM submission_attempts WHERE id=? AND status='UNKNOWN'", (attempt_id,)).fetchone()
            if not row: raise FlowError("该提交记录不存在或已处理")
            run_id = uuid.uuid4().hex
            store.start_run(run_id, "resolve_submission")
            if url:
                store.mark_submitted(row["resource_id"], url)
            else:
                with store.db:
                    store.db.execute("UPDATE submission_attempts SET status='NOT_SUBMITTED', detail='人工确认未提交', ended_at=CURRENT_TIMESTAMP WHERE id=?", (attempt_id,))
                    store.db.execute("UPDATE resources SET status='DRAFT_READY', detail='人工确认未提交，可重新准备草稿' WHERE id=?", (row["resource_id"],))
            store.event(run_id, "WARNING", "submission_manually_resolved", json.dumps({"attempt_id": attempt_id, "submitted": bool(url)}), row["resource_id"])
            store.finish_run(run_id, "SUCCESS")
            print("提交核实结果已记录；未执行发布。")
            return 0
        finally: store.close()


def retry_alert(alert_id: int) -> int:
    """Explicit recovery for proven non-delivery; never retries UNKNOWN or summaries."""
    with RunLock():
        store = Store()
        try:
            row = store.db.execute('SELECT * FROM alerts WHERE id=?', (alert_id,)).fetchone()
            if not row or not notifications.retryable(row): raise FlowError('无法证明旧消息未发送，禁止补发')
            if row['title'] == '每日总结': raise FlowError('每日总结请使用 daily-summary --send --retry-undelivered，以保留日期回执保护')
            settings = Settings.load()
            if not settings.feishu_webhook: raise FlowError('请先配置 FEISHU_WEBHOOK')
            run_id = uuid.uuid4().hex
            store.start_run(run_id, 'notification_retry')
            store.db.execute("UPDATE alerts SET status='RETRIED',updated_at=CURRENT_TIMESTAMP WHERE id=?", (alert_id,)); store.db.commit()
            log = JsonLogger(run_id, store)
            log.event('INFO', 'notification_manually_retried', original_alert_id=alert_id)
            sent = Feishu(settings, log).alert(row['title'], row['detail'], max_attempts=1)
            store.finish_run(run_id, 'SUCCESS' if sent else 'FAILED', f'原通知 {alert_id}')
            return 0 if sent else 1
        finally: store.close()


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quark → HiFiNi publisher (safe by default)")
    sub = parser.add_subparsers(dest="command", required=True); cmd = sub.add_parser("run", help="scan and prepare at most one draft")
    cmd.add_argument("--dry-run", action="store_true", help="explicit safe mode; this is also the default")
    cmd.add_argument("--publish", action="store_true", help="reserved for an explicitly confirmed production submission")
    cmd.add_argument("--session-input", help=argparse.SUPPRESS)
    cmd.add_argument("--session-id", help=argparse.SUPPRESS)
    upload = sub.add_parser("upload", help="scan upload-files and upload missing songs; never publishes")
    upload_mode = upload.add_mutually_exclusive_group()
    upload_mode.add_argument("--apply", action="store_true", help="upload missing songs to Share/Media")
    upload_mode.add_argument("--dry-run", action="store_true", help="preview only (default)")
    upload.add_argument("--session-input", help=argparse.SUPPRESS)
    upload.add_argument("--session-id", help=argparse.SUPPRESS)
    upload.add_argument("--slot-date", help="authorized task date YYYY-MM-DD; prevents duplicate slot execution")
    sub.add_parser("test-feishu", help="send one Feishu configuration test")
    sub.add_parser('test-ai', help='test the configured AI with one real request; no upload or publication')
    sub.add_parser("check", help="local health check and import legacy logs; no network calls")
    retry = sub.add_parser('retry-alert', help='explicitly retry one proven unsent notification; never UNKNOWN')
    retry.add_argument('alert_id', type=int)
    report = sub.add_parser("daily-summary", help="preview combined daily summary; --send sends once per date")
    report.add_argument("--date", help="Beijing date YYYY-MM-DD (default: today)")
    report.add_argument("--send", action="store_true")
    report.add_argument("--retry-undelivered", action="store_true", help="explicitly retry a proven pre-connection failure once")
    resolve = sub.add_parser("resolve-submission", help="record a manually verified uncertain submission; never posts")
    resolve.add_argument("attempt_id", type=int)
    decision = resolve.add_mutually_exclusive_group(required=True)
    decision.add_argument("--url", help="confirmed existing HiFiNi thread URL")
    decision.add_argument("--not-submitted", action="store_true", help="operator has verified the post was never submitted")
    ns = parser.parse_args(argv)
    try:
        if ns.command == "run":
            if ns.dry_run and ns.publish: parser.error("--dry-run and --publish cannot be used together")
            return run(ns)
        if ns.command == "check": return check_local()
        if ns.command == 'test-ai': return test_ai()
        if ns.command == 'retry-alert': return retry_alert(ns.alert_id)
        if ns.command == "daily-summary":
            from daily_summary import run_summary
            return run_summary(ns)
        if ns.command == "upload":
            from uploader import run_upload
            return run_upload(ns)
        if ns.command == "resolve-submission": return resolve_submission(ns.attempt_id, ns.url)
        return test_feishu()
    except (OSError, sqlite3.Error, FlowError) as exc:
        print(f"操作失败：{redact(str(exc))}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("操作已中断。", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(cli())
