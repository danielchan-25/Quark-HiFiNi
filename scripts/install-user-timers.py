"""Generate user systemd units for this checkout; never enable or start timers."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent


def quote_systemd_path(value: str, *, command: bool = False) -> str:
    if any(char in value for char in '\n\r\0'):
        raise ValueError('Project path contains an unsupported control character')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if command:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def render_service(text: str, root: Path) -> str:
    lines = []
    for line in text.splitlines():
        if line.startswith('WorkingDirectory='):
            # systemd directory directives accept spaces but not shell quotes.
            line = 'WorkingDirectory=' + str(root).replace('\\', '\\\\').replace('%', '%%')
        elif line.startswith('ExecStart='):
            suffix = line.split('@PROJECT_ROOT@/', 1)[1]
            script, _, arguments = suffix.partition(' ')
            line = 'ExecStart=/usr/bin/bash ' + quote_systemd_path(str(root / script), command=True)
            if arguments:
                line += ' ' + arguments
        lines.append(line)
    return '\n'.join(lines) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit-dir', type=Path)
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() == 0:
        parser.error('Run as your Ubuntu desktop user, not root')
    destination = args.unit_dir or Path(os.getenv('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'systemd' / 'user'
    files = {}
    for name in ('quark-hifini-daily', 'quark-hifini-summary', 'quark-hifini-monitor'):
        files[name + '.service'] = render_service((ROOT / 'systemd' / (name + '.service.in')).read_text(encoding='utf-8'), ROOT)
        files[name + '.timer'] = (ROOT / 'systemd' / (name + '.timer')).read_text(encoding='utf-8')
    if not args.overwrite and any((destination / name).exists() for name in files):
        parser.error('One or more unit files already exist; inspect them before using --overwrite')
    destination.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (destination / name).write_text(text, encoding='utf-8')
    print(f'Generated disabled user units in {destination}.')
    print('After validation: systemctl --user daemon-reload')
    print('Initialize monitoring: bash scripts/monitor.sh init')
    print('Then explicitly enable: systemctl --user enable --now quark-hifini-daily.timer quark-hifini-summary.timer quark-hifini-monitor.timer')
    print('Keep the desktop session active and the computer awake. Persistent=false does not backfill missed slots.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
