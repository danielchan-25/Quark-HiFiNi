from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import backup
import main as app


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.root=self.base/'app'
        self.root.mkdir()
        self.store=app.Store(self.root/'data/quark-hifini.db')
        self.addCleanup(self.store.close)
        self.store.db.execute("INSERT INTO runs(id,mode,status) VALUES('original','publish','SUCCESS')")
        self.store.db.commit()
        (self.root/'.env').write_text('FAKE_SETTING=private-fixture\n')
        (self.root/'.gitignore').write_text('data/\n.env\n')
        (self.root/'README.md').write_text('backup fixture')
        folder=self.root/'data/daily-summaries';folder.mkdir()
        (folder/'2026-11-01.json').write_text('{"status":"SENT"}')
        for command in (['git','init','-q'],['git','add','README.md','.gitignore'],
                        ['git','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture']):
            subprocess.run(command,cwd=self.root,check=True,capture_output=True)
        self.destination=self.base/'backups'
        self.records=self.base/'records'

    def test_consistent_backup_can_restore_and_does_not_change_business_rows(self):
        result=backup.perform(self.root,self.destination,self.records)
        self.assertEqual(result['status'],'SUCCESS')
        archive=Path(result['archive'])
        self.assertEqual(result['sha256'],backup.digest(archive))
        with tarfile.open(archive) as source:
            self.assertEqual(source.extractfile('data/daily-summaries/2026-11-01.json').read(),b'{"status":"SENT"}')
            self.assertEqual(source.extractfile('.env').read(),b'FAKE_SETTING=private-fixture\n')
            self.assertIn('source/README.md',source.getnames())
        self.assertEqual(self.store.db.execute('SELECT id FROM runs').fetchall()[0][0],'original')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM runs').fetchone()[0],1)
        manifest=json.loads(next(self.records.glob('*/manifest.json')).read_text())
        backup.verify(archive,manifest,result['tables'])

    def test_failed_network_copy_preserves_last_success_and_removes_partial(self):
        first=backup.perform(self.root,self.destination,self.records)
        original=shutil.copyfile
        def fail(source,destination,*args,**kwargs):
            if str(destination).endswith('.partial'):raise OSError('simulated network share failure')
            return original(source,destination,*args,**kwargs)
        with patch.object(backup.shutil,'copyfile',side_effect=fail), self.assertRaises(OSError):
            backup.perform(self.root,self.destination,self.records)
        state=json.loads((self.root/'data/backup/last-backup.json').read_text())
        self.assertEqual(state['status'],'FAILED')
        self.assertEqual(state['last_success_at'],first['last_success_at'])
        self.assertFalse(list(self.destination.glob('*.partial')))
        self.assertTrue(Path(first['archive']).is_file())

    def test_retention_deletes_only_own_old_verified_backups(self):
        first=backup.perform(self.root,self.destination,self.records)
        old=self.destination/'quark-hifini-state-20200101-033000-12345678.tar.gz'
        shutil.copyfile(first['archive'],old)
        old.with_name(old.name+'.sha256').write_text(backup.digest(old)+'  '+old.name)
        unrelated=self.destination/'user-backup.tar.gz';unrelated.write_bytes(b'keep')
        backup.perform(self.root,self.destination,self.records,retention_days=14)
        self.assertFalse(old.exists())
        self.assertTrue(unrelated.exists())
