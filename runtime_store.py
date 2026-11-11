"""Runtime documents and logs use MySQL when enabled; tests keep file fixtures."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import mysql_storage as mysql
from db_documents import Documents


def project(path: Path):
    for candidate in (path, *path.parents):
        if (candidate / '.env').is_file() and (candidate / 'main.py').is_file():
            cfg = mysql.config(candidate)
            if cfg: return candidate, cfg
    return None, None


def enabled(root: Path) -> bool: return mysql.config(root) is not None


def get(path: Path) -> bytes | None:
    root, cfg = project(path)
    if not cfg:
        return path.read_bytes() if path.exists() else None
    db = mysql.connect(cfg, autocommit=True)
    try:
        key = str(path.relative_to(root))
        with db.cursor() as cursor:
            cursor.execute('SELECT content FROM `'+cfg.table('state')+'` WHERE path=%s', (key,))
            row = cursor.fetchone(); data = bytes(row[0]) if row else b''
            if path.suffix.startswith('.log') or '.log.' in path.name:
                cursor.execute('SELECT content FROM `'+cfg.table('logs')+'` WHERE path=%s ORDER BY id', (key,))
                lines = cursor.fetchall()
                data += ''.join(r[0]+'\n' for r in lines).encode('utf-8')
                return data if row or lines else None
            return data if row else None
    finally: db.close()


def put(path: Path, content: bytes, *, exclusive=False):
    root, cfg = project(path)
    if not cfg:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb' if exclusive else 'wb') as stream: stream.write(content)
        return
    db = mysql.connect(cfg, autocommit=True)
    try:
        with db.cursor() as cursor:
            verb = 'INSERT IGNORE' if exclusive else 'INSERT'
            sql = verb+' INTO `'+cfg.table('state')+'` (path,content,sha256) VALUES(%s,%s,%s)'
            if not exclusive: sql += ' ON DUPLICATE KEY UPDATE content=VALUES(content),sha256=VALUES(sha256),updated_at=CURRENT_TIMESTAMP(6)'
            cursor.execute(sql,(str(path.relative_to(root)),content,hashlib.sha256(content).hexdigest()))
            if exclusive and cursor.rowcount == 0: raise FileExistsError(str(path))
    finally: db.close()


def read_json(path: Path, default=None):
    data = get(path)
    return json.loads(data) if data is not None else default


def write_json(path: Path, value, *, exclusive=False):
    put(path,json.dumps(value,ensure_ascii=False,indent=2).encode(),exclusive=exclusive)


def update_json_documents(root: Path, defaults, update):
    """Atomically edit related state documents; file fixtures remain supported."""
    cfg = mysql.config(root)
    if cfg:
        keys = {str(path.relative_to(root)): value for path, value in defaults.items()}
        documents = Documents(lambda: mysql.connect(cfg), cfg.table('state'))
        def mutate(values):
            by_path = {root / key: value for key, value in values.items()}
            result = update(by_path)
            values.update({str(path.relative_to(root)): value for path, value in by_path.items()})
            return result
        return documents.change(keys, mutate)
    values = {path: read_json(path, default) for path, default in defaults.items()}
    result = update(values)
    for path, value in values.items():
        write_json(path, value)
    return result


def text(path: Path):
    data = get(path)
    if data is None: raise FileNotFoundError(str(path))
    return data.decode('utf-8')


def exists(path: Path): return get(path) is not None


def paths(base: Path, pattern: str, *, recursive=False):
    root,cfg = project(base)
    if not cfg: return sorted(base.rglob(pattern) if recursive else base.glob(pattern))
    prefix = str(base.relative_to(root)).rstrip('/')+'/'
    db = mysql.connect(cfg,autocommit=True)
    try:
        with db.cursor() as cursor:
            cursor.execute('SELECT path FROM `'+cfg.table('state')+'` WHERE LEFT(path,%s)=%s UNION SELECT DISTINCT path FROM `'+cfg.table('logs')+'` WHERE LEFT(path,%s)=%s',(len(prefix),prefix,len(prefix),prefix))
            names=[r[0] for r in cursor.fetchall()]
        output=[]
        for name in names:
            relative=name[len(prefix):]
            if (fnmatch.fnmatch(Path(relative).name,pattern) if recursive else Path(relative).match(pattern)):
                output.append(root/name)
        return sorted(output)
    finally:db.close()


@contextmanager
def lock(root: Path, name: str, local_path: Path):
    cfg=mysql.config(root)
    if cfg:
        with mysql.lock(cfg,name):yield
        return
    import fcntl
    local_path.parent.mkdir(parents=True,exist_ok=True)
    with local_path.open('a+') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def append_log(path: Path, content: str, db):
    root,cfg=project(path)
    with db.cursor() as cursor:
        cursor.execute('INSERT INTO `'+cfg.table('logs')+'` (path,content) VALUES(%s,%s)',(str(path.relative_to(root)),content))


def prune(root: Path, at):
    cfg=mysql.config(root);db=mysql.connect(cfg,autocommit=True)
    try:
        with db.cursor() as c:
            c.execute('DELETE FROM `'+cfg.table('logs')+'` WHERE created_at<%s',(datetime.fromtimestamp(at.timestamp()-30*86400,timezone.utc).replace(tzinfo=None),))
            prefix='data/monitor/runs/'
            c.execute('DELETE FROM `'+cfg.table('state')+'` WHERE LEFT(path,%s)=%s AND updated_at<%s',(len(prefix),prefix,datetime.fromtimestamp(at.timestamp()-90*86400,timezone.utc).replace(tzinfo=None)))
    finally:db.close()
