import argparse
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main as app
import daily_summary as report


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.store = app.Store(':memory:')
        self.addCleanup(self.store.close)
        self.db = self.store.db
        self.day = date(2026, 10, 15)

    def run_row(self, key, mode, status='SUCCESS', started='2026-10-14 22:00:00'):
        self.db.execute('INSERT INTO runs(id,mode,status,started_at) VALUES(?,?,?,?)', (key, mode, status, started))

    def test_confirmed_submission_wins_over_partial_failure_and_current_audit(self):
        self.run_row('p', 'publish', 'PARTIAL_FAILURE')
        self.db.execute("INSERT INTO resources(id,folder_fid,folder_name,artist,song,status) VALUES(1,'f','Singer - Song','Singer','Song','DELETED')")
        self.db.execute("INSERT INTO submission_attempts(run_id,resource_id,title,status,created_at) VALUES('p',1,'title','CONFIRMED','2026-10-14 22:05:00')")
        text = report.build_summary(self.db, self.day)
        self.assertIn('发布任务：成功，发布歌曲：《Song》- Singer', text)
        self.assertIn('上传任务：未执行', text)

    def test_beijing_boundary_and_dry_run_exclusion(self):
        self.run_row('old', 'upload', started='2026-10-14 15:59:59')
        self.run_row('new', 'upload', started='2026-10-14 16:00:00')
        self.run_row('dry', 'upload_dry_run')
        for key, song, stamp in [('old', 'Old', '2026-10-14 15:59:59'), ('new', 'New', '2026-10-14 16:00:00'), ('dry', 'Dry', '2026-10-14 22:00:00')]:
            self.db.execute("INSERT INTO uploads(run_id,local_path,status,created_at) VALUES(?,?,'UPLOADED',?)", (key, f'D:\\upload\\Singer - {song}.mp3', stamp))
        text = report.build_summary(self.db, self.day)
        self.assertIn('上传歌曲：《New》- Singer', text)
        self.assertNotIn('Old', text)
        self.assertNotIn('Dry', text)

    def test_upload_partial_failure_and_reason(self):
        self.run_row('u', 'upload', 'PARTIAL_FAILURE')
        for song, status, detail in [('Good', 'UPLOADED', ''), ('Bad', 'UNKNOWN', '结果待核实')]:
            self.db.execute("INSERT INTO uploads(run_id,local_path,status,detail,created_at) VALUES('u',?,?,?,'2026-10-14 22:00:00')", (f'Singer - {song}.mp3', status, detail))
        text = report.build_summary(self.db, self.day)
        self.assertIn('上传任务：失败（部分成功），上传歌曲：《Good》- Singer', text)
        self.assertIn('《Bad》- Singer，失败原因：结果待核实', text)

    def test_empty_day_is_not_success(self):
        text = report.build_summary(self.db, self.day)
        self.assertIn('发布任务：未执行', text)
        self.assertIn('上传任务：未执行', text)
        self.assertTrue(text.endswith('- 无'))

    def test_preflight_and_browser_failures_are_visible_without_a_publish_run(self):
        self.run_row('u', 'upload')
        for phase in ('blocked_by_preflight', 'blocked_by_browser'):
            text = report.build_summary(self.db, self.day, [{'publication': phase}])
            self.assertIn('发布任务：受阻', text)
            self.assertIn('发布前置检查', text)
            self.assertNotIn('失败任务：\n- 无', text)

    def test_upload_recovered_later_is_success_with_history(self):
        self.run_row('old', 'upload', 'PARTIAL_FAILURE')
        self.run_row('new', 'upload', started='2026-10-14 23:00:00')
        for run, state in [('old', 'FAILED'), ('new', 'UPLOADED')]:
            self.db.execute("INSERT INTO uploads(run_id,local_path,status,created_at) VALUES(?,'Singer - Song.mp3',?,'2026-10-14 22:00:00')", (run, state))
        text = report.build_summary(self.db, self.day)
        self.assertIn('上传任务：成功', text)
        self.assertIn('期间已恢复 1 项', text)
        self.assertNotIn('上传任务：失败', text)

    def test_unconfigured_notification_can_be_explicitly_recovered(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'ROOT', Path(directory)), redirect_stdout(io.StringIO()):
            store = app.Store()
            store.db.execute("INSERT INTO alerts(run_id,title,detail,fingerprint,status,attempts) VALUES('old','每日总结','x','f','NOT_CONFIGURED',0)")
            store.db.commit();store.close()
            folder = Path(directory) / 'data/daily-summaries'
            folder.mkdir()
            original = folder / '2026-10-15.json'
            original.write_text(json.dumps({'status': 'UNCONFIRMED', 'run_id': 'old'}))
            args = argparse.Namespace(date='2026-10-15',send=True,retry_undelivered=True)
            with patch.object(app.Settings,'load'), patch.object(app.Feishu,'alert',return_value=True) as send:
                self.assertEqual(report.run_summary(args),0)
                self.assertEqual(report.run_summary(args),0)
                send.assert_called_once()
            self.assertEqual(json.loads(original.read_text())['status'],'UNCONFIRMED')

    def test_delivery_success_or_unknown_never_resends(self):
        for sent in (True, False):
            with self.subTest(sent=sent), tempfile.TemporaryDirectory() as directory, patch.object(app, 'ROOT', Path(directory)), redirect_stdout(io.StringIO()):
                store = app.Store()
                store.close()
                args = argparse.Namespace(date='2026-10-15', send=True)
                with patch.object(app.Settings, 'load'), patch.object(app.Feishu, 'alert', return_value=sent) as send:
                    self.assertEqual(report.run_summary(args), 0 if sent else 1)
                    self.assertEqual(report.run_summary(args), 0 if sent else 1)
                    send.assert_called_once()
                    self.assertEqual(send.call_args.kwargs, {'raw_text': True, 'max_attempts': 1})
                receipt = json.loads((Path(directory) / 'data/daily-summaries/2026-10-15.json').read_text(encoding='utf-8'))
                self.assertEqual(receipt['status'], 'SENT' if sent else 'UNCONFIRMED')

    def test_only_proven_unsent_report_can_be_retried(self):
        for error, allowed in [('curl: (28) Resolving timed out after 5000 milliseconds', True), ('curl: (28) Operation timed out after 30000 milliseconds', False)]:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory, patch.object(app, 'ROOT', Path(directory)), redirect_stdout(io.StringIO()):
                store = app.Store()
                store.db.execute("INSERT INTO alerts(run_id,title,detail,fingerprint,status,attempts,last_error) VALUES('old','每日总结','x','f','FAILED',1,?)", (error,))
                store.db.commit(); store.close()
                folder = Path(directory) / 'data/daily-summaries'
                folder.mkdir()
                original = folder / '2026-10-15.json'
                original.write_text(json.dumps({'status': 'UNCONFIRMED', 'run_id': 'old'}), encoding='utf-8')
                args = argparse.Namespace(date='2026-10-15', send=True, retry_undelivered=True)
                with patch.object(app.Settings, 'load'), patch.object(app.Feishu, 'alert', return_value=True) as send:
                    if allowed:
                        self.assertEqual(report.run_summary(args), 0)
                        self.assertEqual(report.run_summary(args), 0)
                        send.assert_called_once()
                    else:
                        with self.assertRaisesRegex(app.FlowError, '无法证明'):
                            report.run_summary(args)
                        send.assert_not_called()
                self.assertEqual(json.loads(original.read_text())['status'], 'UNCONFIRMED')


if __name__ == '__main__':
    unittest.main()
