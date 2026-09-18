from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import websocket
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
USER_AGENT = "QuarkHiFiNi/1.0"
DAILY_LIMIT = 3


class FlowError(RuntimeError):
    """An expected operational failure that should be logged and alerted."""


@dataclass(frozen=True)
class Settings:
    cdp_url: str
    quark_dir: Path
    git_bash: Path
    feishu_webhook: str | None
    feishu_secret: str | None
    hifini_username: str | None
    hifini_password: str | None

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        return cls(
            cdp_url=os.getenv("HIFINI_CDP_URL", "http://127.0.0.1:9501").rstrip("/"),
            quark_dir=Path(os.getenv("QUARK_SKILL_DIR", r"C:\Users\admin\.agents\skills\quarkclouddrive")),
            git_bash=Path(os.getenv("GIT_BASH", r"D:\Git\bin\bash.exe")),
            feishu_webhook=os.getenv("FEISHU_WEBHOOK") or None,
            feishu_secret=os.getenv("FEISHU_SECRET") or None,
            hifini_username=os.getenv("HIFINI_USERNAME") or None,
            hifini_password=os.getenv("HIFINI_PASSWORD") or None,
        )


class JsonLogger:
    def __init__(self, run_id: str) -> None:
        path = ROOT / "logs" / f"{datetime.now():%Y-%m-%d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.run_id = path, run_id

    def event(self, level: str, name: str, **data: Any) -> None:
        row = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "run_id": self.run_id,
               "level": level, "event": name, **data}
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


