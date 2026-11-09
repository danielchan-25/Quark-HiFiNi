"""Explicit, resumable import with row/content verification; never enables production."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import mysql_storage as mysql


def checksum(rows):
    encoded=sorted(json.dumps(list(row),ensure_ascii=False,separators=(',',':')) for row in rows)
    value=hashlib.sha256()
    for row in encoded:value.update(row.encode());value.update(b'\n')
    return value.hexdigest()


def verify(cfg,source,schema):
    db=mysql.connect(cfg,autocommit=True);report={}
    try:
        with db.cursor() as cursor:
            for name,definition in schema['tables'].items():
                columns=[x[1] for x in definition['columns']]
                cursor.execute('SELECT '+','.join('`'+x+'`' for x in columns)+' FROM `'+cfg.table(name)+'`')
                actual=cursor.fetchall();expected=source.execute('SELECT '+','.join('"'+x+'"' for x in columns)+' FROM "'+name+'"').fetchall()
                a,b=checksum(expected),checksum(actual)
                if len(expected)!=len(actual) or a!=b:raise ValueError('逐行数据校验失败：'+name)
                report[name]={'rows':len(actual),'sha256':a,'verified':True}
            cursor.execute('SELECT COUNT(*) FROM `'+cfg.table('events')+'` e LEFT JOIN `'+cfg.table('resources')+'` r ON r.id=e.resource_id WHERE e.resource_id IS NOT NULL AND r.id IS NULL')
            if cursor.fetchone()[0]:raise ValueError('事件存在孤立歌曲引用')
        return report
    finally:db.close()


def import_files(cfg,root):
    db=mysql.connect(cfg);report={}
    try:
        with db.cursor() as cursor:
            for base in ('data','logs'):
                for path in sorted((root/base).rglob('*')):
                    if not path.is_file() or path.is_symlink() or path.suffix in ('.lock','.tmp','.db-wal','.db-shm') or path.name.startswith('quark-hifini.db') or 'chrome-profile' in path.parts:continue
                    content=path.read_bytes();key=str(path.relative_to(root));digest=hashlib.sha256(content).hexdigest()
                    cursor.execute('INSERT INTO `'+cfg.table('state')+'` (path,content,sha256,updated_at) VALUES(%s,%s,%s,%s) ON DUPLICATE KEY UPDATE content=VALUES(content),sha256=VALUES(sha256),updated_at=VALUES(updated_at)',(key,content,digest,datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).replace(tzinfo=None)))
                    cursor.execute('SELECT content FROM `'+cfg.table('state')+'` WHERE path=%s',(key,))
                    if hashlib.sha256(cursor.fetchone()[0]).hexdigest()!=digest:raise ValueError('文件导入校验失败：'+key)
                    report[key]={'bytes':len(content),'sha256':digest}
        db.commit();return report
    except Exception:db.rollback();raise
    finally:db.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-db',type=Path,required=True);p.add_argument('--source-root',type=Path,required=True);p.add_argument('--credentials',type=Path,required=True);p.add_argument('--report',type=Path,required=True);p.add_argument('--execute',action='store_true');args=p.parse_args()
    if not args.execute:print('Preview only; --execute imports into this application prefix and verifies every row.');return 0
    cfg=mysql.Config(**json.loads(args.credentials.read_text()));source=sqlite3.connect(args.source_db.resolve().as_uri()+'?mode=ro',uri=True)
    db=mysql.connect(cfg,autocommit=True,database=False)
    try:
        with db.cursor() as c:
            c.execute('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s',(cfg.database,));exists=bool(c.fetchone())
        schema=None
        if exists:
            with db.cursor() as c:
                c.execute('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s',(cfg.database,));tables={r[0] for r in c.fetchall()}
            if cfg.table('state') in tables:
                db.select_db(cfg.database)
                with db.cursor() as c:c.execute('SELECT content FROM `'+cfg.table('state')+'` WHERE path=%s',('metadata/schema',));row=c.fetchone();schema=json.loads(row[0]) if row else None
    finally:db.close()
    if schema is None:schema=mysql.create_schema(cfg,source);mysql.import_rows(cfg,source,schema)
    report={'database':cfg.database,'prefix':cfg.prefix,'tables':verify(cfg,source,schema),'files':import_files(cfg,args.source_root),'verified_at':datetime.now(timezone.utc).isoformat()}
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'tables':len(report['tables']),'rows':sum(x['rows'] for x in report['tables'].values()),'files':len(report['files']),'verified':True}))
    return 0


if __name__=='__main__':raise SystemExit(main())
