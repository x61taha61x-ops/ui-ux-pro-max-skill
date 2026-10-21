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


if __name__ == '__main__':
    unittest.main()