class Store:
    def __init__(self) -> None:
        path = ROOT / "data" / "quark-hifini.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
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
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(resources)")}
        for name, definition in {
            "submitted_at": "TEXT",
            "audit_status": "TEXT",
            "audit_checked_at": "TEXT",
            "audit_alerted_at": "TEXT",
        }.items():
            if name not in columns: self.db.execute(f"ALTER TABLE resources ADD COLUMN {name} {definition}")
        self._deduplicate_test_records()
        self.db.commit()

    def _deduplicate_test_records(self) -> None:
        # Folder names are unique in Share/Media. Keep prior test rows for audit
        # history but prevent them from acting as another live resource.
        names = self.db.execute("SELECT folder_name FROM resources GROUP BY folder_name HAVING COUNT(*) > 1").fetchall()
        priority = {"PUBLISHED": 5, "PENDING_REVIEW": 4, "SUBMITTED": 3, "DRAFT_READY": 2}
        for (name,) in names:
            rows = self.db.execute("SELECT id,status FROM resources WHERE folder_name=? AND status!='SUPERSEDED'", (name,)).fetchall()
            if len(rows) < 2: continue
            keeper = max(rows, key=lambda row: (priority.get(row["status"], 1), -row["id"]))
            for row in rows:
                if row["id"] != keeper["id"]:
                    self.db.execute("UPDATE resources SET status='SUPERSEDED', detail='同名测试记录已合并' WHERE id=?", (row["id"],))

    def start_run(self, run_id: str, mode: str) -> None:
        self.db.execute("INSERT INTO runs(id, mode, status) VALUES(?,?,?)", (run_id, mode, "RUNNING")); self.db.commit()

    def finish_run(self, run_id: str, status: str, detail: str = "") -> None:
        self.db.execute("UPDATE runs SET status=?, detail=?, ended_at=CURRENT_TIMESTAMP WHERE id=?", (status, detail, run_id)); self.db.commit()

    def event(self, run_id: str, level: str, event_type: str, detail: str, resource_id: int | None = None) -> None:
        self.db.execute("INSERT INTO events(run_id,resource_id,level,event_type,detail) VALUES(?,?,?,?,?)", (run_id, resource_id, level, event_type, detail)); self.db.commit()

    def upsert_resource(self, item: dict[str, Any]) -> sqlite3.Row:
        # Quark's opaque FID contains a session-scoped capability suffix. The
        # directory name is the stable project identity inside Share/Media;
        # retain the latest FID only as this-run's operation handle.
        current = self.db.execute("SELECT * FROM resources WHERE folder_name=? AND status!='SUPERSEDED' ORDER BY id LIMIT 1", (item["filename"],)).fetchone()
        if current:
            self.db.execute("UPDATE resources SET folder_fid=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (item["fid"], current["id"]))
        else:
            self.db.execute("INSERT INTO resources(folder_fid,folder_name,status) VALUES(?,?, 'DISCOVERED')", (item["fid"], item["filename"]))
        self.db.commit()
        return self.db.execute("SELECT * FROM resources WHERE folder_name=? AND status!='SUPERSEDED' ORDER BY id LIMIT 1", (item["filename"],)).fetchone()

    def update(self, resource_id: int, status: str, **fields: Any) -> None:
        columns = ["status=?", "updated_at=CURRENT_TIMESTAMP"]; values: list[Any] = [status]
        for key, value in fields.items(): columns.append(f"{key}=?"); values.append(value)
        values.append(resource_id)
        self.db.execute(f"UPDATE resources SET {', '.join(columns)} WHERE id=?", values); self.db.commit()

    def fail(self, resource_id: int, detail: str) -> int:
        self.db.execute("UPDATE resources SET failure_count=failure_count+1, status='FAILED', detail=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (detail, resource_id)); self.db.commit()
        return int(self.db.execute("SELECT failure_count FROM resources WHERE id=?", (resource_id,)).fetchone()[0])

    def submitted_today(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM resources WHERE submitted_at IS NOT NULL AND date(submitted_at,'localtime')=date('now','localtime')").fetchone()[0])

    def pending_audits(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT *, CAST((julianday('now')-julianday(submitted_at))*86400 AS INTEGER) AS pending_seconds FROM resources WHERE status IN ('SUBMITTED','PENDING_REVIEW') AND hifini_url IS NOT NULL").fetchall()

    def mark_submitted(self, resource_id: int, url: str) -> None:
        self.db.execute("UPDATE resources SET status='SUBMITTED', hifini_url=?, submitted_at=CURRENT_TIMESTAMP, audit_status='UNVERIFIED', updated_at=CURRENT_TIMESTAMP WHERE id=?", (url, resource_id)); self.db.commit()

    def audit(self, resource_id: int, status: str, detail: str) -> None:
        state = {"PENDING_REVIEW": "PENDING_REVIEW", "APPROVED": "PUBLISHED", "REJECTED": "REJECTED"}[status]
        self.db.execute("UPDATE resources SET status=?, audit_status=?, submitted_at=COALESCE(submitted_at,CURRENT_TIMESTAMP), audit_checked_at=CURRENT_TIMESTAMP, detail=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (state, status, detail, resource_id)); self.db.commit()


class Feishu:
    def __init__(self, settings: Settings, log: JsonLogger) -> None: self.settings, self.log = settings, log

    def alert(self, title: str, detail: str) -> bool:
        if not self.settings.feishu_webhook:
            self.log.event("WARNING", "feishu_skipped", reason="not_configured", title=title); return False
        payload: dict[str, Any] = {"msg_type": "text", "content": {"text": f"【Quark-HiFiNi {title}】\n{detail}"}}
        if self.settings.feishu_secret:
            timestamp = str(int(time.time()))
            key = f"{timestamp}\n{self.settings.feishu_secret}".encode()
            payload.update(timestamp=timestamp, sign=base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode())
        try:
            response = requests.post(self.settings.feishu_webhook, json=payload, timeout=10); response.raise_for_status()
            self.log.event("INFO", "feishu_sent", title=title)
            return True
        except requests.RequestException as exc:
            self.log.event("ERROR", "feishu_failed", error=str(exc), title=title)
            return False


class Quark:
    def __init__(self, settings: Settings, log: JsonLogger) -> None: self.s, self.log = settings, log

    def _run(self, arguments: list[str]) -> list[dict[str, Any]]:
        install = self.s.quark_dir / "scripts" / "install.sh"; cli = self.s.quark_dir / "scripts" / "quark-drive.cjs"
        if not self.s.git_bash.is_file() or not install.is_file() or not cli.is_file(): raise FlowError("夸克 Skill 或 Git Bash 缺失")
        check = subprocess.run([str(self.s.git_bash), str(install).replace('\\', '/')], cwd=self.s.quark_dir, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=90)
        if check.returncode: raise FlowError("夸克 Skill 环境自检失败")
        result = subprocess.run(["node", str(cli), *arguments], cwd=self.s.quark_dir, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=90)
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")]
        if result.returncode or not rows or any(row.get("code", 0) != 0 for row in rows if row.get("type") == "result"):
            raise FlowError(rows[-1].get("msg", "夸克命令失败") if rows else "夸克命令无输出")
        return rows

    def browse(self, parent_fid: str) -> list[dict[str, Any]]:
        rows = self._run(["browse", "--parent-fid", parent_fid, "--all"])
        artifact = next((r for r in rows if r.get("type") == "artifact"), None)
        if not artifact: raise FlowError("夸克浏览未返回完整结果")
        return [json.loads(line) for line in Path(artifact["data"]["file_path"]).read_text(encoding="utf-8").splitlines()]

    def media_folders(self) -> list[dict[str, Any]]:
        root = self.browse("0"); share = next((x for x in root if x.get("filename") == "Share" and x.get("category") == 0), None)
        if not share: raise FlowError("未找到 Share 目录")
        entries = self.browse(share["fid"]); media = next((x for x in entries if x.get("filename") == "Media" and x.get("category") == 0), None)
        if not media: raise FlowError("未找到 Share/Media 目录")
        return [x for x in self.browse(media["fid"]) if x.get("category") == 0]

    def public_share(self, folder_fid: str, title: str) -> str:
        row = next(r for r in self._run(["share", folder_fid, "--title", title, "--url-type", "1", "--expired-type", "1"]) if r.get("type") == "result")
        return str(row["data"]["share_url"])


class TaskTab:
    """One disposable CDP page for one task; no task opens additional tabs."""
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        target = requests.put(f"{endpoint}/json/new?about:blank", timeout=10).json()
        self.target_id = target["id"]
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], suppress_origin=True, timeout=15)
        self.ident = 0
        self.call("Page.enable")

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.ident += 1; self.ws.send(json.dumps({"id": self.ident, "method": method, "params": params or {}}))
        while True:
            result = json.loads(self.ws.recv())
            if result.get("id") == self.ident:
                if "error" in result: raise FlowError(result["error"].get("message", "CDP 调用失败"))
                return result["result"]

    def evaluate(self, expression: str) -> Any:
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in result: raise FlowError(result["exceptionDetails"].get("text", "网页脚本失败"))
        return result["result"].get("value")

    def go(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})
        time.sleep(2)

    def close(self, keep_open: bool) -> None:
        self.ws.close()
        if not keep_open:
            requests.get(f"{self.endpoint}/json/close/{self.target_id}", timeout=10)


class HiFiNi:
    def __init__(self, settings: Settings, log: JsonLogger) -> None:
        self.tab, self.log = TaskTab(settings.cdp_url), log

    def close(self, keep_open: bool) -> None: self.tab.close(keep_open)
    def _eval(self, url: str, expression: str) -> Any:
        self.tab.go(url); return self.tab.evaluate(expression)

    def my_threads(self) -> list[dict[str, Any]]:
        expr = """JSON.stringify({logged:!!document.querySelector('a[href*=logout],a[href*=user-logout]')||document.body.innerText.includes('退出'),items:[...document.querySelectorAll('.media-body')].map(x=>{const a=x.querySelector('.subject a');return a?{title:a.innerText.trim(),url:new URL(a.getAttribute('href'),location.href).href,pending:!!x.querySelector('i.icon-coffee')}:null}).filter(Boolean)})"""
        result = json.loads(self._eval("https://www.hifini.net/my-thread.htm", expr))
        if not result["logged"]: raise FlowError("HiFiNi 未登录")
        return result["items"]

    def duplicate(self, title: str, artist: str | None = None, song: str | None = None) -> dict[str, Any] | None:
        # HiFiNi users commonly append formats and netdisk labels, e.g.
        # 林家谦《四月物语》[FLAC] [夸客网盘], so exact title equality is unsafe.
        return next((item for item in self.my_threads() if item["title"].strip() == title.strip() or (artist and song and artist.casefold() in item["title"].casefold() and song.casefold() in item["title"].casefold())), None)

    def draft(self, title: str, artist: str, category: str, lyrics_html: str, share_url: str) -> None:
        self.tab.go("https://www.hifini.net/thread-create-0.htm")
        # The add-netdisk plugin is intentionally exercised exactly as the UI does:
        # set its hidden link fields, choose Reply-visible, then reassert two blank lines.
        category_id = {"华语": "1", "日韩": "2", "欧美": "3"}.get(category)
        if not category_id: raise FlowError(f"不支持自动选择的分区：{category}")
        expression = f"""(()=>{{const set=(s,v)=>{{const e=document.querySelector(s);if(!e)throw Error(s);const proto=e instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(e,v);e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));}};set('#subject',{json.dumps(title)});set('#fox_tag',{json.dumps(artist)});set('select[name=fid]',{json.dumps(category_id)});const ed=tinymce.get('message');if(!ed)throw Error('编辑器未就绪');ed.setContent({json.dumps(lyrics_html)});ed.focus();ed.selection.select(ed.getBody(),true);ed.selection.collapse(false);const path=[...document.querySelectorAll('svg path')].find(x=>x.getAttribute('d')?.startsWith('M5 16h14'));if(!path)throw Error('未找到添加网盘按钮');path.closest('button').click();const setPlugin=(p,v)=>{{const x=[...document.querySelectorAll('input')].find(i=>i.placeholder===p);if(!x)throw Error('网盘字段缺失');const d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');d.set.call(x,v);x.dispatchEvent(new Event('input',{{bubbles:true}}));x.dispatchEvent(new Event('change',{{bubbles:true}}));}};setPlugin('请输入网盘下载链接',{json.dumps(share_url)});setPlugin('请输入提取码','');const reply=[...document.querySelectorAll('button')].find(x=>x.offsetParent!==null&&x.innerText.trim()==='回复可见');if(!reply)throw Error('未找到回复可见选项');reply.click();let c=ed.getContent();c=c.replace('<p>&nbsp;</p>\\n<h5>','<p>&nbsp;</p>\\n<p>&nbsp;</p>\\n<h5>');ed.setContent(c);ed.fire('change');const final=ed.getContent(),i=final.indexOf({json.dumps(share_url)});if(i<0||!/<p>&nbsp;<\\/p>\\n<p>&nbsp;<\\/p>\\n<h5>/.test(final.slice(0,i)))throw Error('正文布局校验失败');return 'draft-ready'}})()"""
        self.tab.evaluate(expression)

    def submit_and_verify(self, title: str, artist: str, song: str) -> dict[str, Any]:
        # This is only called after the command line explicitly opts in to --publish.
        self.tab.evaluate("(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.offsetParent!==null&&x.innerText.trim()==='发表主题');if(!b)throw Error('未找到发表主题按钮');b.click();return true})()")
        result = None
        for wait in (2, 4, 8):
            time.sleep(wait); result = self.duplicate(title, artist, song)
            if result: break
        if not result: raise FlowError("提交后未在我的帖子中找到同名帖子")
        return result

    def audit_thread(self, url: str) -> str:
        text = str(self._eval(url, "document.body.innerText"))
        if "该主题正在由管理员审核中" in text: return "PENDING_REVIEW"
        if any(token in text for token in ("主题不存在", "审核未通过", "主题已被删除", "无权查看")): return "REJECTED"
        return "APPROVED"


def classify(artist: str, song: str) -> str:
    text = artist + song
    if re.search(r"[ぁ-んァ-ヶ]", text): return "日韩"
    return "华语" if sum("\u4e00" <= c <= "\u9fff" for c in text) >= 2 else "欧美"


def parse_folder(name: str) -> tuple[str, str] | None:
    if " x " in name.lower(): return None
    for separator in (" - ", "-"):
        if separator in name:
            left, right = (part.strip() for part in name.split(separator, 1))
            if left and right: return right, left  # Project convention: song-artist.
    return None


def lyrics_html(browser: HiFiNi, folder_name: str) -> tuple[str, str, str]:
    if " x " in folder_name.lower(): raise FlowError("混合曲目录，等待人工处理")
    if " - " in folder_name: left, right = (item.strip() for item in folder_name.split(" - ", 1))
    elif "-" in folder_name: left, right = (item.strip() for item in folder_name.split("-", 1))
    else: raise FlowError("目录名不含歌手/歌曲分隔符")
    for artist, song in ((right, left), (left, right)):
        search = json.loads(browser._eval(f"https://baike.baidu.com/search?word={quote(song + ' ' + artist)}", f"""JSON.stringify([...document.querySelectorAll('a')].map(a=>({{text:a.innerText.trim(),href:a.href}})).filter(a=>a.text.includes({json.dumps(song)})&&a.href.includes('/item/')).slice(0,1))"""))
        if not search: continue
        text = browser._eval(search[0]["href"], """(()=>{const h=[...document.querySelectorAll('h2')].find(x=>x.innerText.trim()==='歌曲歌词');return JSON.stringify({body:document.body.innerText,lyrics:h?.parentElement?.nextElementSibling?.innerText||''})})()""")
        page = json.loads(text); lyric_text = re.sub(r"\s*\[\d+\]\s*$", "", page["lyrics"]).strip()
        if artist not in page["body"] or len(lyric_text) < 100: continue
        rows = [f"<p>{escape(line)}</p>" if line.strip() else "<p><br></p>" for line in lyric_text.splitlines()]
        return artist, song, "".join(rows) + "<p>&nbsp;</p><p>&nbsp;</p>"
    raise FlowError("百度百科未找到同时匹配歌手、歌曲且含歌词章节的词条")


def wait_text(seconds: int | None) -> str:
    if not seconds or seconds < 60: return "不足 1 分钟"
    days, rest = divmod(seconds, 86400); hours, rest = divmod(rest, 3600); minutes = rest // 60
    return (f"{days} 天 {hours} 小时" if days else f"{hours} 小时 {minutes} 分钟")


def review_pending(store: Store, hifini: HiFiNi, notify: Feishu, log: JsonLogger, run_id: str) -> None:
    """Every pending item is reported each run; a pending review never blocks new work."""
    for item in store.pending_audits():
        try:
            status = hifini.audit_thread(item["hifini_url"])
            if status == "PENDING_REVIEW":
                store.audit(item["id"], status, "主题仍在管理员审核中")
                notify.alert("主题仍在审核中", f"{item['artist']} - {item['song']}\n等待：{wait_text(item['pending_seconds'])}\n{item['hifini_url']}")
                log.event("INFO", "audit_pending", resource=item["folder_name"], wait_seconds=item["pending_seconds"])
            elif status == "APPROVED":
                store.audit(item["id"], status, "主题页可正常访问，审核已通过")
                log.event("INFO", "audit_approved", resource=item["folder_name"])
            else:
                store.audit(item["id"], status, "主题页显示审核失败、删除或无权访问")
                notify.alert("主题审核失败", f"{item['artist']} - {item['song']}\n{item['hifini_url']}")
                log.event("ERROR", "audit_rejected", resource=item["folder_name"])
        except FlowError as exc:
            # Do not call an unavailable page a rejection: retain its prior state.
            store.event(run_id, "ERROR", "audit_check_failed", str(exc), item["id"])
            notify.alert("审核状态检查异常", f"{item['artist']} - {item['song']}：{exc}")
            log.event("ERROR", "audit_check_failed", resource=item["folder_name"], error=str(exc))


def run(args: argparse.Namespace) -> int:
    run_id = uuid.uuid4().hex; log = JsonLogger(run_id); store = Store(); settings = Settings.load(); notify = Feishu(settings, log)
    mode = "publish" if args.publish else "dry_run"; store.start_run(run_id, mode); log.event("INFO", "run_started", mode=mode)
    hifini: HiFiNi | None = None
    try:
        folders = Quark(settings, log).media_folders(); log.event("INFO", "media_scanned", count=len(folders))
        hifini = HiFiNi(settings, log)
        review_pending(store, hifini, notify, log, run_id)
        outcome = "SUCCESS"
        for folder in folders:
            record = store.upsert_resource(folder)
            if record["status"] in {"PUBLISHED", "PENDING_REVIEW", "SUBMITTED", "SKIPPED_TOO_MANY_FAILURES", "REJECTED"}: continue
            if record["failure_count"] > 3:
                store.update(record["id"], "SKIPPED_TOO_MANY_FAILURES", detail="失败次数超过 3 次"); continue
            if " x " in folder["filename"].lower():
                store.update(record["id"], "SKIPPED_MANUAL_REVIEW", detail="混合歌名，按规则跳过")
                log.event("WARNING", "resource_skipped", resource=folder["filename"], reason="mixed_title")
                continue
            try:
                artist, song, lyrics = lyrics_html(hifini, folder["filename"])
                title = f"{artist} - {song}"; duplicate = hifini.duplicate(title, artist, song)
                if duplicate:
                    store.update(record["id"], "PENDING_REVIEW" if duplicate["pending"] else "PUBLISHED", artist=artist, song=song, category=classify(artist, song), hifini_url=duplicate["url"], detail="HiFiNi 已存在同名帖子"); continue
                if args.publish and store.submitted_today() >= DAILY_LIMIT: raise FlowError("已达到每日 3 篇发布上限")
                share_url = record["share_url"] or Quark(settings, log).public_share(folder["fid"], title)
                category = classify(artist, song)
                store.update(record["id"], "DRAFT_READY", artist=artist, song=song, category=category, share_url=share_url)
                hifini.draft(title, artist, category, lyrics, share_url)
                log.event("INFO", "draft_ready", resource=folder["filename"], title=title)
                if args.publish:
                    visible = hifini.submit_and_verify(title, artist, song)
                    # Immediate success is only “visible in 我的帖子”; audit outcome is
                    # intentionally deferred until a later task because it needs time.
                    store.mark_submitted(record["id"], visible["url"])
                    log.event("INFO", "post_visible", title=title, pending_marker=visible["pending"])
                break
            except FlowError as exc:
                failures = store.fail(record["id"], str(exc)); store.event(run_id, "ERROR", "resource_failed", str(exc), record["id"]); notify.alert("素材处理失败", f"{folder['filename']}：{exc}（失败 {failures} 次）"); outcome = "FAILED"; break
        store.finish_run(run_id, outcome)
        log.event("INFO" if outcome == "SUCCESS" else "ERROR", "run_finished", status=outcome); return 0 if outcome == "SUCCESS" else 1
    except Exception as exc:
        store.finish_run(run_id, "FAILED", str(exc)); store.event(run_id, "ERROR", "run_failed", str(exc)); notify.alert("任务异常", str(exc)); log.event("ERROR", "run_finished", status="FAILED", error=str(exc)); return 1
    finally:
        if hifini:
            hifini.close(keep_open=not args.publish)


def test_feishu() -> int:
    run_id = uuid.uuid4().hex; log = JsonLogger(run_id); settings = Settings.load()
    if not settings.feishu_webhook or not settings.feishu_secret:
        log.event("ERROR", "feishu_test_failed", reason="missing_configuration")
        print("Feishu configuration is missing", file=sys.stderr); return 1
    delivered = Feishu(settings, log).alert("告警通道测试", "Quark-HiFiNi 独立项目的飞书告警配置已验证。")
    return 0 if delivered else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Quark → HiFiNi publisher (safe by default)")
    sub = parser.add_subparsers(dest="command", required=True); cmd = sub.add_parser("run", help="scan and prepare at most one draft")
    cmd.add_argument("--dry-run", action="store_true", help="explicit safe mode; this is also the default")
    cmd.add_argument("--publish", action="store_true", help="reserved for an explicitly confirmed production submission")
    sub.add_parser("test-feishu", help="send one Feishu configuration test")
    ns = parser.parse_args()
    if ns.command == "run":
        if ns.dry_run and ns.publish: parser.error("--dry-run and --publish cannot be used together")
        sys.exit(run(ns))
    sys.exit(test_feishu())
