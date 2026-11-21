#!/usr/bin/env python3
"""Opt-in initialization and read-only diagnostics for the separate starter stack."""
import argparse
import base64
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(args):
    try:
        return subprocess.run(args, cwd=ROOT, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def initialize():
    if not shutil.which('openssl'):
        raise RuntimeError('OpenSSL is required before initialization.')
    target = ROOT / '.starter'
    # Exclusive directory creation: never open an existing installation for writing.
    target.mkdir(mode=0o700, exist_ok=False)
    certs = target / 'certs'
    certs.mkdir(mode=0o700)
    env = (ROOT / 'backend/.env.example').read_text()
    replacements = {
        'SCHWAB_REDIRECT_URI': 'https://127.0.0.1:18443/auth/callback',
        'FRONTEND_URL': 'https://127.0.0.1:13000/',
        'CORS_ALLOW_ORIGINS': 'https://127.0.0.1:13000',
        'REDIS_TOKEN_ENC_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        'SESSION_COOKIE_NAME': 'stockapp_starter_session',
        'CSRF_COOKIE_NAME': 'stockapp_starter_csrf',
        'FINNHUB_API_KEY': '', 'FINNHUB_NEWS_ENABLED': 'false', 'NEWSAPI_KEY': '',
        'AGENT_TRADING_KILL_SWITCH': 'true',
    }
    env = '\n'.join(line.split('=', 1)[0] + '=' + replacements[line.split('=', 1)[0]]
                    if '=' in line and line.split('=', 1)[0] in replacements else line
                    for line in env.splitlines()) + '\n'
    with (target / 'backend.env').open('x') as handle:
        os.chmod(handle.name, 0o600)
        handle.write(env)
    if not run(['openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:2048', '-days', '365',
                '-keyout', str(certs / 'server.key'), '-out', str(certs / 'server.crt'),
                '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost']):
        raise RuntimeError('Certificate generation failed. Partial .starter directory retained; no existing files replaced.')
    (certs / 'server.key').chmod(0o600)
    print('Created .starter only. Add your own app credentials to .starter/backend.env.')
    print('Register callback https://127.0.0.1:18443/auth/callback for the starter app.')


def check():
    failures = []
    def report(ok, label):
        print(('PASS ' if ok else 'FAIL ') + label)
        if not ok:
            failures.append(label)
    report(shutil.which('docker') is not None, 'Docker executable')
    report(run(['docker', 'compose', 'version']), 'Docker Compose available')
    report(run(['docker', 'info']), 'Docker daemon accessible')
    report(shutil.which('openssl') is not None, 'OpenSSL executable')
    path = ROOT / '.starter/backend.env'
    report(path.is_file() and not path.is_symlink(), 'Separate starter environment exists')
    env = {}
    if path.is_file() and not path.is_symlink():
        for line in path.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                env[key] = value.strip().strip('\"\'')
    for key in ('SCHWAB_CLIENT_ID', 'SCHWAB_CLIENT_SECRET'):
        value = env.get(key, '')
        report(bool(value) and value not in ('replace-me', 'your-own-app-key', 'your-own-app-secret'), key + ' configured (value hidden)')
    try:
        key_ok = len(base64.b64decode(env.get('REDIS_TOKEN_ENC_KEY', ''), altchars=b'-_', validate=True)) == 32
    except ValueError:
        key_ok = False
    report(key_ok, 'Token encryption key has the required format (value hidden)')
    for key, expected in {'SCHWAB_REDIRECT_URI': 'https://127.0.0.1:18443/auth/callback',
                          'FRONTEND_URL': 'https://127.0.0.1:13000/',
                          'CORS_ALLOW_ORIGINS': 'https://127.0.0.1:13000'}.items():
        report(env.get(key) == expected, key + ' matches starter origins')
    cert = ROOT / '.starter/certs/server.crt'
    key = ROOT / '.starter/certs/server.key'
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        pair_ok = True
    except (OSError, ssl.SSLError):
        pair_ok = False
    report(pair_ok, 'TLS certificate and key form a usable pair')
    report(run(['openssl', 'x509', '-in', str(cert), '-noout', '-checkend', '86400']), 'Certificate valid for at least another day')
    report(run(['openssl', 'x509', '-in', str(cert), '-noout', '-checkip', '127.0.0.1']), 'Certificate covers the callback IP')
    for port in (13000, 18443):
        with socket.socket() as sock:
            sock.settimeout(1)
            report(sock.connect_ex(('127.0.0.1', port)) != 0, f'Port {port} has no listener (pre-start check)')
    report(run(['docker', 'compose', '--env-file', '/dev/null', '-p', 'stockapp-starter', '-f', 'compose.starter.yaml', 'config', '--quiet']), 'Starter Compose configuration validates')
    print('No services started or configuration changed. No credential values printed.')
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--init', action='store_true', help='Create a NEW .starter directory; refuse if it already exists')
    args = parser.parse_args()
    if args.init:
        try:
            initialize()
        except FileExistsError:
            print('REFUSED: .starter already exists. Nothing was overwritten.', file=sys.stderr)
            return 1
        except (OSError, RuntimeError):
            print('Initialization failed; any partial starter files were retained. Existing environment untouched.', file=sys.stderr)
            return 1
        return 0
    return check()


if __name__ == '__main__':
    raise SystemExit(main())
