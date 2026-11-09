"""Durable preflight evidence, with historical failures kept separate from recovery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import uuid
import runtime_store as runtime

BEIJING = timezone(timedelta(hours=8))


def blockers(report: dict) -> list[dict]:
    if isinstance(report.get('blockers'), list):
        return report['blockers']
    result = []
    if report.get('database') != 'ok':
        result.append({'code': 'database', 'detail': '数据库完整性检查未通过'})
    for name, available in report.get('dependencies', {}).items():
        if not available:
            result.append({'code': 'dependency:' + name, 'detail': '缺少运行程序：' + name})
    if not report.get('git_bash'):
        result.append({'code': 'bash', 'detail': '找不到任务运行所需的 Bash'})
    if not report.get('quark_cli'):
        result.append({'code': 'quark_cli', 'detail': '找不到夸克网盘命令工具'})
    # Reports from before the per-song isolation change used this as a global gate.
    if report.get('unresolved_submissions') and report.get('unresolved_blocks_other_songs', True):
        ids = '、'.join(str(value) for value in report.get('unresolved_attempt_ids', []))
        result.append({'code': 'legacy_unknown_submission',
                       'detail': '旧规则将未确认提交作为全局阻断' + ('（记录 ' + ids + '）' if ids else '')})
    return result


def read(path: Path) -> dict | None:
    try:
        value = runtime.read_json(path)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def persist(root: Path, report: dict, code: int, workflow_id: str | None = None) -> None:
    workflow_id = workflow_id or uuid.uuid4().hex
    if not re.fullmatch(r'[A-Za-z0-9_-]+', workflow_id):
        raise ValueError('Invalid workflow ID')
    report.update(checked_at=datetime.now(BEIJING).isoformat(), exit_code=code,
                  status='PASS' if code == 0 else 'FAIL', workflow_id=workflow_id)
    for path in (root / 'data/preflight/runs' / (workflow_id + '.json'), root / 'data/preflight/latest.json'):
        if runtime.enabled(root):
            runtime.write_json(path,report)
            continue
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('x', encoding='utf-8') as stream:
                os.chmod(temp, 0o600)
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def workflow_result(root: Path, workflow_id: str, code: int) -> dict:
    report = read(root / 'data/preflight/runs' / (workflow_id + '.json'))
    if (not report or report.get('workflow_id') != workflow_id or report.get('exit_code') != code
            or report.get('status') != ('PASS' if code == 0 else 'FAIL')
            or not isinstance(report.get('blockers'), list)):
        return {'exit_code': code, 'blockers': [] if code == 0 else [
            {'code': 'unavailable', 'detail': '预检程序未留下完整诊断，请查看本次任务日志'}]}
    return {key: report[key] for key in ('checked_at', 'exit_code', 'status', 'blockers')}


def legacy_report(root: Path, day: str, workflow_id: str) -> dict | None:
    # Legacy reports lived only in the existing bounded task log. Read them as
    # evidence; never rewrite old workflow results or sent notification receipts.
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or not re.fullmatch(r'[A-Za-z0-9_-]+', workflow_id):
        return None
    path = root / 'logs/monitor/daily' / day / (workflow_id + '.log')
    try:
        if not runtime.enabled(root) and path.stat().st_size > 11 * 1024 * 1024: return None
        text = runtime.text(path)
    except OSError:
        return None
    text = re.sub(r'(?m)^\d{4}-\d{2}-\d{2}T\S+ ', '', text)
    for match in re.finditer(r'(?m)^\{\s*\n', text):
        try:
            report, _ = json.JSONDecoder().raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(report, dict) and 'database' in report and 'dependencies' in report:
            return report
    return None


def instant(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def assess(root: Path, row: dict, at: datetime | None = None) -> dict | None:
    value = row.get('workflow', row)
    if value.get('publication') != 'blocked_by_preflight':
        return None
    occurred = value.get('ended_at') or row.get('ended_at') or row.get('started_at')
    workflow_id = value.get('workflow_id') or row.get('id', '')
    day = value.get('date') or row.get('date', '')
    report = value.get('preflight') or legacy_report(root, day, workflow_id) or {}
    reasons = [item for item in blockers(report) if isinstance(item, dict) and isinstance(item.get('detail'), str)] if report else []
    reason = '；'.join(item['detail'] for item in reasons) or '预检未通过，具体诊断未保存，请查看原任务日志'
    result = {'occurred_at': occurred, 'reason': reason, 'recovered': False}
    latest = read(root / 'data/preflight/latest.json') or {}
    checked, failed = instant(latest.get('checked_at')), instant(occurred)
    at = at or datetime.now(BEIJING)
    if (latest.get('status') == 'PASS' and latest.get('exit_code') == 0 and not latest.get('blockers')
            and checked and failed and failed < checked <= at):
        result.update(recovered=True, recovered_at=latest['checked_at'],
                      evidence_workflow_id=latest.get('workflow_id'))
    return result


def describe(result: dict) -> str:
    def label(value):
        stamp = instant(value)
        return stamp.astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S') if stamp else '原定时任务'
    message = label(result.get('occurred_at')) + ' 预检未通过：' + result['reason']
    if result.get('recovered'):
        message += '；' + label(result['recovered_at']) + ' 预检已通过，原任务未补跑'
    return message
