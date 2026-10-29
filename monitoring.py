"""Local scheduler evidence and watchdog; never retries uploads or publication."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from dotenv import load_dotenv
import main as app

ROOT = Path(__file__).resolve().parent
BEIJING = timezone(timedelta(hours=8))
TASKS = {'daily': (6, 0, 45 * 60), 'summary': (23, 55, 3 * 60)}
GRACE = timedelta(minutes=10)


def now() -> datetime:
    return datetime.now(BEIJING)


def clean(value):
    value = app.redact(value)
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)(authorization[\s"\x27:=]+(?:bearer\s+)?)[^\s,"\x27]+', r'\1[REDACTED]', value)
        value = re.sub(r'(?i)(cookie[\s"\x27:=]+)[^\r\n]+', r'\1[REDACTED]', value)
    return value


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8') as stream:
            os.chmod(temp, 0o600)
            json.dump(clean(value), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read(path: Path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def monitor_dir(root: Path) -> Path:
    return root / 'data' / 'monitor'


def logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = logging.Logger(str(path))
    handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=2, encoding='utf-8')
    os.chmod(path, 0o600)
    handler.setFormatter(logging.Formatter('%(message)s'))
    result.addHandler(handler)
    return result


def emit(log: logging.Logger, event: str, **values) -> None:
    row = clean({'time': now().isoformat(timespec='seconds'), 'event': event, **values})
    text = json.dumps(row, ensure_ascii=False)
    log.info(text)
    print(text, flush=True)


def connection(root: Path):
    db = sqlite3.connect((root / 'data' / 'quark-hifini.db').resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def run_ids(root: Path) -> set[str]:
    db = connection(root)
    try:
        return {row[0] for row in db.execute('SELECT id FROM runs')}
    finally:
        db.close()


def evidence(root: Path, previous: set[str], task: str, day: str) -> dict:
    db = connection(root)
    try:
        db.execute('BEGIN')
        modes = ('publish', 'upload') if task == 'daily' else ('daily_summary',)
        runs = [dict(row) for row in db.execute('SELECT * FROM runs')
                if row['id'] not in previous and row['mode'] in modes]
        ids = [row['id'] for row in runs]
        result = {'runs': runs, 'attempts': [], 'uploads': {}, 'event_counts': []}
        if ids:
            placeholders = ','.join('?' for _ in ids)
            result['attempts'] = [dict(row) for row in db.execute(
                f'SELECT a.id,a.run_id,a.title,a.status,r.hifini_url FROM submission_attempts a '
                f'LEFT JOIN resources r ON r.id=a.resource_id WHERE a.run_id IN ({placeholders})', ids)]
            result['uploads'] = dict(db.execute(
                f'SELECT status,COUNT(*) FROM uploads WHERE run_id IN ({placeholders}) GROUP BY status', ids))
            result['event_counts'] = [dict(row) for row in db.execute(
                f"SELECT level,event_type,COUNT(*) AS count FROM events WHERE run_id IN ({placeholders}) "
                "AND level IN ('WARNING','ERROR') GROUP BY level,event_type", ids)]
        if task == 'summary':
            directory = root / 'data' / 'daily-summaries'
            receipt = directory / (day + '.retry.json')
            if not receipt.exists():
                receipt = directory / (day + '.json')
            result['receipt_status'] = (read(receipt) or {}).get('status', 'MISSING')
        return clean(result)
    finally:
        db.close()


def outcome(task: str, code: int, facts: dict) -> str:
    if facts.get('evidence_error'):
        return 'FAILED'
    statuses = Counter(row['status'] for row in facts.get('attempts', []))
    if statuses['UNKNOWN'] or statuses['SUBMITTING']:
        return 'UNKNOWN'
    if task == 'summary':
        return 'SUCCESS' if code == 0 and facts.get('receipt_status') == 'SENT' else 'FAILED'
    runs = facts.get('runs', [])
    if not runs or any(row['status'] in ('FAILED', 'ABORTED', 'RUNNING') for row in runs):
        return 'FAILED'
    uploads_ok = all(row['status'] == 'SUCCESS' for row in runs if row['mode'] == 'upload')
    if code == 0:
        return 'SUCCESS' if uploads_ok else 'FAILED'
    # Confirmed submission is evidence, even if other candidates failed lyrics lookup.
    if statuses['CONFIRMED'] and uploads_ok and any(row['mode'] == 'publish' for row in runs):
        return 'PARTIAL_FAILURE'
    return 'FAILED'


def run_task(root: Path, task: str, command: list[str] | None = None) -> int:
    started = now()
    day = started.date().isoformat()
    identifier = started.strftime('%H%M%S') + '-' + uuid.uuid4().hex[:10]
    directory = monitor_dir(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (directory / (task + '.lock')).open('a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        print(json.dumps({'status': 'already_running', 'task': task}))
        return 1
    path = directory / 'runs' / task / day / (identifier + '.json')
    log_path = root / 'logs' / 'monitor' / task / day / (identifier + '.log')
    log = logger(log_path)
    record = {'task': task, 'date': day, 'id': identifier, 'started_at': started.isoformat(),
              'pid': os.getpid(), 'state': 'RUNNING', 'log_path': str(log_path), 'record_path': str(path),
              'systemd_invocation_id': os.getenv('INVOCATION_ID'), 'last_output_at': None}
    process = None
    code = 1
    before = None
    try:
        save(path, record)
        emit(log, 'monitored_task_started', task=task, id=identifier)
        before = run_ids(root)
        if command is None:
            command = (['bash', str(root / 'scripts' / 'daily-run.sh'), '--execute'] if task == 'daily' else
                       ['bash', str(root / 'scripts' / 'run.sh'), 'daily-summary', '--send'])
        process = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', errors='replace', bufsize=1,
                                   env={**os.environ, 'PYTHONUNBUFFERED': '1'}, start_new_session=True)
        record['child_pid'] = process.pid
        save(path, record)
        for line in process.stdout:
            line = line.rstrip('\r\n')
            try:
                payload = json.loads(line)
                line = json.dumps(clean(payload), ensure_ascii=False)
                if isinstance(payload, dict):
                    record['last_event'] = payload.get('event', record.get('last_event'))
                    if payload.get('event') == 'workflow_stage_started':
                        record['stage'] = payload.get('stage', 'unknown')
                        record['stage_started_at'] = payload.get('time', now().isoformat())
            except ValueError:
                line = clean(line)
            # Keep stdout/stderr available even when application initialization fails.
            log.info('%s %s', now().isoformat(timespec='seconds'), line[:32768])
            print(line[:32768], flush=True)
            record['last_output_at'] = now().isoformat()
            save(path, record)
        code = process.wait()
    except (OSError, sqlite3.Error, ValueError, KeyboardInterrupt, InterruptedError) as exc:
        record['error'] = clean(f'{type(exc).__name__}: {exc}')
        emit(log, 'monitored_task_error', error=record['error'])
        code = 130 if isinstance(exc, (KeyboardInterrupt, InterruptedError)) else 1
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        if process is not None and process.stdout is not None:
            process.stdout.close()
        try:
            record['evidence'] = evidence(root, before, task, day) if before is not None else {'evidence_error': '无法读取执行前数据库快照'}
        except (OSError, sqlite3.Error, ValueError) as exc:
            record['evidence'] = {'evidence_error': clean(f'{type(exc).__name__}: {exc}')}
        record.update(state='FINISHED', ended_at=now().isoformat(), exit_code=code,
                      duration_seconds=round((now() - started).total_seconds(), 3))
        record['outcome'] = 'FAILED' if record.get('error') or code < 0 or code == 130 else outcome(task, code, record['evidence'])
        save(path, record)
        emit(log, 'monitored_task_finished', task=task, outcome=record['outcome'], exit_code=code,
             duration_seconds=record['duration_seconds'], record_path=str(path))
        for handler in log.handlers:
            handler.close()
        lock.close()
    return code


def units() -> dict:
    names = [f'quark-hifini-{task}.{kind}' for task in TASKS for kind in ('timer', 'service')]
    result = subprocess.run(['systemctl', '--user', 'show', *names, '-p', 'Id', '-p', 'ActiveState',
                             '-p', 'UnitFileState', '-p', 'Result'], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError('systemctl --user show failed')
    values = {}
    for section in result.stdout.strip().split('\n\n'):
        row = dict(line.split('=', 1) for line in section.splitlines() if '=' in line)
        if row.get('Id'):
            values[row['Id']] = row
    return values


def inspect(root: Path, at: datetime, config: dict, services: dict) -> tuple[list[dict], list[dict]]:
    problems, warnings = [], []
    enabled = datetime.fromisoformat(config['enabled_at'])

    def problem(key, detail, **extra):
        problems.append({'key': key, 'detail': detail, **extra})

    for task, (hour, minute, maximum) in TASKS.items():
        timer = services.get(f'quark-hifini-{task}.timer', {})
        if timer.get('ActiveState') != 'active' or timer.get('UnitFileState') != 'enabled':
            problem(f'{at.date()}:{task}:timer', f'{task} 定时器未启用或未处于 active 状态')
        service_active = services.get(f'quark-hifini-{task}.service', {}).get('ActiveState') in ('active', 'activating', 'deactivating')
        for day in (at.date() - timedelta(days=1), at.date()):
            due = datetime.combine(day, datetime.min.time(), BEIJING).replace(hour=hour, minute=minute)
            if due < enabled or at < due:
                continue
            records = [read(path) for path in sorted((monitor_dir(root) / 'runs' / task / day.isoformat()).glob('*.json'))]
            records = [row for row in records if datetime.fromisoformat(row['started_at']) >= due]
            if not records:
                if at >= due + GRACE:
                    problem(f'{day}:{task}:missing', f'{task} 应于 {due.isoformat()} 启动，超过 10 分钟仍无运行记录')
                continue
            row = max(records, key=lambda r: r['started_at'])
            elapsed = (at - datetime.fromisoformat(row['started_at'])).total_seconds()
            if row['state'] == 'RUNNING':
                if not service_active:
                    problem(f'{day}:{task}:interrupted', f'{task} 记录未结束且 systemd 服务已停止', record=row)
                elif elapsed > maximum:
                    problem(f'{day}:{task}:slow', f'{task} 运行超过 {maximum // 60} 分钟', record=row)
            elif row['outcome'] in ('FAILED', 'UNKNOWN'):
                problem(f'{day}:{task}:failed', f'{task} 执行异常：{row["outcome"]}，退出码 {row["exit_code"]}', record=row)
            elif row['outcome'] == 'PARTIAL_FAILURE':
                warnings.append({'task': task, 'date': str(day), 'detail': '已有确认提交，部分候选处理失败', 'record': row})
    db = connection(root)
    try:
        integrity = db.execute('PRAGMA quick_check(1)').fetchone()[0]
        if integrity != 'ok':
            problem(f'{at.date()}:database', '数据库 quick_check 异常：' + integrity)
        unknown = db.execute("SELECT a.id FROM submission_attempts a JOIN runs r ON r.id=a.run_id "
                             "WHERE a.status IN ('UNKNOWN','SUBMITTING') AND r.status!='RUNNING'").fetchall()
        if unknown:
            problem(f'{at.date()}:unknown-submission', '存在未确认提交，保留防重发保护', attempt_ids=[row[0] for row in unknown])
    finally:
        db.close()
    if shutil.disk_usage(root).free < 1024 ** 3:
        problem(f'{at.date()}:disk', '项目磁盘可用空间少于 1 GiB')
    return problems, warnings


def notify(detail: str) -> str:
    """One delivery attempt; unknown delivery is retained without automatic replay."""
    webhook = os.getenv('FEISHU_WEBHOOK')
    if not webhook:
        return 'NOT_CONFIGURED'
    payload = {'msg_type': 'text', 'content': {'text': clean('【Quark-HiFiNi 定时任务监测】\n' + detail)}}
    secret = os.getenv('FEISHU_SECRET')
    if secret:
        stamp = str(int(time.time()))
        key = f'{stamp}\n{secret}'.encode()
        payload.update(timestamp=stamp, sign=base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode())
    try:
        result = app.execute([app.curl_binary(), '--noproxy', '*', '-4', '-fsS', '--connect-timeout', '10',
                              '--max-time', '30', '-H', 'Content-Type: application/json', '--data-binary', '@-', webhook],
                             input=json.dumps(payload, ensure_ascii=False), timeout=35, operation='监测告警')
        response = json.loads(result.stdout) if result.returncode == 0 else {}
        return 'SENT' if isinstance(response, dict) and response.get('code', response.get('StatusCode')) in (0, '0') else 'UNCONFIRMED'
    except (OSError, ValueError, app.FlowError):
        return 'UNCONFIRMED'


def prune(root: Path, at: datetime) -> None:
    for base, days, pattern in [(root / 'logs' / 'monitor', 30, '*.log*'),
                                (monitor_dir(root) / 'runs', 90, '*.json')]:
        for path in base.rglob(pattern):
            if path.is_file() and at.timestamp() - path.stat().st_mtime > days * 86400:
                path.unlink()


def check(root: Path, send: bool = False) -> int:
    directory = monitor_dir(root)
    config = read(directory / 'config.json')
    if not config:
        raise ValueError('Run monitor.sh init before enabling the watchdog')
    with (directory / 'check.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        at = now()
        log = logger(root / 'logs' / 'monitor' / 'watchdog.log')
        try:
            try:
                problems, warnings = inspect(root, at, config, units())
            except (OSError, sqlite3.Error, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
                problems = [{'key': f'{at.date()}:monitor-error', 'detail': clean(f'巡检异常：{type(exc).__name__}: {exc}')}]
                warnings = []
            history = read(directory / 'incidents.json', {})
            keys = {row['key'] for row in problems}
            for key, row in history.items():
                if key not in keys and not row.get('resolved_at'):
                    row['resolved_at'] = at.isoformat()
            for issue in problems:
                key = issue['key']
                if key not in history:
                    history[key] = {'first_seen_at': at.isoformat(), 'detail': issue['detail']}
                incident = history[key]
                incident.pop('resolved_at', None)
                if send and not incident.get('notification'):
                    # Persist the claim before network I/O, including ambiguous crashes.
                    incident['notification'] = 'SENDING'
                    save(directory / 'incidents.json', history)
                    row = issue.get('record', {})
                    detail = issue['detail'] + '\n' + str(row.get('log_path', root / 'logs' / 'monitor' / 'watchdog.log'))
                    if row:
                        attempts = Counter(item['status'] for item in row.get('evidence', {}).get('attempts', []))
                        detail += f'\n开始：{row["started_at"]}，阶段：{row.get("stage", "初始化")}，最后事件：{row.get("last_event")}'
                        detail += f'\n本次确认提交 {attempts["CONFIRMED"]} 篇，未确认 {attempts["UNKNOWN"] + attempts["SUBMITTING"]} 篇。'
                    detail += '\n请核对实际提交记录后处理，监测不会自动重跑发布。'
                    incident['notification'] = notify(detail)
                    emit(log, 'monitor_notification', key=key, status=incident['notification'])
                issue['notification'] = incident.get('notification', 'NOT_REQUESTED')
            save(directory / 'incidents.json', history)
            report = {'checked_at': at.isoformat(), 'status': 'ERROR' if problems else ('WARNING' if warnings else 'OK'),
                      'problems': problems, 'warnings': warnings}
            save(directory / 'last-check.json', report)
            emit(log, 'monitor_checked', status=report['status'], problems=len(problems), warnings=len(warnings))
            prune(root, at)
            return 1 if problems else 0
        finally:
            for handler in log.handlers:
                handler.close()


def main() -> int:
    os.umask(0o077)
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init', help='Start observing future slots; no historical replay')
    run = sub.add_parser('run', help='Capture one real scheduled execution')
    run.add_argument('task', choices=TASKS)
    watch = sub.add_parser('check', help='Local health inspection; --notify enables incident notifications')
    watch.add_argument('--notify', action='store_true')
    sub.add_parser('status', help='Read latest monitor report and run records')
    args = parser.parse_args()
    directory = monitor_dir(ROOT)
    if args.command == 'init':
        path = directory / 'config.json'
        if not path.exists():
            save(path, {'enabled_at': now().isoformat(), 'log_retention_days': 30, 'record_retention_days': 90})
        print(json.dumps(read(path), ensure_ascii=False))
        return 0
    if args.command == 'run':
        def stop(signum, frame):
            raise InterruptedError(f'Signal {signum}')
        signal.signal(signal.SIGTERM, stop)
        return run_task(ROOT, args.task)
    if args.command == 'check':
        return check(ROOT, args.notify)
    rows = [read(path) for path in sorted((directory / 'runs').glob('*/*/*.json'))]
    print(json.dumps({'config': read(directory / 'config.json'), 'last_check': read(directory / 'last-check.json'),
                      'recent_runs': sorted(rows, key=lambda row: row['started_at'])[-10:]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
