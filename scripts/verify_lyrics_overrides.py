#!/usr/bin/env python3
"""Validate manually specified Baidu Baike lyrics links through the shared CDP browser.

Writes the shared MySQL state documents so the panel can show verification
results without waiting for the daily publication run.

Usage:
    python scripts/verify_lyrics_overrides.py --all
    python scripts/verify_lyrics_overrides.py --key '张敬轩/笑忘书'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main as app                      # noqa: E402
import runtime_store as runtime         # noqa: E402
from db_documents import apply_lyrics_result

OVERRIDES = ROOT / 'data' / 'lyrics_overrides.json'
STATUS = ROOT / 'data' / 'lyrics_override_status.json'
BJ = timezone(timedelta(hours=8))

EXPR = app.LYRICS_EXTRACT_JS


class NullLog:
    def event(self, *args, **kwargs):
        pass


def load_json(path, default):
    try:
        return runtime.read_json(Path(path), default)
    except (OSError, ValueError):
        return default


def load_overrides():
    data = load_json(OVERRIDES, {})
    items = data.get('items') if isinstance(data, dict) else None
    return items if isinstance(items, dict) else {}


def load_status():
    data = load_json(STATUS, {})
    items = data.get('items') if isinstance(data, dict) else None
    return items if isinstance(items, dict) else {}


def save_status(items):
    runtime.write_json(STATUS, {'version': 1, 'items': items})


def stamp():
    return datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S')


def update(key, entry):
    """Update one result atomically without overwriting another verification."""
    result = dict(entry, checked_at=stamp(), checked_ts=int(datetime.now(BJ).timestamp()))
    return runtime.update_json_documents(ROOT, {
        OVERRIDES: {'version': 1, 'items': {}}, STATUS: {'version': 1, 'items': {}}
    }, lambda documents: apply_lyrics_result(documents, OVERRIDES, STATUS, key, result))


def validate(api, key, url):
    started = datetime.now(BJ)
    try:
        text = api._eval(url, EXPR)
    except app.FlowError as exc:
        message = str(exc)
        if '安全验证' in message or '验证码' in message:
            return dict(status='failed', detail='百度百科要求安全验证，请更换词条或稍后重试', url=url)
        if '超时' in message:
            return dict(status='failed', detail='页面加载超时，请稍后重试', url=url)
        return dict(status='failed', detail=message[:300], url=url)
    except Exception as exc:            # network / CDP / decoding
        return dict(status='failed', detail=f'{type(exc).__name__}: {exc}'[:300], url=url)

    try:
        page = json.loads(text)
    except (TypeError, ValueError):
        return dict(status='failed', detail='页面内容读取失败', url=url)
    h1 = str(page.get('h1') or '').strip()
    lyric_text = str(page.get('lyrics') or '').strip()
    if not h1:
        return dict(status='failed', detail='页面未加载出词条标题', url=url)
    if not lyric_text:
        return dict(status='failed', detail='词条缺少可用的「歌曲歌词」章节', url=url, h1=h1)
    try:
        html = app.plain_lyrics_html(lyric_text)
        app.validate_lyrics_layout(html)
    except app.FlowError as exc:
        return dict(status='failed', detail=f'歌词格式不合格：{exc}'[:300], url=url, h1=h1)
    lines = len([line for line in lyric_text.splitlines() if line.strip()])
    chars = len(lyric_text.replace('\n', ''))
    elapsed = round((datetime.now(BJ) - started).total_seconds(), 1)
    longest = max((len(line.strip()) for line in lyric_text.splitlines() if line.strip()), default=0)
    state = 'warn' if lines < 3 else 'ok'
    detail = f'提取到 {lines} 行歌词（{chars} 字）'
    if state == 'warn' and longest > 60:
        detail += '；该章节是歌词介绍而非逐行歌词，建议更换词条'
    elif state == 'warn':
        detail += '；行数过少，可能是歌词未分行，请确认词条'
    return dict(status=state, detail=detail,
                url=url, h1=h1, lines=lines, chars=chars, elapsed=elapsed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', action='append', default=[], help='override key (repeatable)')
    parser.add_argument('--all', action='store_true', help='validate every configured override')
    parser.add_argument('--expected-url', help='ignore a queued job if its URL has changed')
    args = parser.parse_args()

    overrides = load_overrides()
    keys = list(overrides) if args.all else args.key
    keys = [key for key in dict.fromkeys(keys) if key in overrides]
    if args.expected_url:
        keys = [key for key in keys if overrides[key].get('url') == args.expected_url]
    if not keys:
        print(json.dumps({'event': 'nothing_to_validate'}), flush=True)
        return 0

    # Never fight the daily upload/publish workflow for the single CDP tab.
    lock_context = runtime.lock(ROOT, 'workflow', ROOT / 'data' / 'daily-workflow.lock')
    try:
        lock_context.__enter__()
    except BlockingIOError:
        for key in keys:
            update(key, dict(status='failed', detail='发布任务正在运行，请稍后重新验证',
                             url=overrides[key].get('url')))
        print(json.dumps({'event': 'busy', 'keys': len(keys)}), flush=True)
        return 0

    api = None
    try:
        settings = app.Settings.load()
        api = app.HiFiNi(settings, NullLog())
        for key in keys:
            url = (overrides.get(key) or {}).get('url')
            if not url:
                continue
            result = validate(api, key, url)
            update(key, result)
            print(json.dumps({'key': key, **result}, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        for key in keys:
            update(key, dict(status='failed', detail=f'验证进程异常：{type(exc).__name__}: {exc}'[:300],
                             url=(overrides.get(key) or {}).get('url')))
        print(json.dumps({'event': 'fatal', 'error': str(exc)[:300]}, ensure_ascii=False), flush=True)
        return 0
    finally:
        if api is not None:
            try:
                api.close(False)
            except Exception:
                pass
        lock_context.__exit__(None, None, None)


if __name__ == '__main__':
    raise SystemExit(main())
