import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import maintenance as m


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.up = self.root / 'upstream'
        self.up.mkdir()
        git(self.up, 'init', '-q', '-b', 'main')
        self.identity(self.up)
        self.commit(self.up, 'base.txt', 'base')
        self.remote = self.root / 'fork.git'
        subprocess.run(['git', 'clone', '-q', '--bare', str(self.up), str(self.remote)], check=True)
        self.source = self.root / 'source'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(self.source)], check=True)
        self.identity(self.source)
        self.base = git(self.source, 'rev-parse', 'HEAD')

    def tearDown(self):
        self.tmp.cleanup()

    def identity(self, root):
        git(root, 'config', 'user.email', 'test@example.invalid')
        git(root, 'config', 'user.name', 'Fixture')

    def commit(self, root, name, content):
        (root / name).write_text(content)
        git(root, 'add', name)
        git(root, '-c', 'commit.gpgsign=false', 'commit', '-qm', name)
        return git(root, 'rev-parse', 'HEAD')

    def prepare(self):
        return m.prepare(self.source, str(self.remote), str(self.up), 'main', self.root / 'candidate')

    def test_merge_preserves_own_commits_and_source_tree(self):
        own = self.commit(self.source, 'mine.txt', 'my work')
        git(self.source, 'push', '-q', 'origin', 'HEAD:main')
        newest = self.commit(self.up, 'upstream.txt', 'new upstream')
        result = self.prepare()
        self.assertTrue(result['changed'])
        self.assertEqual(own, git(self.source, 'rev-parse', 'HEAD'))
        candidate = self.root / 'candidate'
        self.assertEqual('my work', (candidate / 'mine.txt').read_text())
        git(candidate, 'merge-base', '--is-ancestor', own, 'HEAD')
        git(candidate, 'merge-base', '--is-ancestor', newest, 'HEAD')
        self.assertEqual(own, git(self.remote, 'rev-parse', 'main'))

    def test_conflict_never_changes_fork_or_source(self):
        own = self.commit(self.source, 'base.txt', 'mine')
        git(self.source, 'push', '-q', 'origin', 'HEAD:main')
        self.commit(self.up, 'base.txt', 'theirs')
        with self.assertRaises(m.UpdateError):
            self.prepare()
        self.assertEqual(own, git(self.remote, 'rev-parse', 'main'))
        self.assertEqual(own, git(self.source, 'rev-parse', 'HEAD'))
        self.assertEqual('mine', (self.source / 'base.txt').read_text())

    def test_dirty_checkout_is_not_stashed_or_overwritten(self):
        (self.source / 'untracked.txt').write_text('keep this')
        with self.assertRaises(m.UpdateError):
            self.prepare()
        self.assertEqual('keep this', (self.source / 'untracked.txt').read_text())

    def test_rejected_validation_leaves_active_release_unchanged(self):
        old, new = self.root / 'old', self.root / 'new'
        old.mkdir(); new.mkdir()
        active = self.root / 'current'
        active.symlink_to(old)
        with self.assertRaises(m.UpdateError):
            m.activate(active, new, {str(self.source): self.base}, lambda: False)
        self.assertEqual(old, active.resolve())

    def test_success_switches_active_release_after_validation(self):
        new = self.root / 'new'; new.mkdir()
        active = self.root / 'current'
        m.activate(active, new, {str(self.source): self.base}, lambda: True)
        self.assertEqual(new, active.resolve())

    def test_source_changed_during_build_prevents_activation(self):
        new = self.root / 'new'; new.mkdir()
        def validate():
            (self.source / 'mine.txt').write_text('concurrent edit')
            return True
        active = self.root / 'current'
        with self.assertRaises(m.UpdateError):
            m.activate(active, new, {str(self.source): self.base}, validate)
        self.assertFalse(active.exists())

    def test_remote_advance_is_preserved_without_force_push(self):
        self.commit(self.up, 'new.txt', 'upstream')
        result = self.prepare()
        m.mark_validated(self.root / 'candidate')
        own = self.commit(self.source, 'remote-only.txt', 'concurrent work')
        git(self.source, 'push', '-q', 'origin', 'HEAD:main')
        with self.assertRaisesRegex(m.UpdateError, 'Remote advanced'):
            m.publish(self.root / 'candidate', 'main', result['fork'])
        self.assertEqual(own, git(self.remote, 'rev-parse', 'main'))

    def test_unchanged_upstream_is_silent_candidate(self):
        result = self.prepare()
        self.assertFalse(result['changed'])

    def test_only_validated_descendant_can_publish(self):
        self.commit(self.up, 'new.txt', 'upstream')
        result = self.prepare()
        with self.assertRaises(m.UpdateError):
            m.publish(self.root / 'candidate', 'main', result['fork'])
        self.assertEqual(self.base, git(self.remote, 'rev-parse', 'main'))

    def test_startup_accepts_published_commits_but_retains_unpublished_work(self):
        from cloud import require_published_source
        own = self.commit(self.source, 'published.txt', 'my changes')
        with self.assertRaisesRegex(m.UpdateError, 'Unpublished'):
            require_published_source(self.source, str(self.remote), 'main')
        self.assertEqual(own, git(self.source, 'rev-parse', 'HEAD'))
        git(self.source, 'push', '-q', 'origin', 'HEAD:main')
        require_published_source(self.source, str(self.remote), 'main')
        self.assertEqual(own, git(self.source, 'rev-parse', 'HEAD'))

    def test_validated_update_can_publish_without_rewriting_history(self):
        newest = self.commit(self.up, 'new.txt', 'upstream')
        result = self.prepare()
        m.mark_validated(self.root / 'candidate')
        m.publish(self.root / 'candidate', 'main', result['fork'])
        self.assertEqual(newest, git(self.remote, 'rev-parse', 'main'))
        git(self.remote, 'merge-base', '--is-ancestor', self.base, 'main')


if __name__ == '__main__':
    unittest.main()
