"""Shared Feishu delivery evidence. Ambiguous delivery is never auto-retried."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time

PRECONNECT = re.compile(r'Resolving timed out|Could not resolve (?:host|proxy)|Connection timed out|Failed to connect|bind failed|Failed binding', re.I)


def retryable(row) -> bool:
    """Accept new evidence and conservative, single-attempt legacy evidence."""
    row = dict(row)
    if row.get('status') == 'NOT_CONFIGURED' and row.get('attempts', 0) == 0:
        return True
    if row.get('status') != 'FAILED':
        return False
    try:
        detail = json.loads(row.get('last_error') or '{}')
    except (ValueError, TypeError):
        detail = None
    if isinstance(detail, dict) and detail.get('delivery_version') == 1:
        return detail.get('status') in ('NOT_SENT', 'NOT_CONFIGURED')
    return row.get('attempts') == 1 and bool(PRECONNECT.search(row.get('last_error') or ''))


def send_text(webhook, secret, text, *, execute, curl, sanitize, windows=False) -> dict:
    base = {'delivery_version': 1, 'status': 'UNKNOWN', 'error': None}
    if not webhook:
        return {**base, 'status': 'NOT_CONFIGURED', 'error_type': 'configuration', 'error': 'FEISHU_WEBHOOK 未配置'}
    payload = {'msg_type': 'text', 'content': {'text': sanitize(text)}}
    if secret:
        stamp = str(int(time.time()))
        key = f'{stamp}\n{secret}'.encode()
        payload.update(timestamp=stamp, sign=base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode())
    try:
        result = execute([curl, '--noproxy', '*', '-4', *(['--ssl-no-revoke'] if windows else []),
                          '-fsS', '--connect-timeout', '10', '--max-time', '30', '-H',
                          'Content-Type: application/json', '--data-binary', '@-', webhook],
                         input=json.dumps(payload, ensure_ascii=False), timeout=35, operation='飞书告警')
    except OSError as exc:
        return {**base, 'status': 'NOT_SENT', 'error_type': 'start', 'error': sanitize(str(exc))}
    except RuntimeError as exc:
        return {**base, 'error_type': 'transport_timeout', 'error': sanitize(str(exc))}
    if result.returncode:
        error = sanitize(result.stderr.strip() or f'curl exit {result.returncode}')
        unsent = result.returncode in (5, 6, 7, 45) or (result.returncode == 28 and bool(PRECONNECT.search(error)))
        return {**base, 'status': 'NOT_SENT' if unsent else 'UNKNOWN', 'curl_exit': result.returncode,
                'error_type': 'preconnect' if unsent else 'transport', 'error': error[:2000]}
    try:
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            raise ValueError('飞书响应不是对象')
        code = response.get('code', response.get('StatusCode'))
        if code is None:
            raise ValueError('飞书响应缺少状态码')
        if code in (0, '0'):
            return {**base, 'status': 'SENT', 'response_code': code}
        return {**base, 'status': 'REJECTED', 'response_code': sanitize(code), 'error_type': 'remote_rejection',
                'error': sanitize(str(response.get('msg') or response.get('StatusMessage') or '飞书明确拒绝请求'))[:2000]}
    except (ValueError, TypeError):
        return {**base, 'error_type': 'invalid_response', 'error': '请求已完成，但响应无法确认发送结果'}
