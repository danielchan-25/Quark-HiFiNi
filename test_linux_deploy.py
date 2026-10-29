"""Offline checks for the Linux scheduler and browser configuration boundary."""
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path, PurePosixPath
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import browser, daily_workflow

spec = importlib.util.spec_from_file_location('timer_installer', Path(__file__).parent / 'scripts/install-user-timers.py')
timer_installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timer_installer)


class DeploymentTests(unittest.TestCase):
    def test_browser_endpoint_must_be_local_origin(self):
        self.assertEqual(browser.cdp_port('http://127.0.0.1:9501'), 9501)
        self.assertEqual(browser.cdp_port('http://localhost:9610/'), 9610)
        for value in ('https://127.0.0.1:9501', 'http://example.com:9501', 'http://user:password@localhost:9501',
                      'http://localhost:9501/json/version', 'http://localhost:9501/?token=hidden'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                browser.cdp_port(value)

    def test_preview_starts_no_task_and_creates_no_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(daily_workflow, 'ROOT', Path(directory)), \
                patch.object(sys, 'argv', ['daily_workflow.py']), patch.object(daily_workflow, 'invoke') as invoke, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(daily_workflow.main(), 0)
            invoke.assert_not_called()
            self.assertFalse((Path(directory) / 'data').exists())

    def execute_workflow(self, results):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        task = root / 'task.txt'
        instruction = 'Exact original scheduled instruction.\nKeep its final newline.\n'
        task.write_text(instruction, encoding='utf-8')
        fake_fcntl = Mock()
        fake_fcntl.LOCK_EX = 2
        fake_fcntl.LOCK_NB = 4
        with patch.object(daily_workflow, 'ROOT', root), patch.object(sys, 'platform', 'linux'), \
                patch.dict(sys.modules, {'fcntl': fake_fcntl}), \
                patch.object(sys, 'argv', ['daily_workflow.py', '--execute', '--session-input-file', str(task)]), \
                patch.object(daily_workflow, 'invoke', side_effect=results) as invoke, redirect_stdout(io.StringIO()):
            code = daily_workflow.main()
            calls = [call.args[0] for call in invoke.call_args_list]
        return code, calls, instruction

    def test_upload_failure_does_not_skip_publish_preflight(self):
        code, calls, instruction = self.execute_workflow([1, 1])
        self.assertEqual(code, 1)
        self.assertIn('upload', calls[0])
        self.assertIn('--apply', calls[0])
        self.assertEqual(calls[0][calls[0].index('--session-input') + 1], instruction)
        self.assertEqual(calls[1][-1], 'check')
        self.assertEqual(len(calls), 2)

    def test_publish_occurs_once_after_check_and_browser(self):
        code, calls, instruction = self.execute_workflow([0, 0, 0, 1])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1][-1], 'check')
        self.assertTrue(calls[2][-1].endswith('start-browser.sh'))
        self.assertIn('--publish', calls[3])
        self.assertEqual(calls[3][calls[3].index('--session-input') + 1], instruction)
        self.assertEqual(calls[0][-1], calls[3][-1])

    def test_browser_failure_never_submits(self):
        code, calls, _ = self.execute_workflow([0, 0, 1])
        self.assertEqual(code, 1)
        self.assertFalse(any('--publish' in call for call in calls))

    def test_unit_generation_quotes_custom_project_directory(self):
        template = 'WorkingDirectory=@PROJECT_ROOT@\nExecStart=/usr/bin/bash @PROJECT_ROOT@/scripts/daily-run.sh --execute\n'
        text = timer_installer.render_service(template, PurePosixPath('/home/user/My Project%name'))
        self.assertIn('WorkingDirectory=/home/user/My Project%%name\n', text)
        self.assertIn('daily-run.sh" --execute', text)
        self.assertNotIn('@PROJECT_ROOT@', text)


if __name__ == '__main__':
    unittest.main()
