"""Local scheduler evidence and watchdog; never retries uploads or publication."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import fcntl
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
import notifications
import preflight
import runtime_store as runtime
import mysql_storage

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
    if runtime.project(path)[1]:
        runtime.write_json(path,clean(value))
        return
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
    return runtime.read_json(path,default)


def monitor_dir(root: Path) -> Path:
    return root / 'data' / 'monitor'


def logger(path: Path) -> logging.Logger:
    root,cfg=runtime.project(path)
    if cfg:
        class DatabaseHandler(logging.Handler):
            def __init__(self):
                super().__init__();self.db=mysql_storage.connect(cfg,autocommit=True)
            def emit(self, record):
                try:runtime.append_log(path,clean(self.format(record)),self.db)
                except Exception as exc:raise OSError('MySQL 日志写入失败：'+str(clean(str(exc)))) from None
            def close(self):
                try:
                    if self.db.open:self.db.close()
                finally:super().close()
        result=logging.Logger(str(path));result.addHandler(DatabaseHandler())
        return result
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = logging.Logger(str(path))
    class FileHandler(RotatingFileHandler):
        def handleError(self, record):
            raise OSError('日志文件写入失败，已降级到 journal')
    handler = FileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=2, encoding='utf-8')
    os.chmod(path, 0o600)
    handler.setFormatter(logging.Formatter('%(message)s'))
    result.addHandler(handler)
    return result


def emit(log: logging.Logger, event: str, **values) -> None:
    row = clean({'time': now().isoformat(timespec='seconds'), 'event': event, **values})
    text = json.dumps(row, ensure_ascii=False)
    try:
        log.info(text)
    except OSError:
        pass
    try:
        print(text, flush=True)
    except OSError:
        pass


def persist_run(path: Path, record: dict) -> None:
    try:
        save(path, record)
    except OSError as exc:
        record['observer_state'] = 'DEGRADED'
        error = clean(f'{type(exc).__name__}: {exc}')
        if error not in record.setdefault('observer_errors', []):
            record['observer_errors'].append(error)
            try:
                print(json.dumps({'event': 'monitor_storage_error', 'error': error}), file=sys.stderr, flush=True)
            except OSError:
                pass


def connection(root: Path):
    if cfg := mysql_storage.config(root): return mysql_storage.Connection(cfg,readonly=True)
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
            if not runtime.exists(receipt):
                receipt = directory / (day + '.json')
            result['receipt_status'] = (read(receipt) or {}).get('status', 'MISSING')
        return clean(result)
    finally:
        db.close()


def outcome(task: str, code: int, facts: dict) -> str:
    if facts.get('evidence_error'):
        return 'UNKNOWN'
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
    lock = None
    storage_error = None
    try:
        if not runtime.enabled(root):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = runtime.lock(root,'monitor.'+task,directory/(task+'.lock'))
        lock.__enter__()
    except BlockingIOError:
        if lock: lock = None
        print(json.dumps({'status': 'already_running', 'task': task}))
        return 0
    except OSError as exc:
        storage_error = clean(str(exc))
        if lock: lock.__exit__(None,None,None)
        lock = None
    path = directory / 'runs' / task / day / (identifier + '.json')
    log_path = root / 'logs' / 'monitor' / task / day / (identifier + '.log')
    try:
        log = logger(log_path)
    except OSError as exc:
        log = logging.Logger('monitor-journal-fallback')
        log.addHandler(logging.NullHandler())
        storage_error = clean(str(exc))
        print(json.dumps({'event': 'monitor_log_unavailable', 'error': clean(str(exc))}), file=sys.stderr, flush=True)
    record = {'task': task, 'date': day, 'id': identifier, 'started_at': started.isoformat(),
              'pid': os.getpid(), 'state': 'RUNNING', 'log_path': str(log_path), 'record_path': str(path),
              'systemd_invocation_id': os.getenv('INVOCATION_ID'), 'last_output_at': None, 'observer_state': 'OK'}
    if storage_error: record.update(observer_state='DEGRADED', observer_errors=[storage_error])
    process = None
    code = 1
    before = None
    interrupted = False
    last_saved = time.monotonic()
    try:
        persist_run(path, record)
        emit(log, 'monitored_task_started', task=task, id=identifier)
        try:
            before = run_ids(root)
        except (OSError, sqlite3.Error) as exc:
            record.update(observer_state='DEGRADED', observer_errors=[clean(str(exc))])
        if command is None:
            command = (['bash', str(root / 'scripts' / 'daily-run.sh'), '--execute'] if task == 'daily' else
                       ['bash', str(root / 'scripts' / 'run.sh'), 'daily-summary', '--send'])
        process = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', errors='replace', bufsize=1,
                                   env={**os.environ, 'PYTHONUNBUFFERED': '1', 'QUARK_WORKFLOW_ID': identifier}, start_new_session=True)
        record['child_pid'] = process.pid
        persist_run(path, record)
        for line in process.stdout:
            line = line.rstrip('\r\n')
            try:
                payload = json.loads(line)
                line = json.dumps(clean(payload), ensure_ascii=False)
                if isinstance(payload, dict):
                    record['last_event'] = payload.get('event', record.get('last_event')) or ''
                    if payload.get('event') == 'workflow_stage_started':
                        record['stage'] = payload.get('stage', 'unknown')
                        record['stage_started_at'] = payload.get('time', now().isoformat())
                    if payload.get('event') == 'workflow_finished' or 'publication' in payload:
                        record['workflow'] = clean(payload)
            except ValueError:
                line = clean(line)
            # Keep stdout/stderr available even when application initialization fails.
            try:
                log.info('%s %s', now().isoformat(timespec='seconds'), line[:32768])
            except OSError as exc:
                record.update(observer_state='DEGRADED', observer_errors=[clean(str(exc))])
            try:
                print(line[:32768], flush=True)
            except OSError:
                pass
            record['last_output_at'] = now().isoformat()
            if time.monotonic() - last_saved >= 5 or record.get('last_event', '').startswith('workflow_'):
                persist_run(path, record)
                last_saved = time.monotonic()
        code = process.wait()
    except (OSError, sqlite3.Error, ValueError, KeyboardInterrupt, InterruptedError) as exc:
        interrupted = isinstance(exc, (KeyboardInterrupt, InterruptedError))
        error = clean(f'{type(exc).__name__}: {exc}')
        if process is None or interrupted:
            record['error'] = error
        else:
            record.update(observer_state='DEGRADED', observer_errors=[error])
        emit(log, 'monitored_task_error', error=error)
        code = 130 if interrupted else 1
    finally:
        if interrupted and process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        elif process is not None and process.poll() is None:
            # An observer failure is not authorization to kill the business task.
            process.communicate()
            code = process.returncode
        if process is not None and process.stdout is not None:
            process.stdout.close()
        try:
            record['evidence'] = evidence(root, before, task, day) if before is not None else {'evidence_error': '无法读取执行前数据库快照'}
        except (OSError, sqlite3.Error, ValueError) as exc:
            record['evidence'] = {'evidence_error': clean(f'{type(exc).__name__}: {exc}')}
        record.update(state='FINISHED', ended_at=now().isoformat(), exit_code=code,
                      duration_seconds=round((now() - started).total_seconds(), 3))
        record['outcome'] = 'FAILED' if record.get('error') or code < 0 or code == 130 else outcome(task, code, record['evidence'])
        if record.get('workflow', {}).get('status') == 'already_running':
            record['outcome'] = 'BUSY'
        persist_run(path, record)
        emit(log, 'monitored_task_finished', task=task, outcome=record['outcome'], exit_code=code,
             duration_seconds=record['duration_seconds'], record_path=str(path))
        for handler in log.handlers:
            handler.close()
        if lock: lock.__exit__(None,None,None)
    return code


def units() -> dict:
    names = [f'quark-hifini-{task}.{kind}' for task in TASKS for kind in ('timer', 'service')]
    if (read(monitor_dir(ROOT) / 'config.json', {}) or {}).get('backup', {}).get('enabled_at'):
        names += ['quark-hifini-backup.timer', 'quark-hifini-backup.service']
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
            records = [read(path) for path in runtime.paths(monitor_dir(root) / 'runs' / task / day.isoformat(),'*.json')]
            records = [row for row in records if datetime.fromisoformat(row['started_at']) >= due]
            if not records:
                if at >= due + GRACE:
                    problem(f'{day}:{task}:missing', f'{task} 应于 {due.isoformat()} 启动，超过 10 分钟仍无运行记录')
                continue
            row = max(records, key=lambda r: r['started_at'])
            if row.get('observer_state') == 'DEGRADED':
                problem(f'{day}:{task}:observer', f'{task} 日志监测存储降级，业务结果为 {row.get("outcome", "进行中")}', record=row)
            elapsed = (at - datetime.fromisoformat(row['started_at'])).total_seconds()
            if row['state'] == 'RUNNING':
                if not service_active:
                    problem(f'{day}:{task}:interrupted', f'{task} 记录未结束且 systemd 服务已停止', record=row)
                elif elapsed > maximum:
                    problem(f'{day}:{task}:slow', f'{task} 运行超过 {maximum // 60} 分钟', record=row)
            elif row['outcome'] in ('FAILED', 'UNKNOWN'):
                assessment = preflight.assess(root, row, at) if task == 'daily' else None
                key = f'{day}:{task}:failed'
                if assessment and assessment['recovered']:
                    warnings.append({'key': key, 'task': task, 'date': str(day), 'kind': 'recovered_preflight',
                                     'detail': preflight.describe(assessment), 'recovery': assessment, 'record': row})
                else:
                    detail = preflight.describe(assessment) if assessment else f'{task} 执行异常：{row["outcome"]}，退出码 {row["exit_code"]}'
                    problem(key, detail, record=row)
            elif row['outcome'] == 'PARTIAL_FAILURE':
                warnings.append({'task': task, 'date': str(day), 'detail': '已有确认提交，部分候选处理失败', 'record': row})
    db = connection(root)
    try:
        integrity = db.execute('PRAGMA quick_check(1)').fetchone()[0]
        if integrity != 'ok':
            problem(f'{at.date()}:database', '数据库 quick_check 异常：' + integrity)
        unknown = db.execute("SELECT a.id,a.title FROM submission_attempts a JOIN runs r ON r.id=a.run_id "
                             "WHERE a.status IN ('UNKNOWN','SUBMITTING') AND r.status!='RUNNING'").fetchall()
        if unknown:
            problem(f'{at.date()}:unknown-submission', '歌曲待核实，请确认是否已发布：' + '；'.join(row['title'] for row in unknown) + '。仅跳过这些歌曲，不影响后续日期处理其他歌曲。', attempt_ids=[row['id'] for row in unknown])
    finally:
        db.close()
    if shutil.disk_usage(root).free < 1024 ** 3:
        problem(f'{at.date()}:disk', '项目磁盘可用空间少于 1 GiB')
    backup = config.get('backup', {})
    if backup.get('enabled_at'):
        timer = services.get('quark-hifini-backup.timer', {})
        if timer.get('ActiveState') != 'active' or timer.get('UnitFileState') != 'enabled':
            problem(f'{at.date()}:backup:timer', '自动备份定时器未启用或未处于 active 状态')
        state = read(root / 'data' / 'backup' / 'last-backup.json', {})
        last = state.get('last_success_at') or backup['enabled_at']
        if (at - datetime.fromisoformat(last)).total_seconds() > 26 * 3600:
            problem(f'{at.date()}:backup:stale', '超过 26 小时没有已校验成功的自动备份')
        if state.get('status') == 'FAILED' or services.get('quark-hifini-backup.service', {}).get('ActiveState') == 'failed':
            problem(f'{at.date()}:backup:failed', '自动备份失败：' + state.get('error', '详见备份记录'))
    return problems, warnings


def notify(detail: str) -> dict:
    """One delivery attempt; unknown delivery is retained without automatic replay."""
    return notifications.send_text(os.getenv('FEISHU_WEBHOOK'), os.getenv('FEISHU_SECRET'),
        '【Quark-HiFiNi 定时任务监测】\n' + detail, execute=app.execute,
        curl=app.curl_binary(), sanitize=clean)


def prune(root: Path, at: datetime) -> None:
    if runtime.enabled(root):
        runtime.prune(root,at)
        return
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
    try:
        check_lock=runtime.lock(root,'monitor.check',directory/'check.lock')
        check_lock.__enter__()
    except BlockingIOError:return 0
    try:
        at = now()
        log = logger(root / 'logs' / 'monitor' / 'watchdog.log')
        try:
            complete = True
            try:
                problems, warnings = inspect(root, at, config, units())
            except (OSError, sqlite3.Error, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
                problems = [{'key': f'{at.date()}:monitor-error', 'detail': clean(f'巡检异常：{type(exc).__name__}: {exc}')}]
                warnings = []
                complete = False
            history = read(directory / 'incidents.json', {})
            keys = {row['key'] for row in problems}
            for key, row in history.items():
                checked_dates = {str(at.date()), str(at.date() - timedelta(days=1))}
                if complete and key.split(':', 1)[0] in checked_dates and key not in keys and not row.get('resolved_at'):
                    row['resolved_at'] = at.isoformat()
                    recovery = next((item['recovery'] for item in warnings if item.get('key') == key and item.get('kind') == 'recovered_preflight'), None)
                    if recovery:
                        row['resolution_evidence'] = recovery
            for issue in problems:
                key = issue['key']
                if key not in history:
                    history[key] = {'first_seen_at': at.isoformat(), 'detail': issue['detail']}
                incident = history[key]
                incident.pop('resolved_at', None)
                prior = incident.get('notification_result', {})
                safe_retry = prior.get('status') in ('NOT_SENT', 'NOT_CONFIGURED') and incident.get('notification_attempts', 0) < 3
                retry_due = not incident.get('retry_after') or at >= datetime.fromisoformat(incident['retry_after'])
                if send and (not incident.get('notification') or (safe_retry and retry_due)):
                    # Persist the claim before network I/O, including ambiguous crashes.
                    incident['notification'] = 'SENDING'
                    incident['notification_attempts'] = incident.get('notification_attempts', 0) + 1
                    save(directory / 'incidents.json', history)
                    row = issue.get('record', {})
                    detail = issue['detail'] + '\n' + str(row.get('log_path', root / 'logs' / 'monitor' / 'watchdog.log'))
                    if row:
                        attempts = Counter(item['status'] for item in row.get('evidence', {}).get('attempts', []))
                        detail += f'\n开始：{row["started_at"]}，阶段：{row.get("stage", "初始化")}，最后事件：{row.get("last_event")}'
                        detail += f'\n本次确认提交 {attempts["CONFIRMED"]} 篇，未确认 {attempts["UNKNOWN"] + attempts["SUBMITTING"]} 篇。'
                    detail += '\n请核对实际提交记录后处理，监测不会自动重跑发布。'
                    result = notify(detail)
                    # Preserve compatibility with old delivery records; never replay ambiguous sends.
                    if isinstance(result, str): result = {'status': result}
                    incident['notification_result'] = result
                    incident['notification'] = result['status']
                    if result['status'] in ('NOT_SENT', 'NOT_CONFIGURED'):
                        incident['retry_after'] = (at + timedelta(minutes=5)).isoformat()
                    emit(log, 'monitor_notification', key=key, status=incident['notification'])
                issue['notification'] = incident.get('notification', 'NOT_REQUESTED')
            save(directory / 'incidents.json', history)
            recoveries = [item for item in warnings if item.get('kind') == 'recovered_preflight']
            warnings = [item for item in warnings if item.get('kind') != 'recovered_preflight']
            report = {'checked_at': at.isoformat(), 'status': 'ERROR' if problems else ('WARNING' if warnings else 'OK'),
                      'problems': problems, 'warnings': warnings, 'recoveries': recoveries, 'checks_complete': complete}
            save(directory / 'last-check.json', report)
            emit(log, 'monitor_checked', status=report['status'], problems=len(problems), warnings=len(warnings))
            prune(root, at)
            return 1 if problems else 0
        finally:
            for handler in log.handlers:
                handler.close()
    finally:check_lock.__exit__(None,None,None)


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
        if not runtime.exists(path):
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
    rows = [read(path) for path in runtime.paths(directory / 'runs','*/*/*.json')]
    print(json.dumps({'config': read(directory / 'config.json'), 'last_check': read(directory / 'last-check.json'),
                      'recent_runs': sorted(rows, key=lambda row: row['started_at'])[-10:]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
