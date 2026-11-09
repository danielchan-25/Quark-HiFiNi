"""MySQL persistence and the small SQL compatibility boundary used by the app."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class Config:
    host: str
    user: str
    password: str = field(repr=False)
    port: int = 3306
    database: str = 'Quark'
    prefix: str = 'bilibili_HiFiNi'

    def table(self, logical: str) -> str:
        return self.prefix if logical == 'resources' else self.prefix + '_' + logical


def config(root: Path) -> Config | None:
    values = dotenv_values(root / '.env')
    backend=values.get('STORAGE_BACKEND')
    if backend is None:
        if (root/'main.py').is_file() and not (root/'data/quark-hifini.db').is_file():
            raise sqlite3.OperationalError('未配置存储且未找到原数据库，禁止自动创建本地空库')
        return None
    if backend.strip().lower()=='sqlite':return None
    if backend.strip().lower()!='mysql':raise sqlite3.OperationalError('STORAGE_BACKEND 只能为 mysql 或 sqlite，禁止静默回退')
    cfg = Config(values.get('MYSQL_HOST', ''), values.get('MYSQL_USER', ''), values.get('MYSQL_PASSWORD', ''),
                 int(values.get('MYSQL_PORT', '3306')), values.get('MYSQL_DATABASE', 'Quark'),
                 values.get('MYSQL_TABLE_PREFIX', 'bilibili_HiFiNi'))
    if not cfg.host or not cfg.user or not all(re.fullmatch(r'[A-Za-z0-9_]+', x) for x in (cfg.database, cfg.prefix)):
        raise sqlite3.OperationalError('MySQL 连接配置不完整或数据库/表前缀无效')
    return cfg


def connect(cfg: Config, *, autocommit=False, database=True):
    import pymysql
    try:
        db = pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password,
                             database=cfg.database if database else None, charset='utf8mb4',
                             autocommit=autocommit, connect_timeout=7, read_timeout=30, write_timeout=30)
        with db.cursor() as cursor:
            cursor.execute("SET time_zone='+00:00'")
        return db
    except pymysql.MySQLError as exc:
        raise sqlite3.OperationalError(str(exc).replace(cfg.password, '[REDACTED]')) from None


@contextmanager
def lock(cfg: Config, name: str, wait: int = 0):
    db = connect(cfg, autocommit=True)
    key = 'quark-hifini:' + hashlib.sha256((cfg.database + ':' + cfg.prefix + ':' + name).encode()).hexdigest()[:48]
    acquired = False
    try:
        with db.cursor() as cursor:
            cursor.execute('SELECT GET_LOCK(%s,%s)', (key, wait))
            acquired = cursor.fetchone()[0] == 1
        if not acquired:
            raise BlockingIOError('已有同类任务正在运行')
        yield
    finally:
        if acquired:
            try:
                with db.cursor() as cursor: cursor.execute('SELECT RELEASE_LOCK(%s)', (key,))
            except Exception:
                pass
        db.close()


class Row:
    def __init__(self, keys, values):
        self.names, self.values = tuple(keys), tuple(self.value(x) for x in values)

    @staticmethod
    def value(value):
        return value.strftime('%Y-%m-%d %H:%M:%S') if isinstance(value, datetime) else value

    def __getitem__(self, key):
        return self.values[self.names.index(key)] if isinstance(key, str) else self.values[key]

    def __iter__(self): return iter(self.values)
    def __len__(self): return len(self.values)
    def keys(self): return self.names


class Result:
    def __init__(self, names=(), rows=(), *, lastrowid=None, rowcount=0):
        self.rows = [Row(names, row) for row in rows]
        self.position, self.lastrowid, self.rowcount = 0, lastrowid, rowcount

    def fetchone(self):
        if self.position >= len(self.rows): return None
        row = self.rows[self.position]; self.position += 1
        return row

    def fetchall(self):
        rows = self.rows[self.position:]; self.position = len(self.rows)
        return rows

    def __iter__(self): return iter(self.fetchall())


class Connection:
    """Native MySQL reads/writes; SQLite is used only for explicit RAM previews."""
    def __init__(self, cfg: Config, *, readonly=False):
        self.cfg, self.raw, self.readonly = cfg, connect(cfg), readonly
        self.in_context, self.row_factory = False, None
        with self.raw.cursor() as cursor:
            cursor.execute('SELECT content FROM `' + cfg.table('state') + '` WHERE path=%s', ('metadata/schema',))
            row = cursor.fetchone()
        if not row:
            self.raw.close()
            raise sqlite3.OperationalError('MySQL 尚未完成迁移，禁止从空库开始运行')
        self.schema = json.loads(row[0])
        self.raw.commit()
        if readonly:
            with self.raw.cursor() as cursor: cursor.execute('SET SESSION TRANSACTION READ ONLY')

    def translate(self, sql: str) -> str:
        sql = re.sub(r"CAST\(\(julianday\('now'\)-julianday\((\w+)\)\)\*86400 AS INTEGER\)", r'TIMESTAMPDIFF(SECOND,\1,UTC_TIMESTAMP())', sql, flags=re.I)
        sql = re.sub(r"datetime\('now','-(\d+) day'\)", r'(UTC_TIMESTAMP() - INTERVAL \1 DAY)', sql, flags=re.I)
        tables = {name.lower(): self.cfg.table(name) for name in self.schema['tables']}
        tokens = re.findall(r"'(?:''|\\.|[^'])*'|\"(?:\"\"|\\.|[^\"])*\"|`[^`]*`|\b[A-Za-z_]\w*\b|\?|.", sql, flags=re.S)
        output = []
        for token in tokens:
            if token == '?': output.append('%s'); continue
            if token.startswith("'"):
                output.append(token.replace('%', '%%')); continue
            name = token[1:-1] if token.startswith(('"', '`')) else token
            if name.lower() in tables: token = '`' + tables[name.lower()] + '`'
            elif token.startswith('"'): token = '`' + name + '`'
            output.append(token.replace('%', '%%'))
        return ''.join(output)

    def execute(self, sql: str, params=()):
        import pymysql
        stripped = sql.strip()
        if re.match(r'PRAGMA (quick_check|integrity_check)', stripped, re.I):
            try:
                self.raw.ping(reconnect=False)
                return Result(['integrity_check'], [('ok',)])
            except pymysql.MySQLError as exc: raise sqlite3.OperationalError(str(exc)) from None
        if re.match(r'PRAGMA foreign_key_check', stripped, re.I): return Result()
        info = re.match(r'PRAGMA table_info\(["\x27]?(\w+)', stripped, re.I)
        if info:
            return Result(['cid', 'name', 'type', 'notnull', 'dflt_value', 'pk'], self.schema['tables'][info[1]]['columns'])
        if stripped.upper().startswith('PRAGMA'): return Result()
        if 'sqlite_master' in stripped:
            wanted = re.search(r"name='([^']+)'", stripped)
            names = [wanted[1]] if wanted and wanted[1] in self.schema['tables'] else list(self.schema['tables']) if not wanted else []
            return Result(['1'], [(1,) for _ in names])
        if stripped.upper().startswith('BEGIN'):
            if not self.in_context: self.raw.begin()
            return Result()
        if self.readonly and not re.match(r'^(SELECT|WITH|SHOW|EXPLAIN)\b', stripped, re.I):
            raise sqlite3.OperationalError('只读连接禁止写入')
        try:
            with self.raw.cursor() as cursor:
                cursor.execute(self.translate(sql), params)
                return Result([x[0] for x in cursor.description] if cursor.description else (),
                              cursor.fetchall() if cursor.description else (), lastrowid=cursor.lastrowid, rowcount=cursor.rowcount)
        except pymysql.MySQLError as exc:
            raise sqlite3.OperationalError(str(exc).replace(self.cfg.password, '[REDACTED]')) from None

    def commit(self): self.raw.commit()
    def rollback(self): self.raw.rollback()
    def close(self): self.raw.close()

    def __enter__(self):
        self.raw.begin(); self.in_context = True
        return self

    def __exit__(self, kind, *_):
        try: self.rollback() if kind else self.commit()
        finally: self.in_context = False

    def backup(self, target):
        target.commit()
        target.execute('PRAGMA foreign_keys=OFF')
        for name, definition in self.schema['tables'].items():
            target.execute(re.sub(r'^CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', definition['sql'], flags=re.I))
            target.execute('DELETE FROM "' + name + '"')
            cols = [x[1] for x in definition['columns']]
            rows = self.execute('SELECT * FROM "' + name + '"').fetchall()
            if rows:
                target.executemany('INSERT INTO "' + name + '" (' + ','.join('"'+c+'"' for c in cols) + ') VALUES(' + ','.join('?' for _ in cols) + ')', [tuple(row) for row in rows])
        target.execute('PRAGMA user_version=' + str(self.schema['version']))
        target.commit()


def create_schema(cfg: Config, source: sqlite3.Connection) -> dict:
    db = connect(cfg, autocommit=True, database=False)
    with db.cursor() as cursor:
        cursor.execute('CREATE DATABASE IF NOT EXISTS `' + cfg.database + '` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin')
    db.close()
    db = connect(cfg, autocommit=True)
    tables = {}
    try:
        with db.cursor() as cursor:
            cursor.execute('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s', (cfg.database,))
            existing = {row[0] for row in cursor.fetchall()}
            if any(name == cfg.prefix or name.startswith(cfg.prefix + '_') for name in existing):
                raise ValueError('目标前缀已存在，禁止覆盖；请先核对迁移状态')
            for name, sql in source.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY CASE WHEN name='resources' THEN 0 ELSE 1 END,name"):
                columns = [tuple(x) for x in source.execute('PRAGMA table_info("'+name+'")')]
                unique = []
                indexed = set()
                for index in source.execute('PRAGMA index_list("'+name+'")'):
                    fields = [x[2] for x in source.execute('PRAGMA index_info("'+index[1]+'")')]
                    indexed.update(fields)
                    if index[2] and index[3] != 'pk': unique.append(fields)
                definitions = []
                pk = [x[1] for x in sorted(columns, key=lambda x:x[5]) if x[5]]
                for cid, col, kind, required, default, primary in columns:
                    automatic = primary and len(pk) == 1 and kind.upper() == 'INTEGER'
                    if kind.upper() == 'INTEGER': typ = 'BIGINT'
                    elif kind.upper() == 'REAL': typ = 'DOUBLE'
                    elif kind.upper() == 'BLOB': typ = 'LONGBLOB'
                    elif primary or col in indexed:
                        maximum = source.execute('SELECT max(length("'+col+'")) FROM "'+name+'"').fetchone()[0] or 0
                        if maximum > 191: raise ValueError('索引列过长：'+name+'.'+col)
                        typ = 'VARCHAR(191)'
                    elif default == 'CURRENT_TIMESTAMP' or col.endswith('_at'): typ = 'VARCHAR(40)'
                    else: typ = 'LONGTEXT'
                    text = '`'+col+'` '+typ + (' NOT NULL' if required or primary else ' NULL')
                    if automatic: text += ' AUTO_INCREMENT'
                    if default is not None:
                        if default == 'CURRENT_TIMESTAMP': text += " DEFAULT (DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%d %H:%i:%s'))"
                        elif typ == 'LONGTEXT': text += ' DEFAULT ('+default+')'
                        else: text += ' DEFAULT '+default
                    definitions.append(text)
                if pk: definitions.append('PRIMARY KEY ('+','.join('`'+x+'`' for x in pk)+')')
                for i, fields in enumerate(unique): definitions.append('UNIQUE KEY `uq_'+str(i)+'` ('+','.join('`'+x+'`' for x in fields)+')')
                if name == 'ai_judgments': definitions += ["CHECK (`mode`='shadow')", 'CHECK (`applied`=0)']
                cursor.execute('CREATE TABLE `'+cfg.table(name)+'` ('+','.join(definitions)+') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin')
                for index in source.execute('PRAGMA index_list("'+name+'")'):
                    if index[2]: continue
                    fields = [x[2] for x in source.execute('PRAGMA index_info("'+index[1]+'")')]
                    cursor.execute('CREATE INDEX `'+index[1]+'` ON `'+cfg.table(name)+'` ('+','.join('`'+x+'`' for x in fields)+')')
                tables[name] = {'sql': sql, 'columns': columns}
            cursor.execute('CREATE TABLE `'+cfg.table('state')+'` (path VARCHAR(512) COLLATE utf8mb4_0900_bin PRIMARY KEY,content LONGBLOB NOT NULL,sha256 CHAR(64) NOT NULL,updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)) ENGINE=InnoDB')
            cursor.execute('CREATE TABLE `'+cfg.table('logs')+'` (id BIGINT PRIMARY KEY AUTO_INCREMENT,path VARCHAR(512) COLLATE utf8mb4_0900_bin NOT NULL,content LONGTEXT NOT NULL,created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),INDEX(path(191),id),INDEX(created_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4')
        return {'version': source.execute('PRAGMA user_version').fetchone()[0], 'tables': tables}
    finally: db.close()


def import_rows(cfg: Config, source: sqlite3.Connection, schema: dict) -> dict:
    db = connect(cfg)
    counts = {}
    try:
        with db.cursor() as cursor:
            for name, definition in schema['tables'].items():
                columns = [x[1] for x in definition['columns']]
                sql = 'INSERT INTO `'+cfg.table(name)+'` ('+','.join('`'+x+'`' for x in columns)+') VALUES ('+','.join('%s' for _ in columns)+')'
                rows = source.execute('SELECT * FROM "'+name+'"').fetchall()
                for offset in range(0,len(rows),200): cursor.executemany(sql,rows[offset:offset+200])
                counts[name] = len(rows)
            payload = json.dumps(schema,ensure_ascii=False).encode()
            cursor.execute('INSERT INTO `'+cfg.table('state')+'` (path,content,sha256) VALUES(%s,%s,%s)', ('metadata/schema',payload,hashlib.sha256(payload).hexdigest()))
        db.commit()
        with db.cursor() as cursor:
            for name in schema['tables']:
                for fk in source.execute('PRAGMA foreign_key_list("'+name+'")'):
                    cursor.execute('ALTER TABLE `'+cfg.table(name)+'` ADD FOREIGN KEY (`'+fk[3]+'`) REFERENCES `'+cfg.table(fk[2])+'` (`'+fk[4]+'`)')
        return counts
    except Exception:
        db.rollback(); raise
    finally: db.close()
