"""Local MP3 ingestion; independent of the forum publishing workflow."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
import uuid

import main as app


AUDIO_SUFFIXES = {'.mp3', '.flac', '.wav', '.ape', '.alac', '.m4a', '.aac', '.ogg', '.opus'}


def song_identity(name: str) -> tuple[str, str] | None:
    # Require an unambiguous separator. Do not infer aliases or discard version text.
    name = unicodedata.normalize('NFKC', name)
    parts = name.split(' - ')
    if len(parts) != 2 or not all(part.strip() for part in parts): return None
    artist, song = parts
    song = re.sub(r'(?i)\s*\[(?:MP3(?:[- ]?\d{2,3}K)?|FLAC|\d{2,3}\s*K(?:BPS)?)\]\s*$', '', song)
    # Preserve punctuation to avoid collapsing distinct titles; normalize spaces and case only.
    key = lambda value: re.sub(r'\s+', ' ', value).strip().casefold()
    return key(artist), key(song)


def is_audio(item: dict) -> bool:
    return item.get('category') != 0 and Path(str(item.get('filename', ''))).suffix.lower() in AUDIO_SUFFIXES


def remote_identity(item: dict, folder_name: str) -> tuple[str, str] | None:
    stem = Path(str(item['filename'])).stem
    explicit = song_identity(stem)
    if explicit: return explicit
    folder = song_identity(folder_name)
    if folder:
        inferred = song_identity(folder[0] + ' - ' + stem)
        if inferred == folder: return folder
    return None


def scan_remote(quark: app.Quark, media_fid: str) -> tuple[set, dict, dict]:
    existing, directories, contents = set(), {}, {}
    pending = [(media_fid, '', True)]
    visited = set()
    while pending:
        fid, folder_name, top = pending.pop()
        fingerprint = fid.rsplit('|', 1)[-1] or fid
        if fingerprint in visited: raise app.FlowError('网盘目录重复或循环，停止上传')
        visited.add(fingerprint)
        items = quark.browse(fid)
        contents[fid] = items
        for item in items:
            if item.get('category') == 0:
                key = song_identity(item['filename'])
                if top and key: directories.setdefault(key, []).append(item)
                pending.append((str(item['fid']), item['filename'], False))
            elif is_audio(item):
                key = remote_identity(item, folder_name)
                if key:
                    # Missing size is not proof of a valid upload; fail closed instead of duplicating.
                    if item.get('size') is None: raise app.FlowError('网盘音频缺少大小信息，无法安全去重')
                    if int(item['size']) > 0: existing.add(key)
    return existing, directories, contents


def file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def upload_workflow(args: argparse.Namespace, store: app.Store, log: app.JsonLogger,
                    quark: app.Quark, notify: app.Feishu) -> dict:
    source = app.ROOT / 'upload-files'
    if not source.is_dir(): raise app.FlowError('缺少固定上传目录 upload-files')
    files = sorted((p for p in source.iterdir() if p.suffix.lower() == '.mp3'), key=lambda p: p.name.casefold())
    summary = {'scanned': len(files), 'uploaded': 0, 'existing': 0, 'planned': 0, 'failed': 0}
    if not files: return summary
    media = quark.media_directory()
    existing, directories, contents = scan_remote(quark, str(media['fid']))
    planned_keys = set()
    for path in files:
        key = song_identity(path.stem)
        key_json = json.dumps(key, ensure_ascii=False) if key else None
        cursor = store.db.execute("INSERT INTO uploads(run_id,local_path,song_key,status) VALUES(?,?,?,'CHECKING')",
                                  (log.run_id, str(path), key_json))
        row_id = cursor.lastrowid
        store.db.commit()

        def finish(status: str, detail: str = '', **values) -> None:
            allowed = {'sha256', 'size', 'probe_json', 'parent_fid', 'remote_fid'}
            assert set(values) <= allowed
            assignments = ''.join(f', {field}=?' for field in values)
            store.db.execute(f"UPDATE uploads SET status=?,detail=?,finished_at=CURRENT_TIMESTAMP{assignments} WHERE id=?",
                             (status, app.redact(detail), *values.values(), row_id))
            store.db.commit()
            log.event('WARNING' if status in {'FAILED', 'UNKNOWN'} else 'INFO', 'upload_item',
                      upload_id=row_id, file=path.name, status=status, detail=detail)

        attempted = False
        try:
            if path.is_symlink() or not path.is_file() or path.resolve().parent != source.resolve():
                raise app.FlowError('仅处理上传目录中的普通 MP3 文件，不跟随链接')
            if not key or not all(key): raise app.FlowError('文件名须为 歌手 - 歌名.mp3，歧义名称需人工处理')
            signature = file_signature(path)
            if signature[0] <= 0: raise app.FlowError('空文件不能上传')
            if time.time_ns() - signature[1] < 60 * 1_000_000_000:
                raise app.FlowError('文件刚刚修改，等待下次任务确认拷贝完成')
            probe = app.probe_local_mp3(path)
            with path.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if signature != file_signature(path): raise app.FlowError('检查期间文件发生变化，等待下次任务')
            store.db.execute('UPDATE uploads SET sha256=?,size=?,probe_json=? WHERE id=?',
                             (digest, signature[0], json.dumps(probe, ensure_ascii=False), row_id))
            store.db.commit()
            if key in existing:
                finish('SKIPPED_EXISTS', '云端已有该歌曲音频，不补充格式或码率')
                # A later complete cloud scan reconciles uncertain previous uploads.
                store.db.execute("UPDATE uploads SET status='RECONCILED',finished_at=CURRENT_TIMESTAMP WHERE song_key=? AND status IN ('UPLOADING','UNKNOWN')", (key_json,))
                store.db.commit()
                summary['existing'] += 1
                continue
            if key in planned_keys:
                finish('SKIPPED_LOCAL_DUPLICATE', '本次已规划同一歌曲')
                summary['existing'] += 1
                continue
            if store.db.execute("SELECT 1 FROM uploads WHERE song_key=? AND status IN ('UPLOADING','UNKNOWN')", (key_json,)).fetchone():
                raise app.FlowError('上次上传结果不明且尚未在网盘找到，需人工核实后处理，禁止自动重传')
            matches = directories.get(key, [])
            if len(matches) > 1: raise app.FlowError('存在多个同名歌曲目录，需人工确认')
            folder = matches[0] if matches else None
            if folder:
                children = contents[str(folder['fid'])]
                if any(is_audio(item) or item['filename'].casefold() == path.name.casefold() or item.get('category') == 0 for item in children):
                    raise app.FlowError('同名目录有不匹配音频、子目录或文件冲突，不覆盖')
            if not args.apply:
                finish('PLANNED', '将上传到 Share/Media/' + path.stem)
                planned_keys.add(key)
                summary['planned'] += 1
                continue
            # Reuse only a unique matching directory; create-folder is server-idempotent.
            parent = str(folder['fid']) if folder else quark.create_folder(str(media['fid']), path.stem)
            children = quark.browse(parent)
            if any(is_audio(item) or item['filename'].casefold() == path.name.casefold() or item.get('category') == 0 for item in children):
                raise app.FlowError('上传前目录内容已变化，留待下次重新去重')
            if signature != file_signature(path): raise app.FlowError('上传前本地文件已变化')
            store.db.execute("UPDATE uploads SET status='UPLOADING',parent_fid=? WHERE id=?", (parent, row_id))
            store.db.commit()
            attempted = True
            rows = quark.upload_file(path, parent)
            log.event('INFO', 'upload_response', upload_id=row_id, response=rows)
            if signature != file_signature(path): raise app.FlowError('上传过程中本地文件发生变化，需核实')
            uploaded = [item for item in quark.browse(parent) if item['filename'] == path.name and item.get('category') != 0 and item.get('size') == signature[0]]
            if len(uploaded) != 1: raise app.FlowError('上传后文件名或大小未通过核验')
            finish('UPLOADED', '上传并核验完成', parent_fid=parent, remote_fid=str(uploaded[0]['fid']))
            existing.add(key)
            summary['uploaded'] += 1
        except (app.FlowError, OSError, ValueError, TypeError) as exc:
            finish('UNKNOWN' if attempted else 'FAILED', str(exc))
            summary['failed'] += 1
            notify.alert('音乐上传异常：' + path.name, str(exc))
    return summary


def run_upload(args: argparse.Namespace) -> int:
    with app.RunLock():
        settings = app.Settings.load()
        store = app.Store()
        run_id = uuid.uuid4().hex
        log = app.JsonLogger(run_id, store)
        notify = app.Feishu(settings, log, enabled=args.apply)
        store.start_run(run_id, 'upload' if args.apply else 'upload_dry_run')
        try:
            quark = app.Quark(settings, log, args.session_input, args.session_id)
            summary = upload_workflow(args, store, log, quark, notify)
            status = 'PARTIAL_FAILURE' if summary['failed'] else 'SUCCESS'
            store.finish_run(run_id, status, json.dumps(summary, ensure_ascii=False))
            log.event('INFO', 'upload_run_finished', **summary)
            return int(bool(summary['failed']))
        except Exception as exc:
            log.event('ERROR', 'upload_run_failed', error=str(exc))
            store.finish_run(run_id, 'FAILED', str(app.redact(str(exc))))
            notify.alert('音乐上传任务失败', str(exc))
            return 1
        except KeyboardInterrupt:
            store.finish_run(run_id, 'ABORTED', '用户中断；上传中的项目须先核实')
            return 130
        finally:
            store.close()
