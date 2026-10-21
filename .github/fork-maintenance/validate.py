#!/usr/bin/env python3
"""Install and test candidates in isolated runtime profiles, never live profiles."""
import argparse
import functools
import http.server
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


def command(args, cwd, env, timeout=3600):
    print('+ ' + ' '.join(str(x) for x in args), flush=True)
    subprocess.run([str(x) for x in args], cwd=cwd, env=env, check=True, timeout=timeout)


def environment(runtime):
    runtime.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    # Candidate package lifecycle scripts do not need fork push credentials.
    for key in list(env):
        if re.search(r'(TOKEN|SECRET|PASSWORD|API_KEY)$', key, re.I):
            env.pop(key)
    env.update({
        'npm_config_cache': env.get('npm_config_cache', str(runtime / 'npm-cache')),
        'XDG_DATA_HOME': env.get('XDG_DATA_HOME', str(runtime / 'data')),
        'XDG_CACHE_HOME': env.get('XDG_CACHE_HOME', str(runtime / 'cache')),
        'CI': 'true', 'CODEX_HOME': str(runtime / 'codex'),
        'GSTACK_STATE_ROOT': str(runtime / 'gstack-state'),
        'BROWSE_STATE_FILE': str(runtime / 'browse/state.json'),
        'GSTACK_SKIP_FONTS': '1', 'GSTACK_SKIP_GBRAIN_REGEN': '1',
        'GSTACK_CHROMIUM_NO_SANDBOX': '1',
        'PATH': str(runtime / 'tools/node_modules/.bin') + os.pathsep + env['PATH'],
    })
    env.pop('GSTACK_ROOT', None)
    (runtime / 'codex').mkdir(exist_ok=True)
    return env


def skills(root, env, required):
    with (Path(env['CODEX_HOME']).parent / 'skill-discovery-errors.log').open('w') as error_log:
        proc = subprocess.Popen(['codex', 'app-server', '--stdio'], cwd=root, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=error_log, text=True)
        def request(rid, method, params):
            proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params}) + '\n')
            proc.stdin.flush()
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if not select.select([proc.stdout], [], [], 1)[0]:
                    continue
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError('Codex skill discovery process stopped')
                data = json.loads(line)
                if data.get('id') == rid:
                    if 'error' in data:
                        raise RuntimeError(data['error'])
                    return data['result']
            raise TimeoutError('Codex skills/list timed out')
        try:
            request(1, 'initialize', {'clientInfo': {'name': 'fork-validation', 'version': '1'},
                                    'capabilities': {'experimentalApi': True}})
            proc.stdin.write('{"jsonrpc":"2.0","method":"initialized"}\n'); proc.stdin.flush()
            data = request(2, 'skills/list', {'cwds': [str(root)], 'forceReload': True})
            entry = data['data'][0]
            if entry.get('errors'):
                raise RuntimeError(entry['errors'])
            found = {s['name'] for s in entry['skills'] if s.get('enabled')}
            missing = set(required) - found
            if missing:
                raise RuntimeError('Missing enabled Codex skills: ' + ', '.join(sorted(missing)))
            print('Native Codex discovery passed: ' + ', '.join(required), flush=True)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()


def superpowers(root, runtime, env):
    command(['node', '--test', 'tests/pi/test-pi-extension.mjs'], root, env)
    command(['bash', 'tests/hooks/test-session-start.sh'], root, env)
    tests = root / 'tests/brainstorm-server'
    command(['npm', 'ci'], tests, env)
    tini = shutil.which('tini', path=env['PATH'])
    if not tini:
        candidate = Path('/workspace/.onboarding/tools/tini-root/usr/bin/tini')
        if candidate.exists():
            tini = str(candidate)
    if not tini:
        raise RuntimeError('tini is required to reap orphaned brainstorm processes')
    command([tini, '-s', '--', 'npm', 'test'], tests, env)
    command(['codex', 'plugin', 'marketplace', 'add', str(root), '--json'], root, env)
    command(['codex', 'plugin', 'add', 'superpowers@superpowers-dev'], root, env)
    skills(root, env, ['superpowers:using-superpowers', 'superpowers:brainstorming'])


