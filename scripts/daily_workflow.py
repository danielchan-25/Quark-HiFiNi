"""Run one upload/publish slot. Preview is the default; --execute authorizes writes."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import preflight


def finish(result: dict, workflow_id: str) -> None:
    result.update(event='workflow_finished', workflow_id=workflow_id,
                  ended_at=datetime.now(timezone(timedelta(hours=8))).isoformat())
    print(json.dumps(result), flush=True)
    path = ROOT / 'data' / 'workflows' / result['date'] / (workflow_id + '.json')
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(result, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError as exc:
        print(json.dumps({'event': 'workflow_record_unavailable', 'error_type': type(exc).__name__}), flush=True)


def invoke(arguments: list[str]) -> int:
    try:
        return subprocess.run(arguments, cwd=ROOT, check=False).returncode
    except OSError as exc:
        print(f'Unable to start task: {type(exc).__name__}', file=sys.stderr)
        return 1


def stage(name: str, arguments: list[str]) -> int:
    started = time.monotonic()
    print(json.dumps({'time': datetime.now(timezone(timedelta(hours=8))).isoformat(),
                      'event': 'workflow_stage_started', 'stage': name}), flush=True)
    code = invoke(arguments)
    print(json.dumps({'time': datetime.now(timezone(timedelta(hours=8))).isoformat(),
                      'event': 'workflow_stage_finished', 'stage': name, 'exit_code': code,
                      'duration_seconds': round(time.monotonic() - started, 3)}), flush=True)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--session-input-file', type=Path, default=ROOT / 'scripts' / 'daily-task.txt')
    args = parser.parse_args()
    if not args.execute:
        print('Preview only: upload -> publish preflight -> local browser -> one publication.')
        print('Run with --execute after configuration, migration, authorization and a dry run have been verified.')
        return 0
    if sys.platform != 'linux':
        parser.error('The Linux scheduled workflow must run on Linux')
    import fcntl
    workflow_id = os.getenv('QUARK_WORKFLOW_ID') or f'{int(time.time())}-{secrets.token_hex(5)}'
    # IDs become filenames; external callers cannot inject paths.
    if not all(char.isalnum() or char in '-_' for char in workflow_id): parser.error('Invalid workflow ID')
    day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    session_input = args.session_input_file.read_text(encoding='utf-8')
    if not session_input.strip():
        parser.error('The task instruction file is empty')
    (ROOT / 'data').mkdir(parents=True, exist_ok=True)
    with (ROOT / 'data' / 'daily-workflow.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            finish({'date': day, 'status': 'already_running', 'publication': 'busy'}, workflow_id)
            return 0
        session_id = f'{int(time.time())}-{secrets.token_hex(3)}'
        run = ['bash', str(ROOT / 'scripts' / 'run.sh')]
        tracking = ['--session-input', session_input, '--session-id', session_id]
        result = {'date': day, 'upload_exit': stage('upload', run + ['upload', '--apply', '--slot-date', day] + tracking)}
        result['check_exit'] = stage('preflight', run + ['check', '--workflow-id', workflow_id])
        result['preflight'] = preflight.workflow_result(ROOT, workflow_id, result['check_exit'])
        if result['check_exit']:
            result['publication'] = 'blocked_by_preflight'
        else:
            browser_exit = stage('browser', ['bash', str(ROOT / 'scripts' / 'start-browser.sh')])
            result['browser_exit'] = browser_exit
            if browser_exit:
                result['publication'] = 'blocked_by_browser'
            else:
                result['publish_exit'] = stage('publish', run + ['run', '--publish'] + tracking)
                result['publication'] = 'finished_check_database_for_actual_submission_result'
        finish(result, workflow_id)
        return int(any(result.get(key, 0) != 0 for key in ('upload_exit', 'check_exit', 'browser_exit', 'publish_exit')))


if __name__ == '__main__':
    raise SystemExit(main())
