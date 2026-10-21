import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import validate


class ValidationIsolationTests(unittest.TestCase):
    def test_fresh_shell_has_writable_caches_and_isolated_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            with patch.dict(os.environ, {'PATH': '/usr/bin', 'GH_TOKEN': 'fixture-token'}, clear=True):
                env = validate.environment(runtime)
            self.assertEqual(str(runtime / 'npm-cache'), env.get('npm_config_cache'))
            self.assertEqual(str(runtime / 'data'), env.get('XDG_DATA_HOME'))
            self.assertNotIn('GH_TOKEN', env)
            self.assertEqual(str(runtime / 'codex'), env['CODEX_HOME'])


if __name__ == '__main__':
    unittest.main()
