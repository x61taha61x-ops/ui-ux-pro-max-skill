#!/usr/bin/env python3
"""Preserve Git history and publish only validated candidates."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOS = {
    'superpowers': ('obra/superpowers', 'main'),
    'ui-ux-pro-max-skill': ('nextlevelbuilder/ui-ux-pro-max-skill', 'main'),
    'gstack': ('garrytan/gstack', 'main'),
    'n8n': ('n8n-io/n8n', 'master'),
}
OWNER = 'x61taha61x-ops'


class UpdateError(RuntimeError):
    pass


def run(args, cwd=None, env=None, timeout=180):
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=timeout)
    if result.returncode:
        raise UpdateError(result.stdout[-6000:] or f'{args[0]} exited {result.returncode}')
    return result.stdout.strip()


def git(root, *args):
    return run(['git', '-C', root, *args])


def head(root):
    return git(root, 'rev-parse', '--verify', 'HEAD^{commit}')


def clean(root):
    if git(root, 'status', '--porcelain', '--untracked-files=normal'):
        raise UpdateError(f'Local changes in {root}; nothing was stashed, reset or overwritten')


def guard(expected):
    for root, commit in expected.items():
        clean(root)
        if head(root) != commit:
            raise UpdateError(f'Source advanced while validating: {root}')


def prepare(source, fork_url, upstream_url, branch, destination):
    source, destination = Path(source), Path(destination)
    clean(source)
    base = head(source)
    run(['git', 'clone', '-q', '--no-hardlinks', '--no-checkout', source, destination])
    git(destination, 'remote', 'set-url', 'origin', fork_url)
    git(destination, 'config', 'user.name', 'Fork maintenance')
    git(destination, 'config', 'user.email', 'fork-maintenance@users.noreply.github.com')
    git(destination, 'config', 'commit.gpgsign', 'false')
    git(destination, 'checkout', '-q', '--detach', base)
    git(destination, 'fetch', '--no-tags', 'origin', f'{branch}:refs/remotes/origin/{branch}')
    fork = git(destination, 'rev-parse', f'refs/remotes/origin/{branch}')
    git(destination, 'fetch', '--no-tags', upstream_url, branch)
    upstream = git(destination, 'rev-parse', 'FETCH_HEAD')
    try:
        git(destination, 'merge', '--no-edit', fork)
        git(destination, 'merge', '--no-edit', upstream)
    except UpdateError as error:
        raise UpdateError(f'Merge conflict or rejected merge; source and fork unchanged. {error}') from error
    clean(destination)
    candidate = head(destination)
    return {'source': str(source), 'base': base, 'fork': fork, 'upstream': upstream,
            'candidate': candidate, 'changed': candidate != fork}


def mark_validated(root):
    clean(root)
    marker = Path(git(root, 'rev-parse', '--absolute-git-dir')) / 'maintenance-validated.json'
    marker.write_text(json.dumps({'commit': head(root), 'validated_at': int(time.time())}))


def publish(candidate, branch, expected_fork):
    clean(candidate)
    marker = Path(git(candidate, 'rev-parse', '--absolute-git-dir')) / 'maintenance-validated.json'
    if not marker.exists() or json.loads(marker.read_text()).get('commit') != head(candidate):
        raise UpdateError('Candidate has not passed validation at its current commit')
    actual = git(candidate, 'ls-remote', 'origin', f'refs/heads/{branch}').split()[0]
    if actual != expected_fork:
        raise UpdateError('Remote advanced during validation; refusing to overwrite it')
    git(candidate, 'merge-base', '--is-ancestor', expected_fork, 'HEAD')
    # No force and no force-with-lease. A concurrent update is rejected by Git.
    git(candidate, 'push', 'origin', f'HEAD:refs/heads/{branch}')


def activate(active, release, expected, validator):
    active, release = Path(active), Path(release)
    guard(expected)
    if not validator():
        raise UpdateError('Validation failed; last good environment was retained')
    guard(expected)
    temporary = active.with_name(active.name + f'.next-{os.getpid()}')
    temporary.symlink_to(release.resolve(), target_is_directory=True)
    os.replace(temporary, active)


def notify(repo, report):
    """One commit comment per distinct update/failure; silence for no change."""
    if report['status'] == 'unchanged':
        return
    fields = ['status', 'candidate'] if report['status'] == 'updated' else ['status', 'fork', 'upstream', 'error']
    key = hashlib.sha256(json.dumps({k: report.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()
    marker = f'<!-- fork-maintenance:{key} -->'
    commit = report.get('candidate') if report['status'] == 'updated' else report.get('fork') or os.environ.get('GITHUB_SHA')
    if not commit or not all(c in '0123456789abcdef' for c in commit) or len(commit) != 40:
        raise UpdateError('Notification requires a published commit SHA')
    endpoint = f'repos/{repo}/commits/{commit}/comments'
    # Older gh versions support --paginate but not --slurp; decode each JSON page.
    stream = run(['gh', 'api', endpoint, '--paginate'])
    comments = []
    decoder = json.JSONDecoder()
    while stream.strip():
        page, end = decoder.raw_decode(stream.lstrip())
        comments.extend(page)
        stream = stream.lstrip()[end:]
    if any(marker in (comment.get('body') or '') for comment in comments):
        return
    title = '[Fork maintenance] ' + ('Updated' if report['status'] == 'updated' else 'Action required')
    summary = report.get('error') or f'Validated update: {report.get("fork")} → {report.get("candidate")}'
    run_url = os.environ.get('GITHUB_SERVER_URL', 'https://github.com') + '/' + repo + '/actions/runs/' + os.environ.get('GITHUB_RUN_ID', '')
    body = f'{title}\n\n@{OWNER}\n\n{summary[:4000]}\n\nRun: {run_url}\n\n{marker}'
    payload = json.dumps({'body': body})
    result = subprocess.run(['gh', 'api', endpoint, '-X', 'POST', '--input', '-'],
                            input=payload, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise UpdateError('Could not deliver the required update/intervention notification: ' + result.stderr[-1500:])


def daily(args):
    upstream, branch = REPOS[args.repo]
    report = {'repository': f'{OWNER}/{args.repo}', 'status': 'attention'}
    try:
        stage = Path(args.stage).resolve()
        stage.mkdir(parents=True, exist_ok=True)
        result = prepare(Path(args.source).resolve(), f'https://github.com/{OWNER}/{args.repo}.git',
                         f'https://github.com/{upstream}.git', branch, stage / 'candidate')
        report.update(result)
        if not result['changed'] and not args.verify_unchanged:
            report['status'] = 'unchanged'
        else:
            validate = [sys.executable, str(Path(__file__).with_name('validate.py')),
                        args.repo, str(stage / 'candidate'), str(stage / 'runtime')]
            with (stage / 'validation.log').open('w') as log:
                verdict = subprocess.run(validate, stdout=log, stderr=subprocess.STDOUT, timeout=5400)
            if verdict.returncode:
                raise UpdateError(f'{args.repo} validation failed (exit {verdict.returncode}); see validation.log. Fork unchanged.')
            mark_validated(stage / 'candidate')
            if args.push and result['changed']:
                publish(stage / 'candidate', branch, result['fork'])
                report['status'] = 'updated'
            else:
                report['status'] = 'validated' if result['changed'] else 'unchanged'
    except Exception as error:
        report['error'] = str(error)
    Path(args.report).write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    return 1 if report['status'] == 'attention' else 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    update = sub.add_parser('daily')
    update.add_argument('--repo', choices=REPOS, required=True)
    update.add_argument('--source', required=True)
    update.add_argument('--stage', required=True)
    update.add_argument('--report', required=True)
    update.add_argument('--push', action='store_true')
    update.add_argument('--verify-unchanged', action='store_true')
    notice = sub.add_parser('notify')
    notice.add_argument('--repo', required=True)
    notice.add_argument('--report', required=True)
    args = parser.parse_args()
    if args.command == 'daily':
        return daily(args)
    notify(args.repo, json.loads(Path(args.report).read_text()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
