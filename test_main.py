"""Offline regression suite. Network, browsers and external writes are mocked."""
import argparse
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import main as app


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(app, "ROOT", self.root))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(app.socket, "create_connection", side_effect=AssertionError("network forbidden")))
        self.store = app.Store()
        self.addCleanup(self.store.close)
        self.store.start_run("test", "dry_run")
        self.log = app.JsonLogger("test", self.store)
        self.settings = app.Settings("http://127.0.0.1:9501", self.root, self.root / "bash.exe", None, None, None, None)
        self.folder = {"fid": "folder1", "filename": "Artist - Song", "category": 0}

    def resource(self):
        return self.store.upsert_resource(self.folder)

    def test_linux_defaults_resolve_native_tools_and_home(self):
        def which(name):
            return {'bash': '/usr/bin/bash', 'curl': '/usr/bin/curl', 'ffprobe': '/usr/bin/ffprobe'}.get(name)
        with patch.dict(os.environ, {}, clear=True), patch.object(app, 'load_dotenv'), patch.object(app, 'IS_WINDOWS', False), patch.object(app.shutil, 'which', side_effect=which), patch.object(app.Path, 'home', return_value=self.root):
            settings = app.Settings.load()
            self.assertEqual(settings.quark_dir, self.root / '.codex' / 'skills' / 'quarkclouddrive')
            self.assertEqual(settings.git_bash, Path('/usr/bin/bash'))
            self.assertEqual(app.curl_binary(), 'curl')
            self.assertEqual(app.ffprobe_path(), '/usr/bin/ffprobe')

    def test_beijing_quota_uses_utc_boundary_not_host_timezone(self):
        row = self.resource()
        clock = Mock()
        clock.now.return_value = datetime(2026, 10, 28, 0, 15, tzinfo=timezone(timedelta(hours=8)))
        with patch.object(app, 'datetime', clock):
            self.assertEqual(app.beijing_day_bounds(), ('2026-10-27 16:00:00', '2026-10-28 16:00:00'))
            self.store.db.execute("UPDATE resources SET submitted_at='2026-10-27 15:59:59' WHERE id=?", (row['id'],))
            self.assertEqual(self.store.submitted_today(), 0)
            self.store.db.execute("UPDATE resources SET submitted_at='2026-10-27 16:00:00' WHERE id=?", (row['id'],))
            self.assertEqual(self.store.submitted_today(), 1)

    def test_linux_feishu_command_omits_windows_tls_option(self):
        result = subprocess.CompletedProcess([], 0, '{"code":0}', '')
        with patch.object(app, 'IS_WINDOWS', False), patch.dict(os.environ, {}, clear=True), patch.object(app, 'execute', return_value=result) as send:
            self.assertTrue(app.Feishu(self.alert_settings(), self.log).alert('test', 'detail'))
        self.assertEqual(send.call_args.args[0][0], 'curl')
        self.assertNotIn('--ssl-no-revoke', send.call_args.args[0])

    def workflow(self, publish=False, folders=None, submit_error=None, draft_error=None):
        quark = Mock()
        quark.media_folders.return_value = folders if folders is not None else [self.folder]
        quark.browse.return_value = [{"fid": "audio1", "filename": "Artist - Song.flac"}]
        quark.public_share.return_value = ("https://pan.quark.cn/s/test", None)
        hifini = Mock()
        hifini.duplicate.return_value = None
        hifini.audit_thread.return_value = "APPROVED"
        hifini.submit_and_verify.return_value = {"url": "https://www.hifini.net/thread-123.htm", "pending": True}
        if submit_error: hifini.submit_and_verify.side_effect = submit_error
        if draft_error: hifini.draft.side_effect = draft_error
        self.stack.enter_context(patch.object(app.Settings, "load", return_value=self.settings))
        self.stack.enter_context(patch.object(app, "Quark", return_value=quark))
        self.stack.enter_context(patch.object(app, "HiFiNi", return_value=hifini))
        self.stack.enter_context(patch.object(app, "lyrics_html", return_value=("Artist", "Song", "<p>Lyrics</p><p>&nbsp;</p><p>&nbsp;</p>")))
        self.stack.enter_context(patch.object(app, "execute", side_effect=AssertionError("external process forbidden")))
        result = app.run(argparse.Namespace(publish=publish, dry_run=not publish, session_input=None, session_id=None))
        return result, quark, hifini

    def test_logs_are_in_database_and_redacted(self):
        with patch.dict(os.environ, {"HIFINI_PASSWORD": "test-password-long"}):
            self.log.event("ERROR", "failure", error="test-password-long", cookie="secret-cookie")
        data = self.store.db.execute("SELECT detail FROM events ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertNotIn("test-password-long", data)
        self.assertNotIn("secret-cookie", data)
        self.assertFalse((self.root / "logs").exists())

    def test_legacy_log_import_is_idempotent_and_handles_invalid_line(self):
        logs = self.root / "logs"; logs.mkdir()
        (logs / "old.jsonl").write_text('{"run_id":"old","event":"started"}\nBROKEN\n', encoding="utf-8")
        self.assertEqual(self.store.import_logs(), 2)
        self.assertEqual(self.store.import_logs(), 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)

    def test_schema_upgrade_backs_up_existing_database(self):
        path = self.root / "old.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE resources(id INTEGER PRIMARY KEY, folder_fid TEXT UNIQUE NOT NULL, folder_name TEXT NOT NULL, artist TEXT, song TEXT, category TEXT, share_url TEXT, status TEXT DEFAULT 'DISCOVERED', failure_count INTEGER DEFAULT 0, hifini_url TEXT, detail TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO resources(folder_fid,folder_name,status) VALUES('x','Artist - Song','DELETED')")
        db.commit(); db.close()
        migrated = app.Store(path)
        try:
            self.assertEqual(migrated.db.execute("SELECT status FROM resources").fetchone()[0], "DELETED")
            self.assertEqual(len(list(self.root.glob(f"old.db.before-v{app.SCHEMA_VERSION}-*.db"))), 1)
            self.assertEqual(migrated.db.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally: migrated.close()

    def test_renamed_folder_with_same_fid_keeps_state(self):
        row = self.resource(); self.store.update(row["id"], "DELETED")
        renamed = self.store.upsert_resource(dict(self.folder, filename="Artist - Renamed"))
        self.assertEqual(renamed["id"], row["id"])
        self.assertEqual(renamed["status"], "DELETED")

    def test_session_fid_change_keeps_state(self):
        row = self.resource(); self.store.update(row["id"], "PUBLISHED")
        new = self.store.upsert_resource(dict(self.folder, fid="new-session"))
        self.assertEqual(new["id"], row["id"])
        self.assertEqual(new["status"], "PUBLISHED")

    def test_failure_stages_do_not_accumulate_together(self):
        row = self.resource()
        self.assertEqual(self.store.fail(row["id"], "x", "baike"), 1)
        self.assertEqual(self.store.fail(row["id"], "x", "baike"), 2)
        self.assertEqual(self.store.fail(row["id"], "x", "bitrate"), 1)

    def test_audit_does_not_invent_submission_time(self):
        row = self.resource()
        self.store.audit(row["id"], "APPROVED", "known historical post")
        self.assertEqual(self.store.submitted_today(), 0)
        self.assertIsNone(self.store.db.execute("SELECT submitted_at FROM resources").fetchone()[0])

    def test_submission_reservation_blocks_second_submission(self):
        row = self.resource()
        self.store.begin_submission("test", row["id"], "title")
        with self.assertRaises(app.FlowError): self.store.begin_submission("test", row["id"], "title")
        self.assertEqual(self.store.submitted_today(), 1)

    def test_old_unknown_only_blocks_its_own_song(self):
        row = self.resource()
        attempt = self.store.begin_submission('test', row['id'], 'old title')
        self.store.db.execute("UPDATE submission_attempts SET created_at=datetime('now','-2 days') WHERE id=?", (attempt,))
        self.store.db.commit()
        with self.assertRaisesRegex(app.FlowError, '该歌曲'):
            self.store.begin_submission('test', row['id'], 'old title')
        other = self.store.upsert_resource({'fid': 'other', 'filename': 'Artist - Other'})
        self.store.begin_submission('test', other['id'], 'other title')
        self.assertEqual(self.store.submitted_today(), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM submission_attempts WHERE status='UNKNOWN'").fetchone()[0], 2)

    def test_unknown_still_consumes_same_day_quota_for_other_songs(self):
        row = self.resource()
        self.store.begin_submission('test', row['id'], 'title')
        other = self.store.upsert_resource({'fid': 'other', 'filename': 'Artist - Other'})
        with self.assertRaisesRegex(app.FlowError, '每日发布上限'):
            self.store.begin_submission('test', other['id'], 'other title')

    def test_confirmed_submission_counts_once(self):
        row = self.resource()
        self.store.begin_submission("test", row["id"], "title")
        self.store.mark_submitted(row["id"], "https://www.hifini.net/thread-1.htm")
        self.assertEqual(self.store.submitted_today(), 1)
        self.assertEqual(self.store.db.execute("SELECT status FROM submission_attempts").fetchone()[0], "CONFIRMED")

    def test_explicit_title_rejection_releases_daily_slot(self):
        row = self.resource()
        attempt = self.store.begin_submission("test", row["id"], "title")
        self.store.reject_submission(attempt)
        self.assertEqual(self.store.submitted_today(), 0)

    def test_stale_runs_recovered_without_erasing_unknown_submission(self):
        row = self.resource(); self.store.begin_submission("test", row["id"], "title")
        self.store.start_run("new", "dry_run")
        self.assertEqual(self.store.db.execute("SELECT status FROM runs WHERE id='test'").fetchone()[0], "ABORTED")
        self.assertEqual(self.store.db.execute("SELECT status FROM submission_attempts").fetchone()[0], "UNKNOWN")

    def test_run_lock_rejects_concurrent_runner(self):
        with app.RunLock():
            with self.assertRaises(app.FlowError):
                with app.RunLock(): pass
        with app.RunLock(): pass

    def test_alert_suppressed_in_dry_run(self):
        with patch.object(app, "execute", side_effect=AssertionError("must not send")):
            self.assertFalse(app.Feishu(self.settings, self.log, enabled=False).alert("test", "detail"))
        self.assertEqual(self.store.db.execute("SELECT status FROM alerts").fetchone()[0], "SUPPRESSED")

    def alert_settings(self):
        return app.Settings("http://127.0.0.1:9501", self.root, self.root / "bash.exe", "https://example.invalid/hook", None, None, None)

    def test_alert_timeout_does_not_raise(self):
        with patch.object(app, "execute", side_effect=app.FlowError("timeout")), patch.object(app.time, "sleep"):
            self.assertFalse(app.Feishu(self.alert_settings(), self.log).alert("error", "detail"))
        row = self.store.db.execute("SELECT * FROM alerts").fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("UNKNOWN", 1))

    def test_alert_non_json_not_reported_as_success(self):
        result = subprocess.CompletedProcess([], 0, "<html>bad gateway</html>", "")
        with patch.object(app, "execute", return_value=result), patch.object(app.time, "sleep"):
            self.assertFalse(app.Feishu(self.alert_settings(), self.log).alert("error", "detail"))

    def test_alert_success_and_deduplication(self):
        result = subprocess.CompletedProcess([], 0, '{"code":0}', "")
        with patch.object(app, "execute", return_value=result) as send:
            notifier = app.Feishu(self.alert_settings(), self.log)
            self.assertTrue(notifier.alert("error", "detail"))
            self.assertTrue(notifier.alert("error", "detail"))
            send.assert_called_once()

    def test_failed_alert_is_retried_on_later_run(self):
        app.Feishu(self.settings, self.log).alert("error", "detail")
        with patch.object(app, "execute", return_value=subprocess.CompletedProcess([], 0, '{"code":0}', "")):
            app.Feishu(self.alert_settings(), self.log).retry_pending()
        self.assertEqual([r[0] for r in self.store.db.execute("SELECT status FROM alerts ORDER BY id")], ["RETRIED", "SENT"])

    def test_dry_run_has_no_share_publish_alert_or_business_state_mutation(self):
        code, quark, hifini = self.workflow()
        self.assertEqual(code, 0)
        quark.public_share.assert_not_called()
        hifini.submit_and_verify.assert_not_called()
        self.assertEqual(hifini.draft.call_args.args[4], app.PREVIEW_URL)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM drafts WHERE preview_only=1").fetchone()[0], 1)

    def test_dry_run_failure_does_not_increment_real_failure_count(self):
        row = self.resource()
        code, _, _ = self.workflow(draft_error=app.FlowError("editor failed"))
        self.assertEqual(code, 1)
        self.assertEqual(self.store.db.execute("SELECT failure_count FROM resources WHERE id=?", (row["id"],)).fetchone()[0], 0)

    def test_uncertain_submission_is_not_retried(self):
        self.stack.enter_context(patch.dict(os.environ, {'HIFINI_PASSWORD': 'private-test-password'}))
        code, _, hifini = self.workflow(True, submit_error=app.FlowError("connection lost after click: private-test-password"))
        self.assertEqual(code, 1)
        hifini.submit_and_verify.assert_called_once()
        self.assertNotIn('private-test-password', self.store.db.execute('SELECT detail FROM resources').fetchone()[0])
        self.assertEqual(self.store.db.execute("SELECT status FROM resources").fetchone()[0], "SUBMISSION_UNKNOWN")
        self.assertEqual(self.store.db.execute("SELECT status FROM runs WHERE mode='publish'").fetchone()[0], 'PENDING_VERIFICATION')
        # The pending attempt consumes today's quota; retrying never clicks again.
        self.assertEqual(app.run(argparse.Namespace(publish=True)), 0)
        hifini.submit_and_verify.assert_called_once()

    def test_next_day_continues_with_another_song_while_old_one_waits(self):
        row = self.resource()
        attempt = self.store.begin_submission('test', row['id'], 'old title')
        self.store.db.execute("UPDATE submission_attempts SET created_at=datetime('now','-2 days') WHERE id=?", (attempt,))
        self.store.db.commit()
        other = {'fid': 'other', 'filename': 'Artist - Other'}
        code, _, hifini = self.workflow(True, folders=[self.folder, other])
        self.assertEqual(code, 0)
        hifini.submit_and_verify.assert_called_once()
        self.assertEqual(self.store.db.execute('SELECT folder_name FROM drafts ORDER BY id DESC LIMIT 1').fetchone()[0], other['filename'])
        self.assertEqual(self.store.db.execute('SELECT status FROM submission_attempts WHERE id=?', (attempt,)).fetchone()[0], 'UNKNOWN')
        self.assertEqual(self.store.db.execute('SELECT status FROM resources WHERE id=?', (row['id'],)).fetchone()[0], 'SUBMISSION_UNKNOWN')
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM alerts WHERE title='歌曲待核实，请确认是否已发布'").fetchone()[0], 1)

    def test_old_reconciliation_error_does_not_block_other_songs(self):
        row = self.resource()
        attempt = self.store.begin_submission('test', row['id'], 'old title')
        self.store.db.execute("UPDATE submission_attempts SET created_at=datetime('now','-2 days') WHERE id=?", (attempt,))
        self.store.db.commit()
        _, quark, hifini = self.workflow(False, folders=[])
        quark.media_folders.return_value = [{'fid': 'other', 'filename': 'Artist - Other'}]
        def lookup(title, *args):
            if title == 'old title': raise app.FlowError('old post page unavailable')
            return None
        hifini.duplicate.side_effect = lookup
        self.assertEqual(app.run(argparse.Namespace(publish=True)), 0)
        hifini.submit_and_verify.assert_called_once()
        self.assertEqual(self.store.db.execute('SELECT status FROM submission_attempts WHERE id=?', (attempt,)).fetchone()[0], 'UNKNOWN')

    def test_preflight_reports_pending_without_blocking(self):
        row = self.resource()
        self.store.begin_submission('test', row['id'], 'title')
        self.settings.git_bash.touch()
        script = self.settings.quark_dir / 'scripts' / 'quark-drive.cjs'
        script.parent.mkdir(parents=True)
        script.touch()
        output = io.StringIO()
        with patch.object(app.Settings, 'load', return_value=self.settings), patch.object(app.shutil, 'which', return_value='/usr/bin/tool'), redirect_stdout(output):
            self.assertEqual(app.check_local(), 0)
        state = json.loads(output.getvalue())
        self.assertEqual(state['unresolved_submissions'], 1)
        self.assertFalse(state['unresolved_blocks_other_songs'])

    def test_publish_flow_persists_one_confirmed_attempt(self):
        code, quark, hifini = self.workflow(True)
        self.assertEqual(code, 0)
        quark.public_share.assert_called_once()
        hifini.submit_and_verify.assert_called_once()
        self.assertEqual(self.store.submitted_today(), 1)

    def test_explicit_site_rejection_is_not_retried_on_next_run(self):
        code, quark, hifini = self.workflow(True, submit_error=app.SubmissionRejectedError('HiFiNi 拒绝提交：素材需要人工处理'))
        self.assertEqual(code, 1)
        self.assertEqual(self.store.db.execute('SELECT status FROM resources').fetchone()[0], 'SKIPPED_MANUAL_REVIEW')
        self.assertEqual(self.store.db.execute('SELECT status FROM submission_attempts').fetchone()[0], 'REJECTED')
        self.assertEqual(app.run(argparse.Namespace(publish=True)),0)
        hifini.submit_and_verify.assert_called_once()

    def test_legacy_rejection_is_recognized_even_after_failure_counter_reset(self):
        row=self.resource()
        self.store.db.execute("INSERT INTO submission_attempts(run_id,resource_id,title,status,detail) VALUES('old',?,'title','REJECTED','HiFiNi 拒绝提交：素材需要人工处理')", (row['id'],))
        self.store.db.commit()
        code, quark, hifini = self.workflow(True)
        self.assertEqual(code,0)
        hifini.submit_and_verify.assert_not_called()
        quark.public_share.assert_not_called()
        self.assertEqual(self.store.db.execute('SELECT status FROM resources').fetchone()[0],'SKIPPED_MANUAL_REVIEW')

    def test_ambiguous_alert_is_never_retried_by_next_run(self):
        result=subprocess.CompletedProcess([],28,'','Operation timed out')
        with patch.object(app,'execute',return_value=result) as send:
            app.Feishu(self.alert_settings(),self.log).alert('uncertain','detail')
            app.Feishu(self.alert_settings(),self.log).retry_pending()
            app.Feishu(self.alert_settings(),self.log).alert('uncertain','detail')
            send.assert_called_once()

    def test_unknown_alert_cannot_be_manually_replayed(self):
        cursor=self.store.db.execute("INSERT INTO alerts(run_id,title,detail,fingerprint,status,attempts) VALUES('old','title','detail','f','UNKNOWN',1)")
        self.store.db.commit()
        with self.assertRaisesRegex(app.FlowError,'无法证明'):
            app.retry_alert(cursor.lastrowid)

    def test_daily_cap_prevents_scan_and_submit(self):
        row = self.resource(); self.store.mark_submitted(row["id"], "https://www.hifini.net/thread-1.htm")
        code, quark, hifini = self.workflow(True)
        self.assertEqual(code, 0)
        quark.media_folders.assert_not_called()
        hifini.submit_and_verify.assert_not_called()

    def test_manual_skip_does_not_keep_alerting(self):
        row = self.resource(); self.store.update(row["id"], "SKIPPED_MANUAL_REVIEW")
        code, quark, _ = self.workflow()
        self.assertEqual(code, 0)
        quark.browse.assert_not_called()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)

    def test_cleanup_failure_does_not_erase_success(self):
        code, _, hifini = self.workflow()
        hifini.close.side_effect = app.FlowError("tab closed")
        self.assertEqual(app.run(argparse.Namespace(publish=False)), 0)

    def browser(self):
        obj = app.HiFiNi.__new__(app.HiFiNi)
        obj.tab = Mock(); obj.log = self.log; obj.settings = self.settings
        return obj

    def test_audit_unknown_page_not_approved(self):
        browser = self.browser()
        browser._eval = Mock(return_value={"text": "service unavailable", "logged": True, "thread": False, "url": "https://www.hifini.net/thread-1.htm"})
        with self.assertRaises(app.FlowError): browser.audit_thread("https://www.hifini.net/thread-1.htm")

    def test_audit_logged_out_not_rejected(self):
        browser = self.browser()
        browser._eval = Mock(return_value={"text": "无权查看", "logged": False, "thread": False})
        with self.assertRaises(app.FlowError): browser.audit_thread("https://www.hifini.net/thread-1.htm")

    def test_audit_valid_article_approved(self):
        browser = self.browser()
        browser._eval = Mock(return_value={"text": "title lyrics", "logged": True, "thread": True, "url": "https://www.hifini.net/thread-1.htm"})
        self.assertEqual(browser.audit_thread("https://www.hifini.net/thread-1.htm"), "APPROVED")

    def test_duplicate_search_reads_later_pages(self):
        browser = self.browser()
        wanted = {"title": "Artist《Song》[FLAC]", "url": "https://www.hifini.net/thread-1.htm", "pending": False}
        browser._eval = Mock(side_effect=[
            json.dumps({"logged": True, "valid": True, "items": [], "next": "https://www.hifini.net/my-thread-2.htm"}),
            json.dumps({"logged": True, "valid": True, "items": [wanted], "next": None}),
        ])
        self.assertEqual(browser.duplicate("different", "Artist", "Song"), wanted)

    def test_duplicate_pagination_loop_fails_closed(self):
        browser = self.browser()
        browser._eval = Mock(return_value=json.dumps({"logged": True, "valid": True, "items": [], "next": "https://www.hifini.net/my-thread.htm"}))
        with self.assertRaises(app.FlowError): browser.my_threads()

    def test_share_network_failure_does_not_recreate_share(self):
        quark = app.Quark(self.settings, self.log)
        quark._run = Mock(side_effect=app.FlowError("network unavailable"))
        with self.assertRaises(app.FlowError): quark.share_is_valid("url", {"name"})

    def test_share_missing_is_invalid(self):
        quark = app.Quark(self.settings, self.log)
        quark._run = Mock(side_effect=app.FlowError("errno=41007 share not exist"))
        self.assertFalse(quark.share_is_valid("url", {"name"}))

    def test_quark_checks_install_once_per_workflow_and_update_notice_once(self):
        (self.root/'scripts').mkdir(exist_ok=True)
        for path in (self.root/'scripts/install.sh',self.root/'scripts/quark-drive.cjs',self.settings.git_bash):path.write_text('fixture')
        install=subprocess.CompletedProcess([],0,'{"type":"install_result","success":true,"updated":true}\n','')
        command=subprocess.CompletedProcess([],0,'{"type":"result","code":0}\n','')
        quark=app.Quark(self.settings,self.log,'original instruction','1793166560-d1e283')
        with patch.object(app,'execute',side_effect=[install,command,command]) as invoke:
            quark._run(['browse']);quark._run(['browse'])
        self.assertEqual(invoke.call_count,3)
        self.assertIn('--skill-updated',invoke.call_args_list[1].args[0])
        self.assertNotIn('--skill-updated',invoke.call_args_list[2].args[0])

    def test_share_extra_files_rejected(self):
        quark = app.Quark(self.settings, self.log)
        quark._run = Mock(return_value=[{"type": "result", "data": {"files": [{"filename": "wanted"}, {"filename": "extra"}]}}])
        self.assertFalse(quark.share_is_valid("url", {"wanted"}))

    def test_bitrate_cannot_use_container_rate(self):
        quark = Mock(); quark.read_file.return_value = self.root / "test.mp3"
        result = subprocess.CompletedProcess([], 0, '{"streams":[{}],"format":{"bit_rate":"320000"}}', "")
        with patch.object(app.shutil, "which", return_value="ffprobe"), patch.object(app, "execute", return_value=result):
            with self.assertRaises(app.BitrateProbeError): app.probe_mp3_bitrate(quark, {"fid": "x"}, self.log)

    def test_m4a_not_assumed_aac(self):
        with self.assertRaises(app.FlowError): app.audio_format_label([{"filename": "x.m4a"}])

    def test_special_version_underscores_recognized(self):
        self.assertIsNotNone(app.special_version_reason([{"filename": "Artist_Song_Live.mp3"}], "Artist - Song"))

    def test_substring_song_does_not_hide_different_tracks(self):
        self.assertIsNotNone(app.special_version_reason([{"filename": "Love.flac"}, {"filename": "Lovely.mp3"}], "Artist - Love"))

    def test_short_lyrics_are_not_discarded(self):
        browser = Mock()
        browser._eval.side_effect = ["[]", "[]", json.dumps({"h1": "Song", "intro": "歌手演唱", "lyrics": "Short lyric\nSecond line"})]
        artist, song, lyrics = app.lyrics_html(browser, "歌手 - Song")
        self.assertEqual(re.findall(r"<p>(.*?)</p>", lyrics), ["Short lyric", "Second line", "&nbsp;", "&nbsp;"])

    def test_lyrics_layout_rejects_multiple_lines_inside_one_paragraph(self):
        with self.assertRaises(app.FlowError):
            app.validate_lyrics_layout("<p>First line\nSecond line</p><p>&nbsp;</p><p>&nbsp;</p>")

    def test_latin_names_route_to_lrclib_and_verify_artist_and_title(self):
        self.assertTrue(app.is_latin_release("The Weeknd", "Snowchild"))
        self.assertFalse(app.is_latin_release("周杰伦", "晴天"))
        browser = Mock()
        with patch.object(app, "urlopen", return_value=io.BytesIO(json.dumps([
            {"artistName": "The Weeknd", "trackName": "Snowchild", "plainLyrics": "First line\nSecond line"}
        ]).encode())):
            artist, song, lyrics = app.lyrics_html(browser, "The Weeknd - Snowchild")
        self.assertEqual((artist, song), ("The Weeknd", "Snowchild"))
        self.assertIn("First line", lyrics)
        browser.log.event.assert_called_once()
        self.assertTrue(browser.log.event.call_args.kwargs["source_url"].startswith("https://lrclib.net/api/search?"))

    def test_lrclib_rejects_wrong_artist_even_when_title_matches(self):
        with patch.object(app, "urlopen", return_value=io.BytesIO(json.dumps([
            {"artistName": "Other Artist", "trackName": "Snowchild", "plainLyrics": "Wrong song"}
        ]).encode())):
            with self.assertRaises(app.LyricsLookupError): app.lrclib_lyrics("The Weeknd", "Snowchild")

    def test_identity_normalization_and_html_escape(self):
        self.assertEqual(app.normalize_identity("Rosé"), app.normalize_identity("ROSE"))
        self.assertIn("&lt;script&gt;", app.plain_lyrics_html("<script>\nline [1]"))
        self.assertNotIn("[1]", app.plain_lyrics_html("line [1]"))

    def test_variable_and_low_bitrate_are_not_inflated(self):
        self.assertEqual(app.mp3_bitrate_label(180), "MP3-180K")
        self.assertEqual(app.mp3_bitrate_label(64), "MP3-64K")
        with self.assertRaises(app.BitrateProbeError): app.mp3_bitrate_label(float("nan"))

    def test_execute_timeout_and_normal_completion(self):
        result = app.execute([sys.executable, "-c", "print('ok')"], timeout=5, operation="test")
        self.assertEqual(result.stdout.strip(), "ok")
        with self.assertRaisesRegex(app.FlowError, "超过 1 秒"):
            app.execute([sys.executable, "-c", "import time; time.sleep(20)"], timeout=1, operation="test")

    def test_manual_not_submitted_resolution(self):
        row = self.resource(); attempt = self.store.begin_submission("test", row["id"], "title")
        self.assertEqual(app.resolve_submission(attempt, None), 0)
        self.assertEqual(self.store.submitted_today(), 0)
        self.assertEqual(self.store.db.execute("SELECT status FROM resources").fetchone()[0], "DRAFT_READY")

    def test_manual_confirmed_resolution(self):
        row = self.resource(); attempt = self.store.begin_submission("test", row["id"], "title")
        self.assertEqual(app.resolve_submission(attempt, "https://www.hifini.net/thread-123.htm"), 0)
        self.assertEqual(self.store.submitted_today(), 1)
        with self.assertRaises(app.FlowError): app.resolve_submission(attempt, None)

    def test_manual_confirmation_without_url_preserves_original_submission_date(self):
        row = self.resource(); attempt = self.store.begin_submission('test', row['id'], 'title')
        self.store.db.execute("UPDATE submission_attempts SET created_at='2026-11-04 22:01:25' WHERE id=?", (attempt,))
        self.store.db.commit()
        self.assertEqual(app.cli(['resolve-submission', str(attempt), '--submitted']), 0)
        resource = self.store.db.execute('SELECT * FROM resources WHERE id=?', (row['id'],)).fetchone()
        self.assertEqual(resource['submitted_at'], '2026-11-04 22:01:25')
        self.assertEqual(resource['status'], 'SUBMITTED')
        self.assertIsNone(resource['hifini_url'])
        self.assertEqual(self.store.db.execute('SELECT status FROM submission_attempts WHERE id=?', (attempt,)).fetchone()[0], 'CONFIRMED')
        self.assertEqual(app.cli(['resolve-submission', str(attempt), '--url', 'https://www.hifini.net/thread-123.htm']), 0)
        resource = self.store.db.execute('SELECT * FROM resources WHERE id=?', (row['id'],)).fetchone()
        self.assertEqual(resource['hifini_url'], 'https://www.hifini.net/thread-123.htm')
        self.assertEqual(resource['submitted_at'], '2026-11-04 22:01:25')

    def test_submit_verification_tolerates_navigation_without_clicking_again(self):
        browser = object.__new__(app.HiFiNi); browser.tab = Mock(); browser.log = self.log
        browser.tab.evaluate.side_effect = [True, app.FlowError('Execution context was destroyed.'), {'ready': False, 'text': ''}, {'ready': True, 'text': '我的帖子'}]
        match = {'url': 'https://www.hifini.net/thread-123.htm', 'pending': False}
        with patch.object(browser, 'duplicate', return_value=match), patch.object(app.time, 'sleep'):
            self.assertEqual(browser.submit_and_verify('title', 'Artist', 'Song'), match)
        clicks = [call for call in browser.tab.evaluate.call_args_list if 'b.click()' in call.args[0]]
        self.assertEqual(len(clicks), 1)

    def test_manual_resolution_rejects_untrusted_url(self):
        with self.assertRaises(app.FlowError): app.resolve_submission(1, "https://example.com/thread-123.htm")

    def test_audit_failure_preserves_state_and_marks_partial_failure(self):
        row = self.resource()
        self.store.update(row["id"], "PUBLISHED", hifini_url="https://www.hifini.net/thread-1.htm")
        hifini = Mock(); hifini.audit_thread.side_effect = app.FlowError("temporary failure")
        self.assertFalse(app.review_existing_posts(self.store, hifini, app.Feishu(self.settings, self.log, False), self.log, "test"))
        self.assertEqual(self.store.db.execute("SELECT status FROM resources").fetchone()[0], "PUBLISHED")

    def test_search_fallback_error_does_not_discard_good_candidate(self):
        browser = Mock()
        browser._eval.side_effect = [
            json.dumps([{"text": "Song", "href": "https://baike.baidu.com/item/song/123"}]),
            app.FlowError("search failed"),
            json.dumps({"h1": "Song", "intro": "歌手演唱", "lyrics": "Short lyric"}),
        ]
        self.assertIn("Short lyric", app.lyrics_html(browser, "歌手 - Song")[2])

    def test_duplicate_rejection_before_switching_away_from_form(self):
        browser = self.browser()
        browser.tab.evaluate.side_effect = [True, "waiting", "该标题已经被使用，请换其他标题试试"]
        browser.duplicate = Mock()
        with patch.object(app.time, "sleep"):
            with self.assertRaises(app.DuplicateTitleError): browser.submit_and_verify("title", "Artist", "Song")
        browser.duplicate.assert_not_called()

    def test_explicit_submission_rejection_does_not_search_or_retry(self):
        browser = self.browser()
        browser.tab.evaluate.side_effect = [True, {'text': '', 'response': {'code': 'subject', 'message': '标题包含不允许的字符'}}]
        browser.duplicate = Mock()
        with patch.object(app.time, 'sleep'):
            with self.assertRaisesRegex(app.SubmissionRejectedError, '标题包含'):
                browser.submit_and_verify('title', 'Artist', 'Song')
        browser.duplicate.assert_not_called()

    def test_negative_transport_response_preserves_unknown_submission(self):
        browser = self.browser()
        browser.tab.evaluate.side_effect = [True] + [{'text': '', 'response': {'code': -1, 'message': 'timeout'}}] * 3
        browser.duplicate = Mock(return_value=None)
        with patch.object(app.time, 'sleep'):
            with self.assertRaisesRegex(app.FlowError, '未在我的帖子'):
                browser.submit_and_verify('title', 'Artist', 'Song')
        self.assertEqual(browser.duplicate.call_count, 3)

    def test_unclassified_transport_response_preserves_unknown_submission(self):
        browser = self.browser()
        browser.tab.evaluate.side_effect = [True] + [{'text': '', 'response': {'code': 'error', 'message': 'timeout'}}] * 3
        browser.duplicate = Mock(return_value=None)
        with patch.object(app.time, 'sleep'):
            with self.assertRaisesRegex(app.FlowError, '未在我的帖子'):
                browser.submit_and_verify('title', 'Artist', 'Song')
        self.assertEqual(browser.duplicate.call_count, 3)

    def test_share_partial_page_is_not_considered_complete(self):
        quark = app.Quark(self.settings, self.log)
        quark._run = Mock(return_value=[{"type": "result", "data": {"share_info": {"file_num": 2}, "files": [{"filename": "wanted"}]}}])
        self.assertFalse(quark.share_is_valid("url", {"wanted"}))

    def test_mp3_fallback_has_separate_submission_attempts(self):
        # First run is a local preview; reuse its mocks for a simulated publication.
        _, quark, hifini = self.workflow()
        quark.browse.return_value.append({"fid": "audio2", "filename": "Artist - Song.mp3"})
        hifini.submit_and_verify.side_effect = [app.DuplicateTitleError("duplicate"), {"url": "https://www.hifini.net/thread-2.htm", "pending": True}]
        with patch.object(app, "inspect_mp3_bitrates", return_value={"audio2": 320}):
            self.assertEqual(app.run(argparse.Namespace(publish=True)), 0)
        attempts = self.store.db.execute("SELECT status,title FROM submission_attempts ORDER BY id").fetchall()
        self.assertEqual([r["status"] for r in attempts], ["REJECTED", "CONFIRMED"])
        self.assertEqual(attempts[-1]["title"], "Artist《Song》[MP3-320K]")
        self.assertEqual(quark.public_share.call_args.args[0], ["audio2"])

    def test_second_title_conflict_is_permanent_skip(self):
        _, quark, hifini = self.workflow()
        quark.browse.return_value.append({"fid": "audio2", "filename": "Artist - Song.mp3"})
        hifini.submit_and_verify.side_effect = app.DuplicateTitleError("duplicate")
        with patch.object(app, "inspect_mp3_bitrates", return_value={"audio2": 320}):
            self.assertEqual(app.run(argparse.Namespace(publish=True)), 0)
        self.assertEqual(self.store.db.execute("SELECT status FROM resources").fetchone()[0], "SKIPPED_TITLE_CONFLICT")
        self.assertEqual(self.store.submitted_today(), 0)

    def test_generated_browser_javascript_has_valid_syntax(self):
        if not app.shutil.which("node"): self.skipTest("Node is not installed")
        browser = self.browser()
        browser.tab.evaluate.side_effect = [False, True]
        browser.draft('A "quoted" title', "Artist", "欧美", "<p>line</p><p>&nbsp;</p><p>&nbsp;</p>", app.PREVIEW_URL)
        expressions = [call.args[0] for call in browser.tab.evaluate.call_args_list]
        browser.tab.evaluate.reset_mock(side_effect=True)
        browser._draft_with_toolbar("title", "Artist", "欧美", app.plain_lyrics_html("line"), app.PREVIEW_URL, "无")
        expressions.extend(call.args[0] for call in browser.tab.evaluate.call_args_list)
        for expression in expressions:
            result = app.execute(["node", "-e", "new (require('vm').Script)(require('fs').readFileSync(0,'utf8'));"],
                                 input=expression, timeout=5, operation="JS syntax check")
            self.assertEqual(result.returncode, 0, result.stderr)


