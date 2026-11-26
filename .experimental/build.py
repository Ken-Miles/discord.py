#!/usr/bin/env python3
"""Rebuilds the experimental branch: upstream discord.py master, plus the pull requests in prs.json.

    python .experimental/build.py                # build and test; updates the local experimental branch
    python .experimental/build.py --push         # also push it (and sync master) to origin
    python .experimental/build.py --no-test      # skip the tests

Every build starts from scratch in a temporary git worktree, so your own checkout isn't touched:

1. Fetch upstream master and each pull request.
2. Merge each pull request at its pinned commit. A pull request that has been merged upstream, closed,
   or conflicts with the others is left out and reported. Conflicts only in union_merge_files (where
   pull requests add things side by side, like new enums) are resolved by keeping both sides.
3. Apply the fix patches whose pull request was merged in.
4. Commit this tooling on top, with build.json describing what went in.
5. Run discord.py's tests, .experimental/tests, and pyright and ruff (if installed). Nothing is pushed unless they pass.

Pull requests stay at their pinned commit even if the author pushes more: the report says so, and you
move the pin in prs.json after reviewing the new commits. Upstream master is followed automatically.
Writes a Markdown report to --report (default: .experimental/report.md, which git ignores) and prints it.
Exit codes: 0 built (and pushed), 1 tests failed or the build broke, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CONFIG = json.loads((HERE / 'prs.json').read_text())
TOOLING = ['.experimental', '.github/workflows/experimental.yml']
HAS_RUFF = importlib.util.find_spec('ruff') is not None
IGNORED_IN_TOOLING = {'__pycache__', '.pytest_cache', 'report.md', 'report.md.attention'}


def git(*args: str, cwd: Path, check: bool = True, quiet: bool = False) -> str:
    result = subprocess.run(['git', *args], cwd=cwd, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)} failed:\n{result.stdout}{result.stderr}')
    return result.stdout.strip()


def pr_info(number: int) -> dict | None:
    """The pull request's state from the GitHub API, or None if it couldn't be fetched (e.g. rate limited)."""
    request = urllib.request.Request(f'https://api.github.com/repos/{CONFIG["upstream_repo"]}/pulls/{number}')
    request.add_header('Accept', 'application/vnd.github+json')
    if token := os.environ.get('GITHUB_TOKEN'):
        request.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except Exception as exc:  # the build still works offline; the report just can't say what changed
        print(f'warning: could not check PR #{number} on GitHub: {exc}', file=sys.stderr)
        return None


def union_resolve(worktree: Path, path: str) -> None:
    """Resolves a conflicted file by keeping both sides of every conflict."""
    with tempfile.TemporaryDirectory() as tmp:
        stages = {}
        for stage, name in ((1, 'base'), (2, 'ours'), (3, 'theirs')):
            content = subprocess.run(['git', 'show', f':{stage}:{path}'], cwd=worktree, capture_output=True).stdout
            stages[name] = Path(tmp, name)
            stages[name].write_bytes(content)
        subprocess.run(['git', 'merge-file', '--union', stages['ours'], stages['base'], stages['theirs']], check=False)
        (worktree / path).write_bytes(stages['ours'].read_bytes())
    git('add', path, cwd=worktree)


def merge_pr(worktree: Path, pr: dict) -> str | None:
    """Merges one pull request. Returns None on success, or why it was left out."""
    message = f'Merge discord.py PR #{pr["number"]}: {pr["title"]}\n\nhttps://github.com/{CONFIG["upstream_repo"]}/pull/{pr["number"]}'
    result = subprocess.run(
        ['git', 'merge', '--no-ff', '--no-edit', '-m', message, pr['sha']], cwd=worktree, capture_output=True, text=True
    )
    if result.returncode == 0:
        return None
    conflicted = git('diff', '--name-only', '--diff-filter=U', cwd=worktree).splitlines()
    unsafe = [path for path in conflicted if path not in CONFIG['union_merge_files']]
    if conflicted and not unsafe:
        for path in conflicted:
            union_resolve(worktree, path)
            if path.endswith('.py') and HAS_RUFF:
                # keeping both sides drops the blank lines between them; restore discord.py's formatting
                subprocess.run([sys.executable, '-m', 'ruff', 'format', '--quiet', path], cwd=worktree, check=False)
                git('add', path, cwd=worktree)
        git(
            'commit',
            '--no-edit',
            '-m',
            message + f'\n\nConflicts resolved by keeping both sides: {", ".join(conflicted)}',
            cwd=worktree,
        )
        return None
    git('merge', '--abort', cwd=worktree, check=False)
    return f'conflicts with upstream or an earlier PR in {", ".join(unsafe or conflicted) or "(unknown)"}'


def copy_tooling(source: Path, destination: Path) -> None:
    for item in TOOLING:
        src, dst = source / item, destination / item
        if dst.is_dir():
            shutil.rmtree(dst)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*IGNORED_IN_TOOLING))
        elif src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def run_tests(worktree: Path) -> tuple[bool, str]:
    commands = [
        [sys.executable, '-m', 'compileall', '-q', 'discord'],
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'tests', '.experimental/tests'],
    ]
    # the same checks as discord.py's lint workflow, when the tools are installed (CI installs them)
    if importlib.util.find_spec('pyright') is not None:
        commands.append([sys.executable, '-m', 'pyright', '--pythonpath', sys.executable])
    if HAS_RUFF:
        commands.append([sys.executable, '-m', 'ruff', 'format', '--check'])
    output = []
    for command in commands:
        result = subprocess.run(
            command, cwd=worktree, capture_output=True, text=True, env={**os.environ, 'PYTHONPATH': str(worktree)}
        )
        tail = '\n'.join((result.stdout + result.stderr).strip().splitlines()[-25:])
        output.append(f'$ {" ".join(command[1:])}\n{tail}')
        if result.returncode != 0:
            return False, '\n\n'.join(output)
    return True, '\n\n'.join(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--push', action='store_true', help='push the branch and sync master to origin')
    parser.add_argument('--no-test', action='store_true', help="don't run the tests (never use with --push)")
    parser.add_argument('--report', type=Path, help='where to write the Markdown report')
    args = parser.parse_args()
    if args.push and args.no_test:
        parser.error('--push needs the tests')

    branch = CONFIG['branch']
    notices: list[str] = []  # things a person should look at
    included: list[dict] = []
    skipped: list[dict] = []

    git('fetch', '--quiet', CONFIG['upstream'], CONFIG['upstream_branch'], cwd=REPO)
    upstream_sha = git('rev-parse', 'FETCH_HEAD', cwd=REPO)
    for pr in CONFIG['prs']:
        git('fetch', '--quiet', CONFIG['upstream'], f'pull/{pr["number"]}/head', cwd=REPO)
        if subprocess.run(['git', 'cat-file', '-e', f'{pr["sha"]}^{{commit}}'], cwd=REPO).returncode != 0:
            # the author force-pushed and the pinned commit isn't on the PR branch anymore
            git('fetch', '--quiet', CONFIG['upstream'], pr['sha'], cwd=REPO, check=False)

    worktree = Path(tempfile.mkdtemp(prefix='dpy-experimental-'))
    git('worktree', 'add', '--detach', '--quiet', str(worktree), upstream_sha, cwd=REPO)
    try:
        for pr in CONFIG['prs']:
            number = pr['number']
            info = pr_info(number)
            if info is not None:
                if info.get('merged_at'):
                    skipped.append({**pr, 'reason': 'merged upstream, so it is already in master'})
                    notices.append(f'#{number} ({pr["title"]}) was merged upstream. Remove it from prs.json.')
                    continue
                if info.get('state') == 'closed':
                    skipped.append({**pr, 'reason': 'closed upstream without being merged'})
                    notices.append(f'#{number} ({pr["title"]}) was closed without being merged. Remove it from prs.json.')
                    continue
                head = info.get('head', {}).get('sha')
                if head and head != pr['sha']:
                    compare = f'https://github.com/{CONFIG["upstream_repo"]}/compare/{pr["sha"]}...{head}'
                    notices.append(
                        f'#{number} ({pr["title"]}) has new commits. Still using the pinned {pr["sha"][:8]}. '
                        f'Review {compare} and update its sha in prs.json to take them.'
                    )
            if subprocess.run(['git', 'cat-file', '-e', f'{pr["sha"]}^{{commit}}'], cwd=worktree).returncode != 0:
                skipped.append({**pr, 'reason': 'pinned commit no longer exists on GitHub'})
                notices.append(f'#{number}: pinned commit {pr["sha"][:8]} is gone (force-pushed away). Pin a new one.')
                continue
            if reason := merge_pr(worktree, pr):
                skipped.append({**pr, 'reason': reason})
                notices.append(f'#{number} ({pr["title"]}) was left out: {reason}.')
            else:
                included.append(pr)

        included_numbers = {pr['number'] for pr in included}
        applied_patches = []
        for patch in CONFIG['patches']:
            # "requires": null means the patch is always applied
            if patch['requires'] is not None and patch['requires'] not in included_numbers:
                continue
            result = subprocess.run(
                ['git', 'apply', '--3way', str(HERE / patch['file'])], cwd=worktree, capture_output=True, text=True
            )
            if result.returncode != 0:
                git('checkout', '--', '.', cwd=worktree, check=False)
                notices.append(
                    f'Patch {patch["file"]} no longer applies, so it was left out. Update it.\n{result.stderr.strip()}'
                )
                continue
            git('add', '-A', cwd=worktree)
            git('commit', '--quiet', '-m', f'Fix: {patch["title"]}', cwd=worktree)
            applied_patches.append(patch)

        copy_tooling(REPO, worktree)
        for path in CONFIG.get('remove_files', []):
            (worktree / path).unlink(missing_ok=True)
        manifest = {
            'built_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
            'upstream': upstream_sha,
            'included': [{'number': p['number'], 'sha': p['sha'], 'title': p['title']} for p in included],
            'skipped': [{'number': p['number'], 'title': p['title'], 'reason': p['reason']} for p in skipped],
            'patches': [p['file'] for p in applied_patches],
        }
        (worktree / '.experimental' / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')
        git('add', '-A', cwd=worktree)
        git('commit', '--quiet', '-m', 'Add experimental branch tooling and build manifest', cwd=worktree)

        tests_ok, test_output = (True, 'skipped (--no-test)') if args.no_test else run_tests(worktree)
        if not tests_ok:
            notices.append('Tests failed, so the branch was not updated.')

        report = [
            f'# experimental build, {manifest["built_at"]}',
            f'Upstream master: `{upstream_sha[:10]}`',
            '## Included',
            *[f'- #{p["number"]} {p["title"]} (`{p["sha"][:8]}`)' for p in included],
            '## Left out' if skipped else '',
            *[f'- #{p["number"]} {p["title"]}: {p["reason"]}' for p in skipped],
            '## Fix patches',
            *([f'- {p["file"]}' for p in applied_patches] or ['- none']),
            '## Needs attention' if notices else '## Needs attention\n- nothing',
            *[f'- {n}' for n in notices],
            '## Tests',
            f'```\n{test_output}\n```',
        ]
        report_text = '\n'.join(line for line in report if line != '') + '\n'
        report_path = args.report or HERE / 'report.md'
        report_path.write_text(report_text)
        print(report_text)
        print(f'(report written to {report_path})')
        attention = Path(str(report_path) + '.attention')
        if notices:
            attention.write_text('\n'.join(notices))  # tells CI to open an issue, without failing the run
        else:
            attention.unlink(missing_ok=True)

        if not tests_ok:
            return 1

        built = git('rev-parse', 'HEAD', cwd=worktree)
        # the result, for inspecting locally (the branch you work on isn't moved)
        git('branch', '-f', f'{branch}-build', built, cwd=REPO)
        print(f'built {branch}-build -> {built[:10]}')
        if args.push:
            git('fetch', '--quiet', 'origin', cwd=REPO, check=False)
            remote = git('rev-parse', '--verify', '--quiet', f'origin/{branch}', cwd=REPO, check=False)
            if remote:
                # only push when the code changed, not just the build timestamp
                code_changed = bool(
                    git('diff', '--name-only', remote, built, '--', '.', ':!.experimental/build.json', cwd=REPO)
                )
                old_manifest = json.loads(git('show', f'{remote}:.experimental/build.json', cwd=REPO, check=False) or '{}')
                changed = code_changed or old_manifest.get('included') != manifest['included']
            else:
                changed = True
            if changed:
                lease = f'--force-with-lease={branch}:{remote}' if remote else f'--force-with-lease={branch}:'
                git('push', '--quiet', lease, 'origin', f'{built}:refs/heads/{branch}', cwd=REPO)
                print(f'pushed {branch} -> {built[:10]}')
            else:
                print(f'{branch} is already up to date')
            # keep the fork's master an exact copy of upstream (fast-forward only)
            sync = subprocess.run(
                ['git', 'push', '--quiet', 'origin', f'{upstream_sha}:refs/heads/master'],
                cwd=REPO,
                capture_output=True,
                text=True,
            )
            if sync.returncode != 0:
                notices.append(f"Couldn't fast-forward the fork's master to upstream: {sync.stderr.strip()}")
                print(notices[-1], file=sys.stderr)
                attention.write_text('\n'.join(notices))
        return 0
    finally:
        git('worktree', 'remove', '--force', str(worktree), cwd=REPO, check=False)


if __name__ == '__main__':
    sys.exit(main())
