#!/usr/bin/env python3
"""Check private paths in the index/history, or export reviewed source without history.

No file contents or secret values are printed. This is a publication boundary,
not a complete detector of personal information or unknown credential formats.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def private_path(name):
    p = Path(name)
    parts = {part.lower() for part in p.parts}
    base = p.name.lower()
    if parts & {'.git', '.starter', '.aws', '.codex', '.agents', 'node_modules', '__pycache__',
                '.pytest_cache', 'data', 'certs', 'cache', 'reports', 'private', 'local',
                'public-release', 'dist', 'build', 'test-results', 'playwright-report'}:
        return True
    if any(p.parts[i:i+2] == ('docs', 'schwab') for i in range(len(p.parts)-1)):
        return True
    if base.startswith('news_llm_real_regressions_') and p.suffix == '.json':
        return True
    if base == 'config.yaml' or base == 'live_schwab_validation_checklist.md':
        return True
    if p.suffix.lower() in {'.md', '.txt', '.json', '.yaml', '.yml'} and any(word in base for word in ('progress', 'goals', 'plan', 'notes')):
        return True
    if base != '.env.example' and (base.startswith('.env') or base.endswith('.env')):
        return True
    if base.startswith(('id_rsa', 'id_ed25519', 'news-llm-')) and p.suffix != '.py':
        return True
    return p.suffix.lower() in {'.key', '.pem', '.crt', '.pfx', '.p12', '.db', '.sqlite', '.sqlite3',
                               '.dump', '.backup', '.log', '.har', '.pt', '.pth', '.safetensors', '.tmp'}


def local_secrets():
    values = []
    for p in (ROOT / 'backend/.env', ROOT / '.starter/backend.env'):
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            name, value = line.split('=', 1)
            value = value.strip().strip('\"\'')
            if re.search(r'SECRET|CLIENT_ID|API_KEY|ACCOUNT_NUMBER|ENC_KEY|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD', name) and len(value) >= 6:
                if not value.startswith(('replace-', 'your-', 'http')) and value not in {'postgres', 'password', 'changeme', 'replace-me'}:
                    values.append(value.encode())
    return values


def sensitive(data, values):
    return (any(value in data for value in values) or
            re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', data) is not None or
            re.search(rb'\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|sk-proj-[A-Za-z0-9_-]{40,})', data) is not None)


def git_args():
    # Locate the repository without changing the user's global safe.directory config.
    repo = next((p for p in (ROOT, *ROOT.parents) if (p / '.git').exists()), None)
    if repo is None:
        raise RuntimeError('No Git repository found.')
    return ['git', '-c', f'safe.directory={repo}', '-C', str(repo)]


def git_read(args, *extra):
    return subprocess.check_output(args + list(extra), stderr=subprocess.DEVNULL)


def check_history(include_history):
    git = git_args()
    values = local_secrets()
    findings = set()
    index = git_read(git, 'ls-files', '--stage', '-z').split(b'\0')
    blobs = []
    for entry in filter(None, index):
        metadata, raw_path = entry.split(b'\t', 1)
        name = raw_path.decode('utf-8', 'replace')
        oid = metadata.split()[1].decode()
        blobs.append((oid, name))
        if private_path(name):
            findings.add('Private path is tracked: ' + name)
    if include_history:
        for commit in git_read(git, 'rev-list', '--all').decode().splitlines():
            for raw_name in git_read(git, 'ls-tree', '-r', '--name-only', '-z', commit).split(b'\0'):
                name = raw_name.decode('utf-8', 'replace')
                if name and private_path(name):
                    findings.add('Private path exists in history: ' + name)
        for line in git_read(git, 'rev-list', '--objects', '--all').decode().splitlines():
            oid, _, name = line.partition(' ')
            blobs.append((oid, name))
            if name and private_path(name):
                findings.add('Private path exists in history: ' + name)
    seen = set()
    for oid, name in blobs:
        if oid in seen:
            continue
        seen.add(oid)
        if sensitive(git_read(git, 'cat-file', '-p', oid), values):
            findings.add('Credential/key pattern detected (content hidden): ' + (name or '[Git object]'))
    for finding in sorted(findings):
        print('BLOCK ' + finding)
    if findings:
        print('Publication blocked. Preserve the working installation; use a reviewed clean export or clean publication history.')
        return 1
    print('PASS: no known private paths or checked credential patterns in the selected Git scope.')
    print('Still review source, images, personal data, and commit identity before publishing.')
    return 0


PUBLIC_TOP = {'.gitignore', 'README.md', 'TESTING.md', 'compose.starter.yaml', 'package.json', 'package-lock.json'}
PUBLIC_DIRS = {'backend', 'frontend', 'postgres', 'nginx', 'docs', 'scripts'}
PUBLIC_DOCS = {'docs/starter-setup.md', 'docs/screenshots.md', 'docs/publishing.md', 'docs/capture-screenshots.mjs', 'docs/system-guide.md', 'docs/schwab-integration.md'}
PUBLIC_SUFFIXES = {'.py', '.js', '.jsx', '.mjs', '.json', '.md', '.sql', '.css', '.html', '.svg', '.txt', '.conf', '.yaml', '.yml', '.sh'}


def candidates():
    for directory, dirs, names in os.walk(ROOT, followlinks=False):
        folder = Path(directory)
        dirs[:] = [name for name in dirs
                   if not (folder / name).is_symlink()
                   and not private_path(str((folder / name).relative_to(ROOT)))]
        for name in names:
            path = folder / name
            rel = path.relative_to(ROOT)
            if private_path(str(rel)) or path.is_symlink() or not path.is_file():
                continue
            if rel.parts[0] == 'docs':
                if str(rel) in PUBLIC_DOCS or (rel.parts[:2] == ('docs', 'images') and path.suffix == '.png'):
                    yield path, rel
                continue
            if str(rel) == 'backend/test/fixtures/news_llm_real_regressions_20261017.json':
                continue
            if len(rel.parts) == 1:
                if path.name in PUBLIC_TOP:
                    yield path, rel
            elif rel.parts[0] in PUBLIC_DIRS:
                if rel.parts[0] == 'docs' and re.search(r'\d{4}-\d{2}-\d{2}', path.name):
                    continue
                if path.suffix in PUBLIC_SUFFIXES or path.name.startswith('Dockerfile') or path.name in {'.dockerignore', '.gitignore', '.env.example'} or (rel.parts[:2] == ('docs', 'images') and path.suffix == '.png'):
                    yield path, rel


def export(destination):
    destination = destination.resolve()
    if destination == ROOT or ROOT in destination.parents or destination.exists():
        raise RuntimeError('Export destination must be new and outside the application directory.')
    values = local_secrets()
    # Freeze exactly the bytes checked below, even if local development continues.
    snapshots = {str(rel): path.read_bytes() for path, rel in candidates()}
    templates = {
        'README.md': 'README.md', 'TESTING.md': 'TESTING.md',
        'frontend-README.md': 'frontend/README.md',
        'schwab-integration.md': 'docs/schwab-integration.md',
        'system-guide.md': 'docs/system-guide.md',
        'StarterCsrf.test.js': 'frontend/src/__tests__/StarterCsrf.test.js',
    }
    for source, target in templates.items():
        template = ROOT / 'docs/public' / source
        if template.is_file() and not any(p.is_symlink() for p in (template, *template.parents)):
            snapshots[target] = template.read_bytes()
    if 'frontend/src/pages/NewsLlm.jsx' in snapshots:
        # This is an event handler, not a hook. Fix the public copy only.
        snapshots['frontend/src/pages/NewsLlm.jsx'] = snapshots['frontend/src/pages/NewsLlm.jsx'].replace(
            b'useForSentiment', b'loadArticleForSentiment')
    if 'frontend/src/lib/api.js' in snapshots:
        # Cookie names are host-scoped, not port-scoped: keep a parallel starter separate.
        snapshots['frontend/src/lib/api.js'] = snapshots['frontend/src/lib/api.js'].replace(
            b"readCookie('csrf')", b"readCookie(import.meta.env.VITE_CSRF_COOKIE_NAME || 'csrf')")
    if (ROOT / 'backend/test').is_dir():
        snapshots['backend/test/__init__.py'] = b'"""Local test package; avoid collision with Python standard-library test."""\n'
    for rel, data in snapshots.items():
        if sensitive(data, values):
            raise RuntimeError('Credential/key pattern in export candidate (content hidden): ' + rel)
    # Validate the published paths, not the private source-document locations.
    for rel, data in snapshots.items():
        if not rel.endswith('.md'):
            continue
        for target in re.findall(r'\]\(([^)]+)\)', data.decode()):
            if target.startswith(('http:', 'https:', '#', 'mailto:')):
                continue
            path = os.path.normpath(str(Path(rel).parent / target.split('#')[0]))
            if path not in snapshots:
                raise RuntimeError('Public document links outside the export: ' + rel + ' -> ' + target)
    destination.mkdir(parents=True, exist_ok=False)
    for rel, data in snapshots.items():
        output = destination / rel
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(snapshots.items())}
    (destination / 'PUBLICATION_MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Exported {len(snapshots)} source/documentation files to a new directory.')
    print('No Git history, original Compose configuration, local environment, notes, datasets, or certificates copied.')
    print('Nothing committed or pushed. Review the export before initializing its own public repository.')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true', help='Also inspect all reachable local Git history')
    parser.add_argument('--export', type=Path, help='Copy public candidates into a NEW directory outside this app')
    args = parser.parse_args()
    try:
        return export(args.export) if args.export else check_history(args.history)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        # Git command exceptions can include command arguments; only our own errors are displayed.
        print('BLOCK ' + (str(exc) if isinstance(exc, RuntimeError) else 'Publication check could not complete.'), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
