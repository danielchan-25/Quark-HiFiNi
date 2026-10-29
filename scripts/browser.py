"""Start or check the dedicated local browser without exposing credentials."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def cdp_port(value: str) -> int:
    parsed = urlparse(value)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'}
            or parsed.username or parsed.password or parsed.path not in {'', '/'}
            or parsed.query or parsed.fragment):
        raise ValueError('HIFINI_CDP_URL must be a local HTTP origin on 127.0.0.1 or localhost')
    return parsed.port or 80


def healthy(port: int) -> bool:
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        connection.request('GET', '/json/version')
        response = connection.getresponse()
        data = json.loads(response.read(128_000))
        return response.status == 200 and isinstance(data, dict) and bool(data.get('webSocketDebuggerUrl'))
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def browser_binary() -> str:
    configured = os.getenv('BROWSER_BIN')
    if configured:
        found = shutil.which(configured)
        if not found:
            raise ValueError('BROWSER_BIN does not name an executable browser')
        return found
    for name in ('google-chrome-stable', 'google-chrome', 'chromium', 'chromium-browser'):
        if found := shutil.which(name):
            return found
    raise ValueError('Install Google Chrome/Chromium or set BROWSER_BIN in .env')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='check only; do not start Chrome')
    args = parser.parse_args()
    load_dotenv(ROOT / '.env')
    try:
        port = cdp_port(os.getenv('HIFINI_CDP_URL', 'http://127.0.0.1:9501'))
        if healthy(port):
            print(f'Chrome is already available on local port {port}.')
            return 0
        if args.check:
            print('The configured local Chrome endpoint is unavailable.', file=sys.stderr)
            return 1
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            raise ValueError('Start the browser as the desktop user, not root')
        executable = browser_binary()
        profile = ROOT / 'data' / 'chrome-profile'
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        command = [executable, f'--remote-debugging-port={port}', '--remote-debugging-address=127.0.0.1',
                   f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check']
        if os.getenv('BROWSER_HEADLESS', '0') == '1':
            command.append('--headless=new')
        command.append('https://www.hifini.net/')
        with (ROOT / 'data' / 'browser.log').open('ab') as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True)
        for _ in range(30):
            if healthy(port):
                print(f'Chrome is ready on local port {port}. Log in before the first production run.')
                return 0
            if process.poll() is not None:
                break
            time.sleep(0.5)
        raise ValueError('Chrome endpoint did not become ready; inspect data/browser.log and the desktop session')
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
