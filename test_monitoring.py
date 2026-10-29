"""Offline failure scenarios for scheduler observation; no real network or publishing."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import main as app
import monitoring as monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = app.Store(self.root / 'data' / 'quark-hifini.db')
        self.addCleanup(self.store.close)
        self.at = datetime(2026, 10, 30, 6, 11, tzinfo=monitor.BEIJING)
        self.config = {'enabled_at': '2026-10-30T01:00:00+08:00'}
        monitor.save(monitor.monitor_dir(self.root) / 'config.json', self.config)
        self.services = {}
        for task in monitor.TASKS:
            self.services[f'quark-hifini-{task}.timer'] = {'ActiveState': 'active', 'UnitFileState': 'enabled'}
            self.services[f'quark-hifini-{task}.service'] = {'ActiveState': 'inactive'}

    def add_record(self, task='daily', day='2026-10-30', state='FINISHED', outcome='SUCCESS', **extra):
        row = {'task': task, 'started_at': day + ('T06:00:00+08:00' if task == 'daily' else 'T23:55:00+08:00'),
               'state': state, 'outcome': outcome, 'exit_code': 0, 'log_path': '/local/task.log', **extra}
        monitor.save(monitor.monitor_dir(self.root) / 'runs' / task / day / 'example.json', row)
        return row

    def inspect(self, at=None, config=None):
        return monitor.inspect(self.root, at or self.at, config or self.config, self.services)

    def check(self, send=False):
        with patch.object(monitor, 'now', return_value=self.at), patch.object(monitor, 'units', return_value=self.services), \
                redirect_stdout(io.StringIO()):
            return monitor.check(self.root, send)

    def test_old_windows_business_history_does_not_hide_missing_linux_timer(self):
        self.store.db.execute("INSERT INTO runs(id,mode,status,started_at) VALUES('windows','publish','SUCCESS','2026-10-29 22:00:00')")
        self.store.db.commit()
        problems, _ = self.inspect()
        self.assertEqual([row['key'] for row in problems], ['2026-10-30:daily:missing'])

    def test_no_retroactive_alerts_and_ten_minute_start_grace(self):
        self.assertEqual(self.inspect(config={'enabled_at': self.at.isoformat()})[0], [])
        self.assertEqual(self.inspect(at=self.at.replace(minute=9))[0], [])

    def test_previous_day_summary_is_checked_after_midnight(self):
        config = {'enabled_at': '2026-10-30T23:00:00+08:00'}
        at = datetime(2026, 10, 31, 0, 6, tzinfo=monitor.BEIJING)
        problems, _ = self.inspect(at=at, config=config)
        self.assertEqual([row['key'] for row in problems], ['2026-10-30:summary:missing'])

    def test_partial_result_with_confirmed_submission_is_warning(self):
        self.add_record(outcome='PARTIAL_FAILURE', exit_code=1)
        problems, warnings = self.inspect()
        self.assertFalse(problems)
        self.assertEqual(len(warnings), 1)
        facts = {'runs': [{'mode': 'upload', 'status': 'SUCCESS'}, {'mode': 'publish', 'status': 'PARTIAL_FAILURE'}],
                 'attempts': [{'status': 'CONFIRMED'}]}
        self.assertEqual(monitor.outcome('daily', 1, facts), 'PARTIAL_FAILURE')
        facts['runs'][0]['status'] = 'FAILED'
        self.assertEqual(monitor.outcome('daily', 1, facts), 'FAILED')
        facts['attempts'].append({'status': 'UNKNOWN'})
        self.assertEqual(monitor.outcome('daily', 1, facts), 'UNKNOWN')

    def test_stopped_owner_and_long_running_owner(self):
        self.add_record(state='RUNNING')
        self.assertTrue(self.inspect()[0][0]['key'].endswith(':interrupted'))
        self.services['quark-hifini-daily.service']['ActiveState'] = 'activating'
        self.assertFalse(self.inspect()[0])
        later = self.at + timedelta(minutes=40)
        self.assertTrue(self.inspect(at=later)[0][0]['key'].endswith(':slow'))

    def test_disabled_timer_alerts_even_before_due_time(self):
        self.services['quark-hifini-daily.timer']['UnitFileState'] = 'disabled'
        self.assertTrue(self.inspect(at=self.at.replace(hour=2))[0][0]['key'].endswith(':timer'))

    def test_incident_delivery_claim_is_durable_and_deduplicated(self):
        with patch.object(monitor, 'notify', return_value='UNCONFIRMED') as send, \
                patch.object(monitor, 'run_task') as publish:
            self.assertEqual(self.check(), 1)
            send.assert_not_called()
            self.check(send=True)
            self.check(send=True)
            send.assert_called_once()
            publish.assert_not_called()
        history = monitor.read(monitor.monitor_dir(self.root) / 'incidents.json')
        self.assertEqual(history['2026-10-30:daily:missing']['notification'], 'UNCONFIRMED')
        self.add_record()
        self.assertEqual(self.check(send=True), 0)
        self.assertIsNotNone(monitor.read(monitor.monitor_dir(self.root) / 'incidents.json')['2026-10-30:daily:missing']['resolved_at'])

    def test_database_and_systemctl_failure_are_observable(self):
        with patch.object(monitor, 'units', side_effect=RuntimeError('systemd unavailable')), \
                patch.object(monitor, 'now', return_value=self.at), redirect_stdout(io.StringIO()):
            self.assertEqual(monitor.check(self.root), 1)
        report = monitor.read(monitor.monitor_dir(self.root) / 'last-check.json')
        self.assertIn('巡检异常', report['problems'][0]['detail'])

    def test_wrapper_captures_startup_failure_stderr_and_redacts_secrets(self):
        secret = 'private-password-example'
        code = 'import sys; print("password=" + sys.argv[1], file=sys.stderr); raise RuntimeError("startup broke")'
        with patch.dict(os.environ, {'HIFINI_PASSWORD': secret}), redirect_stdout(io.StringIO()):
            result = monitor.run_task(self.root, 'daily', [sys.executable, '-u', '-c', code, secret])
        self.assertEqual(result, 1)
        record = monitor.read(next((monitor.monitor_dir(self.root) / 'runs').glob('*/*/*.json')))
        self.assertEqual(record['outcome'], 'FAILED')
        log = Path(record['log_path']).read_text()
        self.assertIn('startup broke', log)
        self.assertNotIn(secret, log)
        self.assertIn('[REDACTED]', log)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)

    def test_wrapper_links_only_new_runs_and_preserves_confirmed_evidence(self):
        self.store.db.execute("INSERT INTO runs(id,mode,status) VALUES('old','publish','SUCCESS')")
        self.store.db.execute("INSERT INTO resources(id,folder_fid,folder_name) VALUES(1,'fid','Artist - Song')")
        self.store.db.commit()
        code = '''import sqlite3,sys
c=sqlite3.connect(sys.argv[1])
c.execute("INSERT INTO runs(id,mode,status) VALUES('new','publish','PARTIAL_FAILURE')")
c.execute("INSERT INTO submission_attempts(run_id,resource_id,title,status) VALUES('new',1,'Song','CONFIRMED')")
c.commit()
print('{"event":"workflow_stage_started","stage":"publish","time":"2026-10-30T06:00:00+08:00"}')
raise SystemExit(1)
'''
        with redirect_stdout(io.StringIO()):
            monitor.run_task(self.root, 'daily', [sys.executable, '-u', '-c', code, str(self.root / 'data' / 'quark-hifini.db')])
        record = monitor.read(next((monitor.monitor_dir(self.root) / 'runs').glob('*/*/*.json')))
        self.assertEqual(record['outcome'], 'PARTIAL_FAILURE')
        self.assertEqual(record['stage'], 'publish')
        self.assertEqual([r['id'] for r in record['evidence']['runs']], ['new'])
        self.assertEqual(record['evidence']['attempts'][0]['status'], 'CONFIRMED')

    def test_summary_requires_a_sent_receipt(self):
        self.assertEqual(monitor.outcome('summary', 0, {'receipt_status': 'UNCONFIRMED'}), 'FAILED')
        self.assertEqual(monitor.outcome('summary', 0, {'receipt_status': 'SENT'}), 'SUCCESS')

    def test_live_attempt_is_not_misreported_as_abandoned_and_reads_do_not_mutate(self):
        self.add_record(state='RUNNING')
        self.services['quark-hifini-daily.service']['ActiveState'] = 'activating'
        self.store.db.execute("INSERT INTO runs(id,mode,status) VALUES('live','publish','RUNNING')")
        self.store.db.execute("INSERT INTO submission_attempts(run_id,resource_id,title,status) VALUES('live',1,'Song','UNKNOWN')")
        self.store.db.commit()
        self.assertFalse(self.inspect()[0])
        self.assertEqual(self.store.db.execute("SELECT status FROM submission_attempts").fetchone()[0], 'UNKNOWN')
        self.store.db.execute("UPDATE runs SET status='FAILED'")
        self.store.db.commit()
        self.assertTrue(any(row['key'].endswith(':unknown-submission') for row in self.inspect()[0]))

    def test_wrapper_records_child_termination_as_failure(self):
        with redirect_stdout(io.StringIO()):
            code = monitor.run_task(self.root, 'daily', [sys.executable, '-c', 'import os,signal; os.kill(os.getpid(),signal.SIGTERM)'])
        self.assertEqual(code, -15)
        record = monitor.read(next((monitor.monitor_dir(self.root) / 'runs').glob('*/*/*.json')))
        self.assertEqual(record['outcome'], 'FAILED')
        self.assertEqual(record['state'], 'FINISHED')

    def test_notification_handles_non_object_response(self):
        with patch.dict(os.environ, {'FEISHU_WEBHOOK': 'https://example.invalid'}), \
                patch.object(app, 'execute', return_value=subprocess.CompletedProcess([], 0, '[]', '')):
            self.assertEqual(monitor.notify('test'), 'UNCONFIRMED')

    def test_retention_does_not_delete_legacy_logs_or_business_data(self):
        legacy = self.root / 'logs' / 'legacy.jsonl'
        legacy.parent.mkdir(exist_ok=True)
        legacy.write_text('legacy')
        own = self.root / 'logs' / 'monitor' / 'daily' / 'old.log'
        own.parent.mkdir(parents=True)
        own.write_text('old')
        for path in (legacy, own):
            os.utime(path, (self.at.timestamp() - 40 * 86400,) * 2)
        monitor.prune(self.root, self.at)
        self.assertFalse(own.exists())
        self.assertTrue(legacy.exists())
        self.assertTrue((self.root / 'data' / 'quark-hifini.db').exists())


if __name__ == '__main__':
    unittest.main()
