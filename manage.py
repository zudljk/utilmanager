"""Local entry point: load explicit configuration; use Gunicorn to serve."""
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

if sys.argv[1:] == ['init-env']:
    target = ROOT / '.env'
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit('.env existiert bereits und wurde nicht geändert.')
    with os.fdopen(descriptor, 'w') as file:
        file.write('APP_USER=admin\n')
        for key in ('APP_PASSWORD', 'UPLOAD_TOKEN', 'SECRET_KEY'):
            file.write(f'{key}={secrets.token_urlsafe(32)}\n')
        file.write('APP_PORT=8080\nCOOKIE_SECURE=false\n')
    print('.env mit individuellen Zugangsdaten angelegt (nur für den Besitzer lesbar).')
    raise SystemExit(0)

# This intentionally reads simple KEY=value entries, never executes shell code.
if (ROOT / '.env').exists():
    for line in (ROOT / '.env').read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if sep and key.strip() in {'APP_USER', 'APP_PASSWORD', 'UPLOAD_TOKEN', 'SECRET_KEY',
                                  'APP_PORT', 'COOKIE_SECURE', 'DATA_DIR'}:
            os.environ.setdefault(key.strip(), value.strip())

if sys.argv[1:] == ['serve']:
    os.execv(sys.executable, [sys.executable, '-m', 'gunicorn', '--bind',
                             '127.0.0.1:' + os.environ.get('APP_PORT', '8080'),
                             '--workers', '1', '--threads', '2', '--timeout', '60',
                             '--no-control-socket', '--access-logfile', '-', 'gas:create_app()'])

from flask.cli import FlaskGroup
from gas import create_app

FlaskGroup(create_app=create_app, load_dotenv=False)()
