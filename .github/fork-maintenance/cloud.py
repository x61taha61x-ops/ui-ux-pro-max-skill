#!/usr/bin/env python3
"""Refresh a versioned cloud runtime; switch one pointer only after every gate passes."""
import argparse
import fcntl
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from contextlib import contextmanager

import maintenance as m
import validate


@contextmanager
def application_lock(base):
    """Exclude application starts/writes for the entire snapshot/build/promotion."""
    with (Path(base) / 'application.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise m.UpdateError('n8n is running or its data is in use; existing runtime retained') from error
        yield


def data_fingerprint(source):
    source = Path(source)
    digest = hashlib.sha256()
    if not source.exists():
        return digest.hexdigest()
    for path in sorted(source.rglob('*')):
        if path.is_symlink():
            raise m.UpdateError('Linked n8n data needs manual review')
        if not path.is_file() or path.name.endswith('-shm'):
            continue
        if path.name.endswith('-wal') and path.stat().st_size == 0:
            continue
        digest.update(str(path.relative_to(source)).encode() + b'\0')
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        digest.update(b'\0')
    return digest.hexdigest()


def require_unchanged_data(source, expected):
    if data_fingerprint(source) != expected:
        raise m.UpdateError('n8n data changed during validation; existing runtime retained')
    return True


def require_stopped_application():
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1', 5678)) == 0:
            raise m.UpdateError('An application is using port 5678. Keep it running; refresh at the next stopped startup or stop it deliberately first.')


def backup_data(source, target):
    source, target = Path(source), Path(target)
    if not source.exists():
        target.mkdir(parents=True)
        return
    if any(p.is_symlink() for p in source.rglob('*')):
        raise m.UpdateError('Linked n8n data needs manual review; no links were followed')
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('database.sqlite*'))
    database = source / '.n8n/database.sqlite'
    if database.exists():
        destination = target / '.n8n/database.sqlite'
        destination.parent.mkdir(exist_ok=True)
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as old:
            with sqlite3.connect(destination) as new:
                old.backup(new)
                if new.execute('pragma integrity_check').fetchone()[0] != 'ok':
                    raise m.UpdateError('SQLite backup integrity check failed')


def remote_tip(url, branch):
    result = m.run(['git', 'ls-remote', url, f'refs/heads/{branch}']).split()
    if not result:
        raise m.UpdateError(f'Missing branch {branch} at {url}')
    return result[0]


def require_published_source(root, url, branch):
    m.git(root, 'fetch', '--no-tags', url, branch)
    try:
        m.git(root, 'merge-base', '--is-ancestor', m.head(root), 'FETCH_HEAD')
    except m.UpdateError as error:
        raise m.UpdateError(f'Unpublished commits in {root}; review before changing active runtime') from error


def link_profile_skills(source, target):
    for skill in source.iterdir():
        # Every native Codex profile contains the same reserved built-in namespace.
        if skill.name == '.system':
            continue
        registered = target / skill.name
        if registered.exists() or registered.is_symlink():
            if registered.is_symlink() and registered.resolve() == skill.resolve():
                continue
            raise m.UpdateError('Skill registration collision: ' + skill.name)
        registered.symlink_to(skill, target_is_directory=True)


def combine_profiles(release):
    profile = release / 'native-codex'
    if not profile.exists():
        shutil.copytree(release / 'runtime/superpowers/codex', profile, symlinks=True)
    registered = profile / 'skills'; registered.mkdir(exist_ok=True)
    for name in ['ui-ux-pro-max-skill', 'gstack']:
        link_profile_skills(release / 'runtime' / name / 'codex/skills', registered)
    env = validate.environment(release / 'integration')
    env['CODEX_HOME'] = str(profile)
    for name in m.REPOS:
        validate.skills(release / 'repos' / name, env,
                        ['superpowers:using-superpowers', 'ui-ux-pro-max', 'gstack', 'browse'])


def wrappers(release):
    binaries = release / 'bin'; binaries.mkdir()
    activate = release / 'env.sh'
    activate.write_text('#!/usr/bin/env bash\n'
        'source /workspace/.onboarding/activate.sh\n'
        f'export MAINTENANCE_RELEASE={release}\n'
        f'export MAINTENANCE_CODEX_PROFILE={release}/native-codex\n'
        f'export PATH={binaries}:{release}/runtime/n8n/tools/node_modules/.bin:$PATH\n'
        f'export GSTACK_ROOT={release}/runtime/gstack/codex/skills/gstack\n'
        f'export GSTACK_STATE_ROOT={release}/runtime/gstack/gstack-state\n'
        f'export BROWSE_STATE_FILE={release}/runtime/gstack/browse/state.json\n'
        f'export N8N_USER_FOLDER={release}/n8n-data\n'
        'export DB_TYPE=sqlite DB_SQLITE_DATABASE=database.sqlite\n')
    codex = shutil.which('codex')
    tini = shutil.which('tini') or '/workspace/.onboarding/tools/tini-root/usr/bin/tini'
    commands = {
        'n8n': f'cd {release}/repos/n8n\nexec flock --shared --nonblock --conflict-exit-code=75 {release.parent.parent}/application.lock node packages/cli/bin/n8n "$@"',
        'uipro': f'exec node {release}/repos/ui-ux-pro-max-skill/cli/dist/index.js "$@"',
        'browse': f'exec {release}/repos/gstack/browse/dist/browse "$@"',
        'codex-with-skills': f'exec env CODEX_HOME={release}/native-codex {codex} "$@"',
        'brainstorm-server': f'exec {tini} -s -- bash {release}/repos/superpowers/skills/brainstorming/scripts/start-server.sh --foreground "$@"',
    }
    for name, command in commands.items():
        path = binaries / name
        path.write_text(f'#!/usr/bin/env bash\nset -euo pipefail\nsource {activate}\n{command}\n')
        path.chmod(0o700)
    start = release / 'start-n8n.sh'
    start.write_text('#!/usr/bin/env bash\nset -euo pipefail\n'
        f'source {activate}\nexport N8N_LISTEN_ADDRESS=127.0.0.1 N8N_PORT=5678\n'
        'export N8N_DIAGNOSTICS_ENABLED=false N8N_VERSION_NOTIFICATIONS_ENABLED=false N8N_TEMPLATES_ENABLED=false\n'
        f'exec {binaries}/n8n start\n')


