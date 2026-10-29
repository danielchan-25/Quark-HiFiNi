#!/usr/bin/env bash
set -euo pipefail
umask 077
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$PROJECT_ROOT"
if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    printf '%s\n' 'Run this installer as your desktop user; it uses sudo only for apt when requested.' >&2
    exit 1
fi
if [[ ${1:-} == --install-system-packages ]]; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv python3-pip ffmpeg curl bash nodejs npm
    shift
fi
if [[ $# -ne 0 ]]; then
    printf '%s\n' 'Usage: bash scripts/setup-ubuntu.sh [--install-system-packages]' >&2
    exit 2
fi
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required; Python 3.12 is the tested baseline"'
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p data upload-files
if [[ ! -e .env ]]; then
    cp -- .env.example .env
fi
chmod 600 .env
printf '%s\n' 'Python environment created. Install Chrome and the official Quark Skill, configure .env, and authorize on this device.'
printf '%s\n' 'Next: bash scripts/run.sh check; bash scripts/start-browser.sh; bash scripts/run.sh run --dry-run'
printf '%s\n' 'No production task or systemd timer has been enabled.'