def ui(root, runtime, env):
    cli = root / 'cli'
    command(['npm', 'ci'], cli, env)
    command(['npm', 'run', 'typecheck'], cli, env)
    command(['npm', 'run', 'build'], cli, env)
    command(['node', 'dist/index.js', '--help'], cli, env)
    command(['python3', '-m', 'unittest', 'discover', '-s', 'src/ui-ux-pro-max/scripts/tests', '-p', 'test_*.py'], root, env)
    project = runtime / 'ui-project'; project.mkdir(exist_ok=True)
    command(['node', str(cli / 'dist/index.js'), 'init', '--ai', 'codex', '--offline'], project, env)
    installed = project / '.agents/skills/ui-ux-pro-max/scripts/search.py'
    result = subprocess.check_output(['python3', str(installed), 'saas dashboard', '--domain', 'product', '-n', '2'], env=env, text=True)
    if 'SaaS' not in result or 'Found:' not in result:
        raise RuntimeError('Installed UI search did not return expected SaaS results')
    print('Installed UI CLI and Python search passed', flush=True)
    registered = Path(env['CODEX_HOME']) / 'skills'; registered.mkdir(exist_ok=True)
    for skill in (project / '.agents/skills').iterdir():
        target = registered / skill.name
        if not target.exists():
            target.symlink_to(skill, target_is_directory=True)
    skills(root, env, ['ui-ux-pro-max'])


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def gstack(root, runtime, env):
    command(['bun', 'install', '--frozen-lockfile'], root, env)
    command(['./setup', '--host', 'codex', '--prefix', '--no-team'], root, env)
    command(['bun', 'run', 'typecheck'], root, env)
    command(['bun', 'test', 'browse/test/config.test.ts'], root, env)
    command(['bunx', '--no-install', 'playwright', 'install', 'chromium'], root, env)
    skills(root, env, ['gstack', 'browse'])
    site = runtime / 'probe'; site.mkdir(exist_ok=True)
    (site / 'index.html').write_text('<title>maintenance-probe</title><button id="go" onclick="this.textContent=\'clicked\'">ready</button>')
    handler = functools.partial(QuietHandler, directory=str(site))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    browse = root / 'browse/dist/browse'
    try:
        command([browse, 'goto', f'http://127.0.0.1:{server.server_port}/'], root, env, 120)
        script = 'document.querySelector("#go").click(); JSON.stringify({title:document.title,text:document.querySelector("#go").textContent})'
        result = subprocess.check_output([str(browse), 'js', script], cwd=root, env=env, text=True, timeout=30)
        if 'maintenance-probe' not in result or 'clicked' not in result:
            raise RuntimeError('Chromium did not render and interact with the probe')
        print('Real Chromium HTTP navigation and button interaction passed', flush=True)
    finally:
        subprocess.run([str(browse), 'stop'], cwd=root, env=env, timeout=60, check=False)
        server.shutdown(); server.server_close()


def n8n(root, runtime, env):
    package = json.loads((root / 'package.json').read_text())
    manager = package['packageManager'].split('+')[0]
    if not re.fullmatch(r'pnpm@\d+\.\d+\.\d+', manager):
        raise RuntimeError('Unknown pnpm packageManager pin: ' + manager)
    command(['npm', 'install', '--prefix', str(runtime / 'tools'), '--no-package-lock', manager], root, env)
    command(['pnpm', 'agent:setup', 'install', '--log-dir', str(runtime / 'install-logs')], root, env)
    # Retry the package's supported installer if a prior failure left an empty bin directory.
    for package_dir in (root / 'node_modules/.pnpm').glob('@vscode+ripgrep@*/node_modules/@vscode/ripgrep'):
        if not (package_dir / 'bin/rg').exists():
            command(['node', 'lib/postinstall.js', '--force'], package_dir, env)
        command([package_dir / 'bin/rg', '--version'], root, env)
    build_env = env.copy(); build_env['NODE_OPTIONS'] = '--max-old-space-size=4096'
    with (runtime / 'build.log').open('w') as log:
        subprocess.run(['pnpm', 'exec', 'turbo', 'run', 'build', '--filter=n8n...', '--concurrency=1'],
                       cwd=root, env=build_env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=5400)
    print('Complete n8n application/editor build passed; see build.log', flush=True)
    command(['pnpm', '--filter', '@n8n/di', 'test', '--maxWorkers=1'], root, env)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    app_env = env.copy()
    app_env.update({'N8N_USER_FOLDER': str(runtime / 'n8n-smoke-data'), 'N8N_PORT': str(port),
                    'N8N_LISTEN_ADDRESS': '127.0.0.1', 'N8N_DIAGNOSTICS_ENABLED': 'false',
                    'N8N_VERSION_NOTIFICATIONS_ENABLED': 'false', 'N8N_TEMPLATES_ENABLED': 'false'})
    with (runtime / 'n8n-smoke.log').open('w') as log:
        proc = subprocess.Popen(['node', 'packages/cli/bin/n8n', 'start'], cwd=root, env=app_env, stdout=log, stderr=subprocess.STDOUT)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f'http://127.0.0.1:{port}'
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError('n8n exited before readiness; inspect n8n-smoke.log')
                try:
                    data = json.load(opener.open(url + '/healthz/readiness', timeout=3))
                    if data.get('status') == 'ok':
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.5)
            else:
                raise RuntimeError('n8n readiness deadline exceeded')
            html = opener.open(url, timeout=10).read().decode()
            if '<html' not in html or 'type="module"' not in html:
                raise RuntimeError('n8n did not serve the built editor')
            settings = json.load(opener.open(url + '/rest/settings', timeout=10))
            if not isinstance(settings.get('data'), dict):
                raise RuntimeError('n8n settings endpoint did not return application data')
            print('n8n SQLite startup, readiness, editor and settings HTTP requests passed', flush=True)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=45)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('repository', choices=['superpowers', 'ui-ux-pro-max-skill', 'gstack', 'n8n'])
    parser.add_argument('checkout', type=Path)
    parser.add_argument('runtime', type=Path)
    args = parser.parse_args()
    root, runtime = args.checkout.resolve(), args.runtime.resolve()
    env = environment(runtime)
    functions = {'superpowers': superpowers, 'ui-ux-pro-max-skill': ui, 'gstack': gstack, 'n8n': n8n}
    functions[args.repository](root, runtime, env)
    (runtime / 'passed.json').write_text(json.dumps({'repository': args.repository, 'passed': True}))


if __name__ == '__main__':
    main()