def update(base, verify=False):
    base.mkdir(parents=True, exist_ok=True)
    active = base / 'current'
    previous = active.resolve() if active.exists() else None
    manifest = json.loads((previous / 'manifest.json').read_text()) if previous else None
    originals = {name: Path('/workspace') / name for name in m.REPOS}
    sources = {name: previous / 'repos' / name if previous else originals[name] for name in m.REPOS}
    expected = {str(root): m.head(root) for root in set(originals.values()) | set(sources.values())}
    m.guard(expected)
    if manifest:
        for name, root in originals.items():
            if m.head(root) != manifest['original_heads'][name]:
                _, branch = m.REPOS[name]
                require_published_source(root, f'https://github.com/{m.OWNER}/{name}.git', branch)
    remotes = {}
    for name, (upstream, branch) in m.REPOS.items():
        remotes[name] = {'fork': remote_tip(f'https://github.com/{m.OWNER}/{name}.git', branch),
                         'upstream': remote_tip(f'https://github.com/{upstream}.git', branch)}
    if manifest and not verify and remotes == manifest['remotes'] and all(m.head(sources[n]) == manifest['commits'][n] for n in m.REPOS):
        return {'status': 'unchanged'}
    with application_lock(base):
        return prepare_release(base, active, previous, originals, sources, expected, remotes)


def prepare_release(base, active, previous, originals, sources, expected, remotes):
    require_stopped_application()
    release = base / 'releases' / time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    (release / 'repos').mkdir(parents=True)
    (release / 'runtime').mkdir()
    result = {'status': 'attention', 'release': str(release)}
    (base / 'latest.json').write_text(json.dumps(result, indent=2))
    try:
        commits = {}
        for name, (upstream, branch) in m.REPOS.items():
            prepared = m.prepare(sources[name], f'https://github.com/{m.OWNER}/{name}.git',
                                 f'https://github.com/{upstream}.git', branch, release / 'repos' / name)
            commits[name] = prepared['candidate']
            remotes[name] = {'fork': prepared['fork'], 'upstream': prepared['upstream']}
        old_data = previous / 'n8n-data' if previous else Path('/workspace/.onboarding/n8n-app')
        before_data = data_fingerprint(old_data)
        backup_data(old_data, release / 'n8n-data')
        require_unchanged_data(old_data, before_data)

        def gate():
            for name in m.REPOS:
                with (release / f'{name}.log').open('w') as log:
                    command = [sys.executable, str(Path(__file__).with_name('validate.py')), name,
                               str(release / 'repos' / name), str(release / 'runtime' / name)]
                    verdict = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=7200)
                if verdict.returncode:
                    raise m.UpdateError(f'{name} failed validation. Existing environment retained. See {release}/{name}.log')
                m.mark_validated(release / 'repos' / name)
            # Migrate only the copied SQLite database. This command does not activate workflows.
            env = validate.environment(release / 'runtime/n8n')
            env.update({'N8N_USER_FOLDER': str(release / 'n8n-data'), 'DB_TYPE': 'sqlite',
                        'DB_SQLITE_DATABASE': 'database.sqlite'})
            with (release / 'n8n-data-migration.log').open('w') as log:
                subprocess.run(['node', 'packages/cli/bin/n8n', 'list:workflow', '--onlyId'],
                               cwd=release / 'repos/n8n', env=env, stdout=log,
                               stderr=subprocess.STDOUT, check=True, timeout=180)
            with sqlite3.connect(release / 'n8n-data/.n8n/database.sqlite') as db:
                if db.execute('pragma integrity_check').fetchone()[0] != 'ok':
                    raise m.UpdateError('Migrated SQLite copy failed integrity check')
            combine_profiles(release)
            wrappers(release)
            metadata = {'commits': commits, 'remotes': remotes,
                        'original_heads': {name: m.head(root) for name, root in originals.items()},
                        'previous': str(previous) if previous else None, 'validated': True}
            (release / 'manifest.json').write_text(json.dumps(metadata, indent=2))
            require_stopped_application()
            require_unchanged_data(old_data, before_data)
            return True

        m.activate(active, release, expected, gate)
        result = {'status': 'updated', 'release': str(release), 'commits': commits}
        (base / 'latest.json').write_text(json.dumps(result, indent=2))
        return result
    except Exception as error:
        result['error'] = str(error)
        (base / 'latest.json').write_text(json.dumps(result, indent=2))
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, default=Path('/workspace/.onboarding/updates'))
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    args.base.mkdir(parents=True, exist_ok=True)
    with (args.base / 'update.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = update(args.base, args.verify)
        except Exception as error:
            result = {'status': 'attention', 'error': str(error)}
        if result['status'] != 'unchanged':
            signature = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
            notice = args.base / 'last-notice'
            if not notice.exists() or notice.read_text() != signature:
                print(json.dumps(result), flush=True)
                notice.write_text(signature)
        return 1 if result['status'] == 'attention' else 0


if __name__ == '__main__':
    sys.exit(main())
