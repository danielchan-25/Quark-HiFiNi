"""Consistent private runtime backups; restore verification never opens production for writes."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent
BEIJING = timezone(timedelta(hours=8))


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def verify(archive: Path, manifest: dict, tables: dict | None = None) -> None:
    """Stream every member, without extracting credentials or trusting tar paths."""
    seen = set()
    with tarfile.open(archive, 'r:gz') as source:
        for item in source:
            if not item.isfile() or item.name not in manifest or item.name in seen:
                raise ValueError('Unexpected archive member')
            value = hashlib.sha256()
            with source.extractfile(item) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    value.update(block)
            if value.hexdigest() != manifest[item.name]: raise ValueError('Backup member checksum mismatch')
            seen.add(item.name)
    if seen != set(manifest): raise ValueError('Backup members missing')
    if tables is not None:
        with tempfile.TemporaryDirectory(prefix='quark-hifini-restore-') as directory:
            restored = Path(directory) / 'restored.sqlite'
            with tarfile.open(archive, 'r:gz') as source, restored.open('wb') as destination:
                shutil.copyfileobj(source.extractfile('data/quark-hifini.db'), destination)
            db = sqlite3.connect(restored.as_uri() + '?mode=ro', uri=True)
            try:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('Restore integrity check failed')
                if list(db.execute('PRAGMA foreign_key_check')): raise ValueError('Restore foreign key check failed')
                actual = {name: db.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0] for name in tables}
                if actual != tables: raise ValueError('Restore table counts differ')
            finally: db.close()


def perform(root: Path, destination: Path, records: Path, retention_days: int = 14) -> dict:
    directory = root / 'data' / 'backup'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'job.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _perform(root, destination.resolve(), records.resolve(), retention_days)


def _perform(root: Path, destination: Path, records: Path, retention_days: int) -> dict:
    if retention_days < 1: raise ValueError('Retention must be positive')
    at = datetime.now(BEIJING)
    identifier = at.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    filename = 'quark-hifini-state-' + identifier + '.tar.gz'
    record = records / identifier
    state = root / 'data' / 'backup' / 'last-backup.json'
    previous = json.loads(state.read_text()) if state.exists() else {}
    result = {'started_at': at.isoformat(), 'status': 'RUNNING', 'archive': str(destination / filename),
              'last_success_at': previous.get('last_success_at'), 'retention_days': retention_days}
    incoming = destination / ('.' + filename + '.partial')
    try:
        if destination.is_relative_to(Path('/mnt/data')) and not os.path.ismount('/mnt/data'):
            raise OSError('/mnt/data 网络存储尚未挂载，拒绝将长期备份写入本地替代目录')
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        record.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Stage on local disk: a network share must not hold the publication lock.
        with tempfile.TemporaryDirectory(prefix='quark-hifini-backup-') as directory:
            temporary = Path(directory)
            stage = temporary / 'snapshot'
            stage.mkdir()
            with (root / 'data' / 'run.lock').open('a+b') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                target_db = stage / 'data' / 'quark-hifini.db'
                target_db.parent.mkdir()
                source = sqlite3.connect((root / 'data' / 'quark-hifini.db').resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
                target = sqlite3.connect(target_db)
                try:
                    source.backup(target, pages=256)
                    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('Snapshot integrity check failed')
                    if list(target.execute('PRAGMA foreign_key_check')): raise ValueError('Snapshot foreign key check failed')
                    result['tables'] = {name: target.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]
                                        for (name,) in target.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                finally:
                    source.close()
                    target.close()
                for path in (root / 'data').rglob('*'):
                    relative = path.relative_to(root)
                    if not path.is_file() or path.is_symlink(): continue
                    if path.name.startswith('quark-hifini.db') or path.suffix in ('.lock', '.tmp') or 'chrome-profile' in relative.parts: continue
                    to = stage / relative
                    to.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, to)
                if (root / '.env').is_file(): shutil.copy2(root / '.env', stage / '.env')
            # Code and installed units are reproducibility evidence; not a venv/browser copy.
            source_files = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                                          cwd=root, capture_output=True, check=True).stdout.decode().split('\0')
            for name in source_files:
                if not name: continue
                path = root / name
                if not path.is_file() or path.is_symlink(): continue
                to = stage / 'source' / name
                to.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, to)
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
            result['git_commit'] = head
            result['git_dirty'] = bool(subprocess.check_output(['git', 'status', '--short'], cwd=root, text=True))
            for path in (Path.home() / '.config' / 'systemd' / 'user').glob('quark-hifini-*'):
                if path.is_file() and not path.is_symlink():
                    to = stage / 'systemd' / path.name
                    to.parent.mkdir(exist_ok=True)
                    shutil.copy2(path, to)
            atomic_json(stage / 'backup-metadata.json', result)
            manifest = {str(path.relative_to(stage)): digest(path) for path in stage.rglob('*') if path.is_file()}
            archive = temporary / filename
            with tarfile.open(archive, 'w:gz') as output:
                for name in sorted(manifest): output.add(stage / name, arcname=name, recursive=False)
            verify(archive, manifest)
            expected = digest(archive)
            shutil.copyfile(archive, incoming)
            if digest(incoming) != expected: raise ValueError('Remote backup checksum mismatch')
            verify(incoming, manifest, result['tables'])
            incoming.replace(destination / filename)
            (destination / (filename + '.sha256')).write_text(expected + '  ' + filename + '\n')
            result.update(status='SUCCESS', sha256=expected, file_count=len(manifest),
                          finished_at=datetime.now(BEIJING).isoformat(), last_success_at=datetime.now(BEIJING).isoformat())
            atomic_json(record / 'manifest.json', manifest)
        atomic_json(record / 'backup.json', result)
        atomic_json(state, result)
        # Remove only this backup tool's verified, old archives; retain at least the new backup.
        cutoff = at - timedelta(days=retention_days)
        for path in destination.glob('quark-hifini-state-????????-??????-????????.tar.gz'):
            if path.name == filename: continue
            try: stamp = datetime.strptime(path.name[19:34], '%Y%m%d-%H%M%S').replace(tzinfo=BEIJING)
            except ValueError: continue
            checksum = path.with_name(path.name + '.sha256')
            if stamp < cutoff and checksum.exists():
                saved = checksum.read_text().split()[0]
                if digest(path) == saved:
                    path.unlink()
                    checksum.unlink()
        return result
    except Exception as exc:
        try: incoming.unlink(missing_ok=True)
        except OSError: pass
        result.update(status='FAILED', error_type=type(exc).__name__, error=str(exc), finished_at=datetime.now(BEIJING).isoformat())
        for path in (state, record / 'backup.json'):
            try: atomic_json(path, result)
            except OSError: pass
        raise


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--destination', type=Path, default=Path('/mnt/data/backup/Quark-HiFiNi/daily'))
    parser.add_argument('--record-dir', type=Path, default=Path('/mnt/data/record/Quark-HiFiNi/daily-backup'))
    parser.add_argument('--retention-days', type=int, default=14)
    args = parser.parse_args()
    if not args.execute:
        print('Preview: consistent SQLite/state backup, checksums and isolated restore verification; no changes.')
        return 0
    try:
        print(json.dumps(perform(ROOT, args.destination, args.record_dir, args.retention_days), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'FAILED', 'error_type': type(exc).__name__, 'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
