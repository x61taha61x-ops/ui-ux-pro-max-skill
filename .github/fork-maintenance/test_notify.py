import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintenance as m


class NotificationTests(unittest.TestCase):
    def test_no_change_makes_no_notification_request(self):
        with patch.dict(os.environ, {'PATH': ''}):
            m.notify('owner/repo', {'status': 'unchanged'})

    def test_commit_comment_works_without_issues_and_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'comments.json').write_text('[]')
            stub = root / 'gh'
            stub.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
p=Path(os.environ['COMMENT_FIXTURE'])
items=json.loads(p.read_text())
assert '/commits/' in sys.argv[2] and sys.argv[2].endswith('/comments'), sys.argv
if '-X' in sys.argv:
 items.append(json.load(sys.stdin));p.write_text(json.dumps(items));print('{}')
else:
 print(json.dumps(items))
''')
            stub.chmod(0o700)
            env = {'PATH': str(root) + os.pathsep + os.environ['PATH'], 'COMMENT_FIXTURE': str(root / 'comments.json')}
            report = {'status': 'updated', 'candidate': 'a' * 40, 'fork': 'b' * 40}
            with patch.dict(os.environ, env):
                m.notify('owner/repo', report)
                m.notify('owner/repo', report)
            notices = json.loads((root / 'comments.json').read_text())
            self.assertEqual(1, len(notices))
            self.assertIn('@x61taha61x-ops', notices[0]['body'])

    def test_retried_failed_merge_does_not_repeat_the_same_owner_mention(self):
        # Same source/fork/error, but a fresh merge commit receives a different SHA.
        from unittest.mock import patch
        reports = []
        def api(args, **kwargs):
            if '-X' in args:
                reports.append(json.loads(kwargs['input']))
                return type('Result', (), {'returncode': 0, 'stderr': ''})()
            return type('Result', (), {'returncode': 0, 'stdout': json.dumps(reports), 'stderr': ''})()
        report = {'status': 'attention', 'candidate': 'a' * 40, 'fork': 'b' * 40,
                  'upstream': 'c' * 40, 'error': 'n8n validation failed'}
        with patch('maintenance.subprocess.run', side_effect=api):
            m.notify('owner/repo', report)
            report['candidate'] = 'd' * 40
            m.notify('owner/repo', report)
        self.assertEqual(1, len(reports))


if __name__ == '__main__':
    unittest.main()
