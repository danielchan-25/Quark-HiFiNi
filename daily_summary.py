"""One combined Beijing-day report, built from durable local evidence."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path, PureWindowsPath
import sqlite3
import uuid

import main as app

BEIJING = timezone(timedelta(hours=8))


def song_label(artist: str | None, song: str | None, fallback: str = '') -> str:
    if artist and song:
        return f'《{song}》- {artist}'
    stem = PureWindowsPath(fallback).stem
    if ' - ' in stem:
        artist, song = stem.split(' - ', 1)
        return f'《{song}》- {artist}'
    return stem or '未识别歌曲'


def build_summary(db: sqlite3.Connection, day: date) -> str:
    """Reads only; never infers success from a run exit code or current resource status."""
    start = datetime.combine(day, time(), BEIJING).astimezone(timezone.utc)
    bounds = tuple(t.strftime('%Y-%m-%d %H:%M:%S') for t in (start, start + timedelta(days=1)))
    runs = [dict(r) for r in db.execute(
        "SELECT * FROM runs WHERE started_at>=? AND started_at<? AND mode IN ('publish','upload') ORDER BY started_at,id", bounds)]
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='task_slots'").fetchone():
        slots = {r['run_id']: r['slot_date'] for r in db.execute('SELECT run_id,slot_date FROM task_slots')}
        runs = [r for r in runs if r['id'] not in slots or slots[r['id']] == day.isoformat()]
        known = {r['id'] for r in runs}
        for row in db.execute("SELECT n.* FROM runs n JOIN task_slots s ON s.run_id=n.id WHERE s.slot_date=?", (day.isoformat(),)):
            if row['id'] not in known:
                runs.append(dict(row))
    publish_runs = [r for r in runs if r['mode'] == 'publish']
    upload_runs = [r for r in runs if r['mode'] == 'upload']
    attempts = [dict(r) for r in db.execute(
        "SELECT a.*,r.artist,r.song,r.folder_name FROM submission_attempts a JOIN resources r ON r.id=a.resource_id "
        "JOIN runs n ON n.id=a.run_id WHERE n.mode='publish' AND a.created_at>=? AND a.created_at<? ORDER BY a.id", bounds)]
    confirmed = {r['resource_id']: song_label(r['artist'], r['song'], r['folder_name'])
                 for r in attempts if r['status'] == 'CONFIRMED'}
    failures: dict[str, str] = {}
    event_types = ('lyrics_lookup_failed', 'bitrate_probe_failed', 'resource_skipped_no_lyrics',
                   'title_conflict_skipped', 'resource_failed', 'submission_unknown')
    for row in db.execute(
        "SELECT e.*,r.artist,r.song,r.folder_name FROM events e JOIN runs n ON n.id=e.run_id "
        "LEFT JOIN resources r ON r.id=e.resource_id WHERE n.mode='publish' AND e.created_at>=? AND e.created_at<? ORDER BY e.id", bounds):
        if row['event_type'] not in event_types:
            continue
        detail = row['detail']
        try:
            payload = json.loads(detail)
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        resource_id = row['resource_id'] or payload.get('resource_id')
        if resource_id in confirmed:
            continue
        resource = db.execute('SELECT artist,song,folder_name FROM resources WHERE id=?', (resource_id,)).fetchone()
        label = song_label(resource['artist'], resource['song'], resource['folder_name']) if resource else '发布任务'
        failures[label] = str(payload.get('error') or detail)
    unknown = [r for r in attempts if r['status'] in ('UNKNOWN', 'SUBMITTING')]
    for row in unknown:
        failures[song_label(row['artist'], row['song'], row['folder_name'])] = '提交结果未确认，禁止自动重发'
    for run in runs:
        if run['status'] in ('FAILED', 'ABORTED'):
            label = '发布任务' if run['mode'] == 'publish' else '上传任务'
            failures[label] = run['detail'] or '任务异常结束'

    upload_ids = [r['id'] for r in upload_runs]
    uploads = [dict(r) for r in db.execute(
        "SELECT * FROM uploads WHERE run_id IN (" + ','.join('?' for _ in upload_ids) + ") ORDER BY id", upload_ids)] if upload_ids else []
    uploaded = list(dict.fromkeys(song_label(None, None, r['local_path']) for r in uploads if r['status'] == 'UPLOADED'))
    latest_uploads = {r['local_path']: r for r in uploads}
    upload_failures = []
    for row in latest_uploads.values():
        if row['status'] in ('FAILED', 'UNKNOWN', 'UPLOADING', 'CHECKING'):
            label = song_label(None, None, row['local_path'])
            reason = row['detail'] or ('上传结果待核实' if row['status'] in ('UNKNOWN', 'UPLOADING') else '检查未完成')
            failures[label] = reason
            upload_failures.append(reason)

    if unknown:
        publication = '失败，失败原因：提交结果待核实'
    elif confirmed:
        publication = '成功，发布歌曲：' + '；'.join(confirmed.values())
    elif any(r['status'] == 'RUNNING' for r in publish_runs):
        publication = '进行中，尚无确认提交'
    elif not publish_runs:
        held = db.execute("SELECT a.title FROM submission_attempts a WHERE a.status='UNKNOWN' AND a.created_at<? LIMIT 1", (bounds[1],)).fetchone()
        publication = '受阻，失败原因：存在旧提交结果待核实（' + held['title'] + '）' if held else '未执行'
        if held:
            failures['发布任务'] = '旧提交结果待核实，已保留防重发保护：' + held['title']
    elif any(r['status'] in ('FAILED', 'ABORTED', 'PARTIAL_FAILURE') for r in publish_runs):
        publication = '失败，失败原因：' + (next(iter(failures.values()), '') or '未完成发布，详见任务记录')
    else:
        publication = '无新增发布（达到每日上限或无可用素材）'

    upload_failed = bool(upload_failures) or any(r['status'] in ('FAILED', 'ABORTED', 'PARTIAL_FAILURE') for r in upload_runs)
    if upload_failed:
        upload = '失败' + ('（部分成功），上传歌曲：' + '；'.join(uploaded) if uploaded else '，上传歌曲：无')
    elif any(r['status'] == 'RUNNING' for r in upload_runs):
        upload = '进行中，上传歌曲：' + ('；'.join(uploaded) or '暂无')
    elif uploaded:
        upload = '成功，上传歌曲：' + '；'.join(uploaded)
    elif upload_runs:
        upload = '成功，无新增上传（已有歌曲已跳过或无待上传文件）'
    else:
        upload = '未执行'
    clean = lambda value: ' '.join(str(app.redact(value)).splitlines())
    lines = ['【Quark-HiFiNi】', day.isoformat(), '', '发布任务：' + clean(publication),
             '上传任务：' + clean(upload), '失败任务：']
    lines.extend(f'- {clean(label)}，失败原因：{clean(reason)}' for label, reason in failures.items())
    if not failures:
        lines.append('- 无')
    return '\n'.join(lines)


def run_summary(args) -> int:
    try:
        day = date.fromisoformat(args.date) if args.date else datetime.now(BEIJING).date()
    except ValueError as exc:
        raise app.FlowError('日期须为 YYYY-MM-DD') from exc
    if day > datetime.now(BEIJING).date():
        raise app.FlowError('不能汇总未来日期')
    with app.RunLock():
        path = app.ROOT / 'data' / 'quark-hifini.db'
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN')
            message = build_summary(db, day)
        finally:
            db.close()
        if not args.send:
            print(message)
            return 0
        directory = app.ROOT / 'data' / 'daily-summaries'
        directory.mkdir(parents=True, exist_ok=True)
        receipt = directory / (day.isoformat() + '.json')
        retried_receipt = directory / (day.isoformat() + '.retry.json')
        if retried_receipt.exists():
            retried = json.loads(retried_receipt.read_text(encoding='utf-8'))
            print(json.dumps({'status': 'already_sent' if retried.get('status') == 'SENT' else 'needs_review',
                              'date': day.isoformat(), 'receipt_status': retried.get('status')}))
            return 0 if retried.get('status') == 'SENT' else 1
        if getattr(args, 'retry_undelivered', False):
            if not args.send or not receipt.exists():
                raise app.FlowError('补发仅用于有既存回执的日期')
            previous = json.loads(receipt.read_text(encoding='utf-8'))
            if previous.get('status') == 'SENT':
                print(json.dumps({'status': 'already_sent', 'date': day.isoformat()}))
                return 0
            # A DNS/bind/connect failure proves the webhook request was not sent.
            # Read timeouts, lost responses and interrupted attempts remain blocked.
            connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                evidence = connection.execute("SELECT last_error FROM alerts WHERE run_id=? AND title='每日总结' AND status='FAILED' AND attempts=1 ORDER BY id DESC LIMIT 1", (previous.get('run_id'),)).fetchone()
            finally:
                connection.close()
            import re
            if previous.get('status') != 'UNCONFIRMED' or not evidence or not re.search(r'Resolving timed out|Could not resolve host|Connection timed out|Failed to connect|bind failed|Failed binding', evidence[0] or '', re.I):
                raise app.FlowError('无法证明旧消息未发送，禁止补发')
            receipt = directory / (day.isoformat() + '.retry.json')
        # Exclusive creation survives process interruption; uncertain delivery never retries.
        record = {'date': day.isoformat(), 'status': 'SENDING', 'message': message}
        try:
            with receipt.open('x', encoding='utf-8') as stream:
                json.dump(record, stream, ensure_ascii=False, indent=2)
        except FileExistsError:
            previous = json.loads(receipt.read_text(encoding='utf-8'))
            print(json.dumps({'status': 'already_sent' if previous['status'] == 'SENT' else 'needs_review',
                              'date': day.isoformat(), 'receipt_status': previous['status']}))
            return 0 if previous['status'] == 'SENT' else 1
        store = app.Store()
        try:
            run_id = uuid.uuid4().hex
            store.start_run(run_id, 'daily_summary')
            log = app.JsonLogger(run_id, store)
            sent = app.Feishu(app.Settings.load(), log).alert('每日总结', message, raw_text=True, max_attempts=1)
            record.update(status='SENT' if sent else 'UNCONFIRMED', run_id=run_id)
            temp = receipt.with_suffix('.tmp')
            temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(receipt)
            store.finish_run(run_id, 'SUCCESS' if sent else 'FAILED', record['status'])
            print(json.dumps({'status': record['status'], 'date': day.isoformat()}))
            return 0 if sent else 1
        finally:
            store.close()
