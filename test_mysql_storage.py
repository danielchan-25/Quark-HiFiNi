"""Real MySQL integration. Every test uses a new private table prefix, removed afterwards."""
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

import main as app
import mysql_storage as mysql
import runtime_store as runtime
import mysql_backup
import monitoring
import daily_summary
import preflight
from scripts import daily_workflow
from datetime import datetime


class StorageConfigurationTests(unittest.TestCase):
    def test_missing_configuration_cannot_recreate_a_retired_local_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'main.py').touch()
            with self.assertRaises(sqlite3.OperationalError):mysql.config(root)
            self.assertFalse((root/'data/quark-hifini.db').exists())
            (root/'.env').write_text('STORAGE_BACKEND=sqlite\n')
            self.assertIsNone(mysql.config(root))

    def test_unknown_backend_never_falls_back_to_local_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'.env').write_text('STORAGE_BACKEND=mysqll\n')
            with self.assertRaises(sqlite3.OperationalError):mysql.config(root)


@unittest.skipUnless(os.getenv('HIFINI_MYSQL_INTEGRATION')=='1','MySQL integration is explicitly enabled')
class MySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw=json.loads(Path(os.environ['HIFINI_MYSQL_CONFIG']).read_text()) if os.getenv('HIFINI_MYSQL_CONFIG') else {
            'host':os.getenv('MYSQL_TEST_HOST','127.0.0.1'),'user':'root','password':os.getenv('MYSQL_TEST_PASSWORD','ci-test-only'),'database':'quark_hifini_ci'}
        cls.cfg=replace(mysql.Config(**raw),prefix='bilibili_HiFiNi_it_'+uuid.uuid4().hex[:4])
        memory=app.Store(':memory:');cls.schema=mysql.create_schema(cls.cfg,memory.db);mysql.import_rows(cls.cfg,memory.db,cls.schema);memory.close()
        cls.directory=tempfile.TemporaryDirectory(prefix='quark-hifini-mysql-integration-');cls.root=Path(cls.directory.name)
        (cls.root/'main.py').touch()
        (cls.root/'.env').write_text('\n'.join(['STORAGE_BACKEND=mysql','MYSQL_HOST='+cls.cfg.host,'MYSQL_USER='+cls.cfg.user,'MYSQL_PASSWORD='+cls.cfg.password,'MYSQL_PORT='+str(cls.cfg.port),'MYSQL_DATABASE='+cls.cfg.database,'MYSQL_TABLE_PREFIX='+cls.cfg.prefix]))
        (cls.root/'.env').chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        db=mysql.connect(cls.cfg,autocommit=True)
        try:
            with db.cursor() as c:
                c.execute('SET FOREIGN_KEY_CHECKS=0')
                for logical in [*cls.schema['tables'],'state','logs']:c.execute('DROP TABLE `'+cls.cfg.table(logical)+'`')
        finally:db.close();cls.directory.cleanup()

    def setUp(self):
        self.patch=patch.object(app,'ROOT',self.root);self.patch.start();self.addCleanup(self.patch.stop)
        db=mysql.connect(self.cfg,autocommit=True)
        try:
            with db.cursor() as c:
                c.execute('SET FOREIGN_KEY_CHECKS=0')
                for logical in self.schema['tables']:c.execute('DELETE FROM `'+self.cfg.table(logical)+'`')
                c.execute('DELETE FROM `'+self.cfg.table('state')+'` WHERE path<>%s',('metadata/schema',))
                c.execute('DELETE FROM `'+self.cfg.table('logs')+'`')
        finally:db.close()
        self.store=app.Store();self.addCleanup(self.store.close)
        self.store.start_run('integration','publish')

    def resource(self,name='歌手 - 歌曲'):
        return self.store.upsert_resource({'fid':uuid.uuid4().hex,'filename':name})

    def test_old_pending_song_allows_a_different_song_next_day(self):
        row=self.resource();attempt=self.store.begin_submission('integration',row['id'],'old')
        self.store.db.execute("UPDATE submission_attempts SET created_at='2026-01-01 00:00:00' WHERE id=?",(attempt,));self.store.db.commit()
        with self.assertRaises(app.FlowError):self.store.begin_submission('integration',row['id'],'old')
        other=self.resource('歌手 - 其他歌曲');self.store.begin_submission('integration',other['id'],'new')
        self.assertEqual(self.store.submitted_today(),1)
        with self.assertRaises(app.FlowError):self.store.begin_submission('integration',self.resource('歌手 - 第三首')['id'],'third')

    def test_transaction_rollback_and_indexed_rows(self):
        with self.assertRaises(ValueError):
            with self.store.db:
                self.store.db.execute("INSERT INTO resources(folder_fid,folder_name) VALUES(?,?)",('rollback','不可保留'))
                raise ValueError('rollback')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM resources').fetchone()[0],0)
        row=self.resource();self.assertEqual(dict(row)['id'],row[0])

    def test_documents_claim_once_and_do_not_create_files(self):
        path=self.root/'data/daily-summaries/2026-01-01.json'
        runtime.write_json(path,{'status':'SENDING','message':'中文'},exclusive=True)
        with self.assertRaises(FileExistsError):runtime.write_json(path,{'status':'SENDING'},exclusive=True)
        self.assertEqual(runtime.read_json(path)['message'],'中文');self.assertFalse(path.exists())
        with patch.object(daily_workflow,'ROOT',self.root),redirect_stdout(io.StringIO()):daily_workflow.finish({'date':'2026-01-01','publication':'busy'},'test-workflow')
        self.assertEqual(len(runtime.paths(self.root/'data/workflows/2026-01-01','*.json')),1)

    def test_preflight_and_logs_stay_in_mysql(self):
        preflight.persist(self.root,{'blockers':[]},0,'integration-health')
        self.assertEqual(preflight.read(self.root/'data/preflight/latest.json')['status'],'PASS')
        path=self.root/'logs/monitor/watchdog.log';log=monitoring.logger(path)
        log.info('数据库日志：%s','成功')
        for handler in log.handlers:handler.close()
        self.assertIn('数据库日志：成功',runtime.text(path));self.assertFalse(path.exists())

    def test_confirmed_post_is_summarized_and_readonly_is_enforced(self):
        row=self.resource();self.store.begin_submission('integration',row['id'],'title');self.store.mark_submitted(row['id'],'https://www.hifini.net/thread-123.htm')
        text=daily_summary.build_summary(self.store.db,datetime.now(app.BEIJING).date())
        self.assertIn('发布任务：成功',text)
        db=mysql.Connection(self.cfg,readonly=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM resources')
        finally:db.close()

    def test_preview_uses_ram_and_cannot_change_production_rows(self):
        self.resource();target=app.Store(':memory:')
        try:
            self.store.db.backup(target.db);target.db.execute("UPDATE resources SET status='FAILED'");target.db.commit()
            self.assertEqual(self.store.db.execute('SELECT status FROM resources').fetchone()[0],'DISCOVERED')
        finally:target.close()

    def test_named_lock_is_exclusive_and_released(self):
        with mysql.lock(self.cfg,'test'):
            with self.assertRaises(BlockingIOError):
                with mysql.lock(self.cfg,'test'):pass
        with mysql.lock(self.cfg,'test'):pass

    def test_missing_migration_marker_never_starts_an_empty_database(self):
        db=mysql.connect(self.cfg,autocommit=True)
        try:
            with db.cursor() as c:
                c.execute('SELECT content,sha256 FROM `'+self.cfg.table('state')+'` WHERE path=%s',('metadata/schema',));saved=c.fetchone()
                c.execute('DELETE FROM `'+self.cfg.table('state')+'` WHERE path=%s',('metadata/schema',))
            with self.assertRaises(sqlite3.OperationalError):app.Store()
            self.assertFalse((self.root/'data/quark-hifini.db').exists())
        finally:
            with db.cursor() as c:c.execute('INSERT INTO `'+self.cfg.table('state')+'` (path,content,sha256) VALUES(%s,%s,%s)',('metadata/schema',*saved))
            db.close()

    def test_full_monitor_wrapper_records_child_without_local_runtime_files(self):
        code="""import sys,uuid
from pathlib import Path
sys.path.insert(0,sys.argv[2])
import main as app
app.ROOT=Path(sys.argv[1])
store=app.Store();key=uuid.uuid4().hex
store.start_run(key,'upload');store.finish_run(key,'SUCCESS')
print('mysql child completed')
store.close()
"""
        with redirect_stdout(io.StringIO()):
            result=monitoring.run_task(self.root,'daily',[sys.executable,'-c',code,str(self.root),str(Path(__file__).parent)])
        self.assertEqual(result,0)
        path=runtime.paths(self.root/'data/monitor/runs','*/*/*.json')[0]
        self.assertEqual(runtime.read_json(path)['outcome'],'SUCCESS')
        self.assertFalse(list((self.root/'data').rglob('*.json')))

    def test_sql_time_percent_and_real_backup_restore(self):
        row=self.resource();self.store.update(row['id'],'SUBMITTED',submitted_at='2026-01-01 00:00:00',hifini_url='https://www.hifini.net/thread-1.htm',detail='100% 中文')
        self.assertGreater(self.store.audit_candidates()[0]['pending_seconds'],0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM resources WHERE detail LIKE '%中文'").fetchone()[0],1)
        unrelated=self.cfg.prefix+'_unrelated_test';db=mysql.connect(self.cfg,autocommit=True)
        with db.cursor() as cursor:cursor.execute('CREATE TABLE `'+unrelated+'` (id INT)')
        with tempfile.TemporaryDirectory(prefix='mysql-restore-test-') as directory:
            try:
                metadata=mysql_backup.export(self.cfg,Path(directory))
                self.assertNotIn(unrelated,metadata['tables'])
                self.assertTrue(mysql_backup.verify_restore(self.cfg,Path(directory),metadata)['verified'])
            finally:
                with db.cursor() as cursor:cursor.execute('DROP TABLE `'+unrelated+'`')
                db.close()


if __name__=='__main__':unittest.main()
