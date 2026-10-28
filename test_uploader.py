"""Offline tests of upload policies. No real cloud writes."""
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import main as app
import uploader as up


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(app, 'ROOT', self.root))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(app, 'execute', side_effect=AssertionError('external call forbidden')))
        self.probe = self.stack.enter_context(patch.object(app, 'probe_local_mp3', return_value={'kbps': 128, 'label': 'MP3-128K'}))
        self.source = self.root / 'upload-files'
        self.source.mkdir()
        self.path = self.source / 'Artist - Song.mp3'
        self.path.write_bytes(b'test-audio')
        os.utime(self.path, (1, 1))
        self.store = app.Store()
        self.addCleanup(self.store.close)
        self.log = app.JsonLogger('test', self.store)
        self.quark = Mock()
        self.quark.media_directory.return_value = {'fid': 'media'}
        self.quark.browse.return_value = []
        self.quark.create_folder.return_value = 'song'
        self.quark.upload_file.return_value = [{'type': 'result', 'code': 0}]
        self.notify = Mock()

    def run_upload(self, apply=False):
        return up.upload_workflow(SimpleNamespace(apply=apply), self.store, self.log, self.quark, self.notify)

    def remote(self, name='Artist - Song.flac', size=100):
        return {'filename': name, 'fid': 'audio', 'category': 2, 'size': size}

    def status(self):
        return self.store.db.execute('SELECT status FROM uploads ORDER BY id DESC LIMIT 1').fetchone()[0]

    def test_dry_run_never_writes_cloud(self):
        self.assertEqual(self.run_upload()['planned'], 1)
        self.quark.create_folder.assert_not_called()
        self.quark.upload_file.assert_not_called()
        self.assertEqual(self.status(), 'PLANNED')

    def test_existing_flac_skips_mp3(self):
        self.quark.browse.return_value = [self.remote()]
        self.assertEqual(self.run_upload(True)['existing'], 1)
        self.quark.upload_file.assert_not_called()

    def test_upload_low_bitrate_and_verify(self):
        self.quark.browse.side_effect = [[], [], [self.remote(self.path.name, 10)]]
        self.assertEqual(self.run_upload(True)['uploaded'], 1)
        self.assertEqual(self.status(), 'UPLOADED')
        self.quark.create_folder.assert_called_once_with('media', 'Artist - Song')
        self.quark.upload_file.assert_called_once_with(self.path, 'song')
        self.assertTrue(self.path.exists())

    def test_empty_folder_can_be_reused(self):
        folder = {'filename': 'Artist - Song', 'fid': 'song', 'category': 0}
        self.quark.browse.side_effect = [[folder], [], [], [self.remote(self.path.name, 10)]]
        self.assertEqual(self.run_upload(True)['uploaded'], 1)
        self.quark.create_folder.assert_not_called()

    def test_unrelated_audio_in_folder_blocks_write(self):
        folder = {'filename': 'Artist - Song', 'fid': 'song', 'category': 0}
        self.quark.browse.side_effect = [[folder], [self.remote('Artist - Other.mp3')]]
        self.assertEqual(self.run_upload(True)['failed'], 1)
        self.quark.upload_file.assert_not_called()

    def test_live_and_original_are_distinct(self):
        self.quark.browse.return_value = [self.remote('Artist - Song (Live).mp3')]
        self.assertEqual(self.run_upload()['planned'], 1)
        self.assertNotEqual(up.song_identity('Artist - Song'), up.song_identity('Artist - Song (Live)'))
        self.assertNotEqual(up.song_identity('Artist - Scared To Live'), up.song_identity('Artist - Scared To'))

    def test_quality_suffix_ignored(self):
        self.assertEqual(up.song_identity('Artist - Song [MP3-128K]'), up.song_identity('Artist - Song [FLAC]'))

    def test_probe_failure_never_uploads(self):
        self.probe.side_effect = app.BitrateProbeError('invalid')
        self.assertEqual(self.run_upload(True)['failed'], 1)
        self.quark.upload_file.assert_not_called()
        self.notify.alert.assert_called_once()

    def test_recent_file_waits_for_next_run(self):
        os.utime(self.path, None)
        self.assertEqual(self.run_upload()['failed'], 1)
        self.probe.assert_not_called()

    def test_upload_timeout_is_unknown_and_not_retried(self):
        self.quark.upload_file.side_effect = app.FlowError('timeout')
        self.assertEqual(self.run_upload(True)['failed'], 1)
        self.assertEqual(self.status(), 'UNKNOWN')
        self.run_upload(True)
        self.assertEqual(self.quark.upload_file.call_count, 1)

    def test_unknown_reconciles_when_cloud_file_appears(self):
        self.store.db.execute("INSERT INTO uploads(run_id,local_path,song_key,status) VALUES('old','x',?,'UPLOADING')", (json.dumps(('artist', 'song')),))
        self.store.db.commit()
        self.quark.browse.return_value = [self.remote()]
        self.assertEqual(self.run_upload(True)['existing'], 1)
        self.assertEqual(self.store.db.execute('SELECT status FROM uploads ORDER BY id LIMIT 1').fetchone()[0], 'RECONCILED')

    def test_cloud_verification_size_mismatch_is_unknown(self):
        self.quark.browse.side_effect = [[], [], [self.remote(self.path.name, 1)]]
        self.assertEqual(self.run_upload(True)['failed'], 1)
        self.assertEqual(self.status(), 'UNKNOWN')

    def test_no_total_upload_limit(self):
        second = self.source / 'Artist - Other.mp3'
        second.write_bytes(b'test-audio')
        os.utime(second, (1, 1))
        self.quark.browse.side_effect = [[], [], [self.remote(second.name, 10)], [], [self.remote(self.path.name, 10)]]
        self.assertEqual(self.run_upload(True)['uploaded'], 2)
        self.assertEqual(self.quark.upload_file.call_count, 2)

    def test_cloud_lookup_error_fails_closed(self):
        self.quark.browse.side_effect = app.FlowError('network error')
        with self.assertRaises(app.FlowError): self.run_upload(True)
        self.quark.upload_file.assert_not_called()

    def test_probe_environment_path_wins(self):
        with patch.object(app.shutil, 'which', return_value='preferred-ffprobe'):
            self.assertEqual(app.ffprobe_path(), 'preferred-ffprobe')

    def test_probe_rejects_fake_mp3_extension(self):
        data = {'streams': [{'codec_name': 'aac', 'bit_rate': '128000', 'duration': '30'}]}
        with patch.object(app, 'execute', return_value=subprocess.CompletedProcess([], 0, json.dumps(data), '')):
            with self.assertRaises(app.BitrateProbeError): ORIGINAL_PROBE(self.path)


ORIGINAL_PROBE = app.probe_local_mp3

if __name__ == '__main__': unittest.main()
