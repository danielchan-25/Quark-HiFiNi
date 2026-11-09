"""Evidence-based reporting; these tests never touch real accounts or task state."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

import preflight


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = {'database': 'ok', 'dependencies': {'node': True, 'curl': True},
                       'git_bash': True, 'quark_cli': True, 'unresolved_submissions': 1,
                       'unresolved_attempt_ids': [26], 'unresolved_blocks_other_songs': False}
        self.at = datetime.fromisoformat('2026-11-09T08:00:00+08:00')
        self.row = {'date': '2026-11-08', 'id': '060001-example',
                    'workflow': {'publication': 'blocked_by_preflight',
                                 'ended_at': '2026-11-08T06:00:01+08:00',
                                 'preflight': {'blockers': [{'code': 'dependency:node', 'detail': '缺少运行程序：node'}]}}}

    def health(self, code=0, checked='2026-11-08T14:30:00+08:00'):
        path = self.root / 'data/preflight/latest.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'status': 'PASS' if code == 0 else 'FAIL', 'exit_code': code,
                                    'checked_at': checked, 'workflow_id': 'health-proof',
                                    'blockers': [] if code == 0 else [{'code': 'dependency:node', 'detail': 'node unavailable'}]}))

    def test_pending_song_is_not_a_current_environment_blocker(self):
        self.assertEqual(preflight.blockers(self.report), [])
        self.report.pop('unresolved_blocks_other_songs')
        result = preflight.blockers(self.report)
        self.assertEqual([item['code'] for item in result], ['legacy_unknown_submission'])
        self.assertIn('26', result[0]['detail'])

    def test_dependency_and_database_errors_have_specific_causes(self):
        self.report['database'] = 'corrupt'
        self.report['dependencies']['node'] = False
        result = preflight.blockers(self.report)
        self.assertEqual([item['code'] for item in result], ['database', 'dependency:node'])
        self.assertIn('node', result[1]['detail'])

    def test_record_is_correlated_with_its_workflow_and_private(self):
        self.report['blockers'] = []
        preflight.persist(self.root, self.report, 0, 'specific-workflow')
        result = preflight.workflow_result(self.root, 'specific-workflow', 0)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(preflight.read(self.root / 'data/preflight/latest.json')['workflow_id'], 'specific-workflow')
        self.assertEqual((self.root / 'data/preflight/latest.json').stat().st_mode & 0o777, 0o600)
        self.assertTrue(preflight.workflow_result(self.root, 'specific-workflow', 1)['blockers'])
        path = self.root / 'data/preflight/runs/specific-workflow.json'
        path.write_text(json.dumps({'workflow_id': 'specific-workflow', 'exit_code': 0, 'status': 'PASS'}))
        self.assertEqual(preflight.workflow_result(self.root, 'specific-workflow', 0)['exit_code'], 0)
        with self.assertRaises(ValueError): preflight.persist(self.root, self.report, 0, '../escape')

    def test_later_success_recovers_environment_but_not_original_run(self):
        self.health()
        original = json.loads(json.dumps(self.row))
        result = preflight.assess(self.root, self.row, self.at)
        self.assertTrue(result['recovered'])
        self.assertEqual(self.row, original)
        text = preflight.describe(result)
        self.assertIn('2026-11-08 06:00:01', text)
        self.assertIn('缺少运行程序：node', text)
        self.assertIn('2026-11-08 14:30:00', text)
        self.assertIn('原任务未补跑', text)

    def test_old_future_and_failed_health_records_cannot_prove_recovery(self):
        for code, checked in [(0, '2026-11-08T05:59:00+08:00'), (0, '2026-11-09T09:00:00+08:00'),
                              (1, '2026-11-08T14:30:00+08:00')]:
            with self.subTest(code=code, checked=checked):
                self.health(code, checked)
                self.assertFalse(preflight.assess(self.root, self.row, self.at)['recovered'])
        self.row['workflow']['publication'] = 'finished_check_database_for_actual_submission_result'
        self.health()
        self.assertIsNone(preflight.assess(self.root, self.row, self.at))

    def test_legacy_log_recovers_original_reason_without_rewriting_it(self):
        self.row['workflow'].pop('preflight')
        report = dict(self.report)
        report.pop('unresolved_blocks_other_songs')
        path = self.root / 'logs/monitor/daily/2026-11-08/060001-example.log'
        path.parent.mkdir(parents=True)
        original = ''.join('2026-11-08T06:00:01+08:00 ' + line + '\n' for line in json.dumps(report, indent=2).splitlines())
        path.write_text(original)
        result = preflight.assess(self.root, self.row, self.at)
        self.assertIn('旧规则', result['reason'])
        self.assertIn('26', result['reason'])
        self.assertEqual(path.read_text(), original)

    def test_malformed_health_cannot_hide_a_failure(self):
        path = self.root / 'data/preflight/latest.json'
        path.parent.mkdir(parents=True)
        path.write_text('[]')
        self.assertFalse(preflight.assess(self.root, self.row, self.at)['recovered'])


if __name__ == '__main__':
    unittest.main()
