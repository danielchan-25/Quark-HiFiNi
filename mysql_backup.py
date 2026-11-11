"""Portable, verified MySQL snapshots of this application's tables only."""
from __future__ import annotations
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid

import mysql_storage as mysql
import runtime_store as runtime


def pack(value):
    if isinstance(value,bytes):return {'$bytes':base64.b64encode(value).decode()}
    if isinstance(value,datetime):return {'$datetime':value.isoformat()}
    return value


def unpack(value):
    if isinstance(value,dict) and '$bytes' in value:return base64.b64decode(value['$bytes'])
    if isinstance(value,dict) and '$datetime' in value:return datetime.fromisoformat(value['$datetime'])
    return value


def export(cfg, target: Path):
    db=mysql.connect(cfg);tables={}
    try:
        db.begin()
        with db.cursor() as cursor:
            cursor.execute('SELECT content FROM `'+cfg.table('state')+'` WHERE path=%s',('metadata/schema',))
            schema=json.loads(cursor.fetchone()[0])
            expected={cfg.table(name) for name in [*schema['tables'],'state','logs']}
            extra=set(schema.get('backup_tables', []))
            if not all(re.fullmatch(r'[A-Za-z0-9_]{1,64}', name) for name in extra):
                raise ValueError('附加备份表名无效')
            expected.update(extra)
            cursor.execute('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s',(cfg.database,))
            names=[r[0] for r in cursor.fetchall() if r[0] in expected]
            if set(names)!=expected:raise ValueError('本项目的数据表缺失，拒绝生成不完整备份')
            for name in sorted(names):
                cursor.execute('SHOW CREATE TABLE `'+name+'`');ddl=cursor.fetchone()[1]
                cursor.execute('SELECT * FROM `'+name+'`');columns=[x[0] for x in cursor.description];rows=cursor.fetchall()
                filename=name+'.jsonl';h=hashlib.sha256()
                with (target/filename).open('wb') as stream:
                    for row in rows:
                        encoded=(json.dumps([pack(x) for x in row],ensure_ascii=False,separators=(',',':'))+'\n').encode()
                        stream.write(encoded);h.update(encoded)
                tables[name]={'ddl':ddl,'columns':columns,'rows':len(rows),'file':filename,'sha256':h.hexdigest()}
        db.rollback()
    finally:db.close()
    metadata={'format':1,'database':cfg.database,'prefix':cfg.prefix,'tables':tables}
    (target/'mysql-metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
    return metadata


def verify_restore(cfg, directory: Path, metadata):
    """Restore to unique temporary tables, compare every row, then remove only those tables."""
    prefix=cfg.prefix+'_restore_'+uuid.uuid4().hex[:8]
    mapping={name:prefix+'_'+hashlib.sha256(name.encode()).hexdigest()[:12] for name in metadata['tables']}
    db=mysql.connect(cfg);created=[]
    try:
        with db.cursor() as cursor:
            cursor.execute('SET FOREIGN_KEY_CHECKS=0')
            for original,table in metadata['tables'].items():
                ddl=table['ddl']
                for old,new in sorted(mapping.items(),key=lambda x:len(x[0]),reverse=True):ddl=ddl.replace('`'+old+'`','`'+new+'`')
                ddl=re.sub(r'CONSTRAINT `([^`]+)`',lambda match:'CONSTRAINT `'+prefix+'_'+hashlib.sha256((original+match[1]).encode()).hexdigest()[:8]+'`',ddl)
                cursor.execute(ddl);created.append(mapping[original])
                content=(directory/table['file']).read_bytes()
                if hashlib.sha256(content).hexdigest()!=table['sha256']:raise ValueError('备份行数据校验和不一致')
                rows=[[unpack(x) for x in json.loads(line)] for line in content.splitlines()]
                if rows:
                    sql='INSERT INTO `'+mapping[original]+'` ('+','.join('`'+x+'`' for x in table['columns'])+') VALUES ('+','.join('%s' for _ in table['columns'])+')'
                    for i in range(0,len(rows),200):cursor.executemany(sql,rows[i:i+200])
                cursor.execute('SELECT * FROM `'+mapping[original]+'`')
                actual=cursor.fetchall()
                canonical=lambda row:json.dumps([pack(x) for x in row],ensure_ascii=False,sort_keys=True,separators=(',',':'))
                if sorted(map(canonical,actual))!=sorted(map(canonical,rows)):raise ValueError('MySQL 隔离恢复逐行校验失败')
            cursor.execute('SET FOREIGN_KEY_CHECKS=1')
        db.rollback()
        return {'tables':len(created),'rows':sum(x['rows'] for x in metadata['tables'].values()),'verified':True}
    finally:
        try:
            with db.cursor() as cursor:
                cursor.execute('SET FOREIGN_KEY_CHECKS=0')
                for name in reversed(created):
                    assert name.startswith(prefix)
                    cursor.execute('DROP TABLE `'+name+'`')
                cursor.execute('SET FOREIGN_KEY_CHECKS=1')
            db.commit()
        finally:db.close()


def perform(root, destination, records, retention_days=14):
    import backup
    cfg=mysql.config(root)
    with runtime.lock(root,'backup',root/'data/backup/job.lock'):
        at=datetime.now(timezone(timedelta(hours=8)));identifier=at.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
        filename='quark-hifini-mysql-'+identifier+'.tar.gz';state=root/'data/backup/last-backup.json'
        previous=runtime.read_json(state,{})
        result={'started_at':at.isoformat(),'status':'RUNNING','archive':str(destination/filename),
                'last_success_at':previous.get('last_success_at'),'retention_days':retention_days,'format':'mysql'}
        runtime.write_json(state,result)
        try:
            if destination.is_relative_to(Path('/mnt/data')) and not Path('/mnt/data').is_mount():raise OSError('备份网络存储未挂载')
            destination.mkdir(parents=True,exist_ok=True);record=records/identifier;record.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='quark-hifini-mysql-backup-') as directory:
                stage=Path(directory)/'snapshot';stage.mkdir();metadata=export(cfg,stage)
                result['restore_verification']=verify_restore(cfg,stage,metadata)
                result['tables']={name:row['rows'] for name,row in metadata['tables'].items()}
                shutil.copy2(root/'.env',stage/'.env')
                files=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=root).decode().split('\0')
                for name in files:
                    if not name:continue
                    path=root/name
                    if not path.is_file() or path.is_symlink():continue
                    out=stage/'source'/name;out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,out)
                for path in (Path.home()/'.config/systemd/user').glob('quark-hifini-*'):
                    if path.is_file() and not path.is_symlink():
                        out=stage/'systemd'/path.name;out.parent.mkdir(exist_ok=True);shutil.copy2(path,out)
                if 'panel_state' in metadata['tables']:
                    panel=Path('/root/quark-panel')
                    for path in panel.rglob('*'):
                        relative=path.relative_to(panel)
                        if any(part in ('.venv','.git','data','__pycache__') for part in relative.parts):continue
                        if not path.is_file() or path.is_symlink() or path.suffix=='.log':continue
                        out=stage/'panel-source'/relative;out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,out)
                    service=Path('/etc/systemd/system/quark-panel.service')
                    if service.is_file():
                        out=stage/'systemd'/service.name;out.parent.mkdir(exist_ok=True);shutil.copy2(service,out)
                result['git_commit']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
                manifest={str(f.relative_to(stage)):backup.digest(f) for f in stage.rglob('*') if f.is_file()}
                archive=Path(directory)/filename
                with tarfile.open(archive,'w:gz') as tar:
                    for name in sorted(manifest):tar.add(stage/name,arcname=name,recursive=False)
                backup.verify(archive,manifest)
                result['sha256']=backup.digest(archive)
                incoming=destination/('.'+filename+'.partial');shutil.copy2(archive,incoming)
                if backup.digest(incoming)!=result['sha256']:raise ValueError('备份传输校验失败')
                incoming.replace(destination/filename)
                (destination/(filename+'.sha256')).write_text(result['sha256']+'  '+filename+'\n')
                backup.atomic_json(record/'manifest.json',manifest)
            result.update(status='SUCCESS',last_success_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),finished_at=datetime.now(timezone(timedelta(hours=8))).isoformat())
            runtime.write_json(state,result);backup.atomic_json(record/'backup.json',result)
            for path in destination.glob('quark-hifini-*.tar.gz'):
                if not re.fullmatch(r'quark-hifini-(state|mysql)-\d{8}-\d{6}-[a-f0-9]{8}\.tar\.gz',path.name):continue
                if path.stat().st_mtime >= (at-timedelta(days=retention_days)).timestamp():continue
                checksum=path.with_name(path.name+'.sha256')
                if checksum.is_file() and checksum.read_text().split()[0]==backup.digest(path):path.unlink();checksum.unlink()
            return result
        except Exception as exc:
            result.update(status='FAILED',error_type=type(exc).__name__,error=str(exc).replace(cfg.password,'[REDACTED]'))
            runtime.write_json(state,result)
            raise
