import sqlite3
import tempfile
import unittest
from pathlib import Path

from cloud import backup_data, link_profile_skills, data_fingerprint, require_unchanged_data, application_lock
from maintenance import UpdateError


class DataIsolationTests(unittest.TestCase):
    def test_live_data_change_rejects_activation(self):
        import maintenance as m
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); old = root / 'old'; new = root / 'new'
            old.mkdir(); new.mkdir(); active = root / 'current'; active.symlink_to(old)
            (old / 'config').write_text('before')
            before = data_fingerprint(old)
            (old / 'config').write_text('new writes during build')
            with self.assertRaises(UpdateError):
                m.activate(active, new, {}, lambda: require_unchanged_data(old, before))
            self.assertEqual(old, active.resolve())

    def test_application_start_cannot_take_a_shared_lock_during_refresh(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with application_lock(root):
                result = subprocess.run(['flock', '--shared', '--nonblock', str(root / 'application.lock'), 'true'])
                self.assertNotEqual(0, result.returncode)
    def test_running_application_prevents_refresh_lock(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = subprocess.Popen(['flock', '--shared', str(root / 'application.lock'), 'cat'], stdin=subprocess.PIPE)
            try:
                import time
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    probe = subprocess.run(['flock', '--exclusive', '--nonblock', str(root / 'application.lock'), 'true'])
                    if probe.returncode:
                        break
                    time.sleep(.01)
                else:
                    self.fail('application did not obtain shared lock')
                with self.assertRaises(UpdateError):
                    with application_lock(root):
                        self.fail('updater acquired lock while app was running')
            finally:
                app.stdin.close(); app.wait(timeout=5)

    def test_builtin_skills_do_not_collide_when_profiles_are_combined(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'one', Path(directory) / 'two'
            source.mkdir(); target.mkdir()
            (source / '.system').mkdir(); (target / '.system').mkdir()
            skill = source / 'custom'; skill.mkdir(); (skill / 'SKILL.md').write_text('fixture')
            link_profile_skills(source, target)
            self.assertEqual('fixture', (target / 'custom/SKILL.md').read_text())
            self.assertFalse((target / '.system').is_symlink())

    def test_candidate_database_changes_never_modify_previous_data(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'old', Path(directory) / 'new'
            source.mkdir(); (source / '.n8n').mkdir()
            database = source / '.n8n/database.sqlite'
            with sqlite3.connect(database) as db:
                db.execute('create table fixture (value text)')
                db.execute('insert into fixture values (?)', ('keep',))
            (source / '.n8n/config').write_text('fixture configuration')
            before = data_fingerprint(source)
            backup_data(source, target)
            self.assertTrue(require_unchanged_data(source, before))
            with sqlite3.connect(target / '.n8n/database.sqlite') as db:
                db.execute('delete from fixture')
            with sqlite3.connect(database) as db:
                self.assertEqual([('keep',)], db.execute('select * from fixture').fetchall())
            self.assertEqual('fixture configuration', (target / '.n8n/config').read_text())

    def test_linked_data_is_not_followed_into_live_or_external_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'old', Path(directory) / 'new'
            source.mkdir()
            (source / 'link').symlink_to('/tmp')
            with self.assertRaises(UpdateError):
                backup_data(source, target)


if __name__ == '__main__':
    unittest.main()