class NavigationTests(unittest.TestCase):
    def tab(self, loaders, states):
        tab = object.__new__(app.TaskTab)
        frames = iter(loaders)
        def call(method, params=None):
            if method == "Page.navigate":
                return {"frameId": "main", "loaderId": "requested"}
            return {"frameTree": {"frame": {"id": "main", "loaderId": next(frames)}}}
        tab.call = Mock(side_effect=call)
        tab.evaluate = Mock(side_effect=states)
        return tab

    def state(self, **overrides):
        return dict(ready="complete", body=True, host="www.hifini.net", guard=False, **overrides)

    def test_verification_reload_finishes_with_a_different_loader(self):
        ready = self.state()
        tab = self.tab(["old", "old", "requested", "reloaded"], [dict(ready, guard=True), ready])
        with patch.object(app.time, "sleep"), patch.object(app.time, "monotonic", side_effect=range(30)):
            tab.go("https://www.hifini.net/my-thread.htm")
        self.assertEqual(tab.evaluate.call_count, 2)
        self.assertEqual(sum(c.args[0] == "Page.navigate" for c in tab.call.call_args_list), 1)

    def test_previous_document_cannot_count_as_a_successful_visit(self):
        tab = self.tab(["old"] * 30, [self.state()])
        with patch.object(app.time, "sleep"), patch.object(app.time, "monotonic", side_effect=range(30)):
            with self.assertRaises(app.FlowError): tab.go("https://www.hifini.net/my-thread.htm")
        tab.evaluate.assert_not_called()

    def test_external_redirect_and_persistent_guard_remain_failures(self):
        for state in [dict(self.state(), host="example.org"), dict(self.state(), guard=True)]:
            with self.subTest(state=state):
                tab = self.tab(["old"] + ["reloaded"] * 30, [state] * 30)
                with patch.object(app.time, "sleep"), patch.object(app.time, "monotonic", side_effect=range(30)):
                    with self.assertRaises(app.FlowError): tab.go("https://www.hifini.net/my-thread.htm")


if __name__ == "__main__":
    unittest.main()
