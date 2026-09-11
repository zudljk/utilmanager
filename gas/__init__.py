import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import closing
from functools import wraps
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

import click
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.exceptions import BadRequest, HTTPException

from .db import audit, connection, initialize
from .domain import (ValidationError, formatted, ledger, month_key, number,
                     previous_month, quantity, validate_period)
from .importer import apply_import, read_ods
from .ocr import normalize_image, recognize
from .photos import photo_lock
from .mount import BasePathMiddleware, normalize_base_path


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATA_DIR=os.environ.get('DATA_DIR', 'data'),
        APP_BASE_PATH=os.environ.get('APP_BASE_PATH', ''),
        APP_USER=os.environ.get('APP_USER', 'admin'),
        APP_PASSWORD=os.environ.get('APP_PASSWORD', ''),
        UPLOAD_TOKEN=os.environ.get('UPLOAD_TOKEN', ''),
        SECRET_KEY=os.environ.get('SECRET_KEY', ''),
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=128 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', 'false').lower() == 'true',
    )
    if test_config:
        app.config.update(test_config)
    base_path = normalize_base_path(app.config['APP_BASE_PATH'])
    app.config['APP_BASE_PATH'] = base_path
    app.config['APPLICATION_ROOT'] = base_path or '/'
    if base_path:
        app.wsgi_app = BasePathMiddleware(app.wsgi_app, base_path)
    for key in ('APP_PASSWORD', 'UPLOAD_TOKEN', 'SECRET_KEY'):
        if len(app.config[key]) < 16:
            raise RuntimeError(f'{key} muss gesetzt sein und mindestens 16 Zeichen enthalten.')
    data_dir = Path(app.config['DATA_DIR']).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    photo_dir = data_dir / 'photos'
    photo_dir.mkdir(exist_ok=True)
    db_path = data_dir / 'gas.sqlite3'
    initialize(db_path)
    app.config['DATABASE'] = str(db_path)

    def today():
        return app.config.get('TODAY') or datetime.now(ZoneInfo('Europe/Berlin')).date()

    def db_context():
        return connection(db_path)

    def photo_operation(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with photo_lock(data_dir):
                return function(*args, **kwargs)
        return wrapped

    def remove_photo(filename):
        try:
            (photo_dir / filename).unlink(missing_ok=True)
            return True
        except OSError:
            app.logger.exception('Abgeschlossenes Foto konnte nicht gelöscht werden: %s', filename)
            return False

    # Also remove photos retained by older versions or left by an interrupted
    # cleanup. Pending photos are never removed by this operation.
    with photo_lock(data_dir), db_context() as db:
        for row in db.execute("SELECT filename FROM uploads WHERE status != 'pending'"):
            remove_photo(row['filename'])

    def configured(db):
        result = db.execute('SELECT * FROM settings WHERE id=1').fetchone()
        if not result:
            raise ValidationError('Bitte zuerst die ODS-Datei importieren oder den Tank einrichten.')
        return result

    def csrf_token():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return session['csrf']

    def equal(left, right):
        return hmac.compare_digest(str(left or '').encode(), str(right or '').encode())

    @app.before_request
    def authenticate():
        g.upload_stage = 'Anmeldung und Anfrageprüfung'
        if request.path == '/healthz' and request.method == 'GET':
            return None
        if request.path.startswith('/api/'):
            expected = 'Bearer ' + app.config['UPLOAD_TOKEN']
            if not equal(request.headers.get('Authorization'), expected):
                g.upload_error = 'Gültiger Bearer-Token erforderlich.'
                return jsonify(error='Gültiger Bearer-Token erforderlich.'), 401
        else:
            auth = request.authorization
            if not auth or auth.type != 'basic' or not equal(auth.username, app.config['APP_USER']) or not equal(auth.password, app.config['APP_PASSWORD']):
                g.upload_error = 'Anmeldung erforderlich.'
                return Response('Anmeldung erforderlich.', 401,
                                {'WWW-Authenticate': 'Basic realm="Gasverbrauch", charset="UTF-8"'})
            if request.method == 'POST' and not (session.get('csrf') and equal(request.form.get('csrf'), session['csrf'])):
                abort(400, 'Das Formular ist abgelaufen. Bitte neu laden.')

    @app.after_request
    def headers(response):
        if request.method == 'POST' and request.path in ('/api/uploads', '/uploads/new') and response.status_code >= 400:
            record_upload_failure(response)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    def diagnostic_text(value, limit=1000):
        # Only bounded diagnostic metadata, never request bodies or credentials.
        value = str(value)
        for key in ('UPLOAD_TOKEN', 'APP_PASSWORD', 'SECRET_KEY'):
            value = value.replace(app.config[key], '[entfernt]')
        return ''.join(char if char.isprintable() else ' ' for char in value)[:limit]

    def record_upload_failure(response):
        reason = diagnostic_text(getattr(g, 'upload_error', 'Interner Serverfehler. Details stehen im Serverlog.'))
        stage = getattr(g, 'upload_stage', 'Anfrageprüfung')
        source = 'iPhone / API' if request.path == '/api/uploads' else 'Webformular'
        # Do not access request.form/files here: parsing may already have failed,
        # or the request may have been rejected before reading its body (401/413).
        fields = getattr(g, 'upload_fields', None)
        failure_id = None
        try:
            with db_context() as db:
                inserted_id = db.execute(
                    'INSERT INTO upload_failures(source,status_code,reason,stage,content_type,content_length,fields_json) VALUES(?,?,?,?,?,?,?)',
                    (source, response.status_code, reason, stage, diagnostic_text(request.mimetype, 128),
                     request.content_length, json.dumps(fields, ensure_ascii=False))).lastrowid
                db.execute('DELETE FROM upload_failures WHERE id NOT IN (SELECT id FROM upload_failures ORDER BY id DESC LIMIT 100)')
            failure_id = inserted_id
        except (sqlite3.Error, OSError):
            # A full/unavailable database must not replace the original error.
            app.logger.exception('Upload-Fehlerprotokoll konnte nicht gespeichert werden.')
        app.logger.warning('Foto-Upload fehlgeschlagen: Versuch=%s Quelle=%s HTTP=%s Schritt=%s Grund=%s',
                           failure_id or '-', source, response.status_code, stage, reason)
        if failure_id is not None:
            response.headers['X-Upload-Failure-ID'] = str(failure_id)

    @app.errorhandler(ValidationError)
    def validation_error(exc):
        g.upload_error = str(exc)
        if request.path.startswith('/api/'):
            return jsonify(error=str(exc)), 400
        return render_template('error.html', message=str(exc)), 400

    @app.errorhandler(HTTPException)
    def http_error(exc):
        message = exc.description
        if exc.code == 413:
            message = 'Anfrage zu groß. Maximal 12 MiB insgesamt und 128 KiB für Text-Formularfelder.'
        elif exc.code == 400 and exc.description == BadRequest.description:
            message = 'Die Anfrage ist unvollständig oder ungültig. Bitte das Formularformat prüfen.'
        elif exc.code == 500:
            message = 'Interner Serverfehler. Details stehen im Serverlog.'
        g.upload_error = message
        if request.path.startswith('/api/'):
            return jsonify(error=message), exc.code
        return render_template('error.html', message=message), exc.code

    @app.errorhandler(sqlite3.IntegrityError)
    def conflict(exc):
        message = 'Der Datensatz existiert bereits oder wurde gleichzeitig geändert. Bitte die Übersicht neu laden.'
        g.upload_error = message
        if request.path.startswith('/api/'):
            return jsonify(error=message), 409
        return render_template('error.html', message=message), 409

    app.jinja_env.filters['num'] = formatted
    app.jinja_env.filters['quantity'] = quantity
    app.jinja_env.filters['month_de'] = lambda value: value[5:7] + '/' + value[:4]
    app.context_processor(lambda: dict(csrf_token=csrf_token, current_month=today().strftime('%Y-%m'),
                                      previous_month=previous_month(today())))

    @app.get('/healthz')
    def health():
        with db_context() as db:
            db.execute('SELECT 1').fetchone()
        return jsonify(status='ok')

    @app.get('/')
    def index():
        with db_context() as db:
            settings = db.execute('SELECT * FROM settings WHERE id=1').fetchone()
            if not settings:
                return render_template('setup.html')
            first_year = int(settings['start_month'][:4])
            latest = db.execute('SELECT MAX(month) FROM consumption').fetchone()[0]
            max_year = max(today().year + 1, int(latest[:4]) if latest else today().year)
            try:
                year = int(request.args.get('year', today().year))
            except ValueError:
                raise ValidationError('Ungültiges Jahr.')
            if not first_year <= year <= max_year:
                raise ValidationError('Das Jahr liegt außerhalb des erfassten Zeitraums.')
            all_rows = ledger(db, f'{max_year}-12')
            rows = [r for r in all_rows if r['month'].startswith(str(year))]
            last_complete = next((r for r in reversed(all_rows) if r['rest'] is not None), None)
            pending = db.execute("SELECT COUNT(*) FROM uploads WHERE status='pending'").fetchone()[0]
            return render_template('index.html', rows=rows, settings=settings, year=year,
                                   years=range(first_year, max_year+1), last_complete=last_complete, pending=pending)

    @app.post('/setup')
    def setup():
        s = dict(capacity_liters=str(number(request.form.get('capacity_liters'), 'Tankvolumen', positive=True)),
                 factor=str(number(request.form.get('factor'), 'Umrechnungsfaktor', positive=True)),
                 opening_liters=str(number(request.form.get('opening_liters'), 'Anfangsbestand')),
                 start_month=month_key(request.form.get('start_month')))
        if number(s['opening_liters']) > number(s['capacity_liters']):
            raise ValidationError('Anfangsbestand darf das Tankvolumen nicht überschreiten.')
        if s['start_month'] > today().strftime('%Y-%m'):
            raise ValidationError('Der Anfangsbestand darf nicht in der Zukunft liegen.')
        with db_context() as db:
            db.execute('INSERT INTO settings VALUES(1,?,?,?,?)', tuple(s.values()))
            audit(db, 'create', 'settings', 1, after=s)
        flash('Tank eingerichtet.')
        return redirect(url_for('index'))

    @app.post('/import')
    def import_file():
        source = request.files.get('file')
        if not source:
            raise ValidationError('Bitte eine ODS-Datei auswählen.')
        parsed = read_ods(source.read())
        with db_context() as db:
            changed = apply_import(db, parsed, Path(source.filename or 'Gasverbrauch.ods').name)
        flash(f"{len(parsed['consumption'])} Verbrauchswerte und {len(parsed['deliveries'])} Lieferungen importiert."
              if changed else 'Diese Datei wurde bereits importiert; es wurden keine Daten doppelt angelegt.')
        return redirect(url_for('index'))

    @app.route('/consumption/new', methods=['GET', 'POST'])
    @app.route('/consumption/<month>/edit', methods=['GET', 'POST'])
    def consumption(month=None):
        with db_context() as db:
            settings = configured(db)
            old = db.execute('SELECT * FROM consumption WHERE month=?', (month,)).fetchone() if month else None
            if month and not old:
                abort(404)
            if request.method == 'GET':
                return render_template('consumption.html', reading=old,
                                       month=month or request.args.get('month', previous_month(today())))
            target = validate_period(month or request.form.get('month'), settings, today())
            value = str(number(request.form.get('kwh'), 'Verbrauch'))
            note = request.form.get('note', '').strip()[:2000]
            if old:
                changed = db.execute('UPDATE consumption SET kwh=?,note=?,revision=revision+1 WHERE month=? AND revision=?',
                                     (value, note, target, request.form.get('revision'))).rowcount
                if not changed:
                    abort(409, 'Der Wert wurde inzwischen geändert. Bitte neu laden.')
            else:
                db.execute('INSERT INTO consumption(month,kwh,note) VALUES(?,?,?)', (target, value, note))
            audit(db, 'update' if old else 'create', 'consumption', target, old, dict(kwh=value, note=note))
        flash('Verbrauch gespeichert. Bestand und Projektion wurden neu berechnet.')
        return redirect(url_for('index', year=target[:4]))

    @app.route('/deliveries/new', methods=['GET', 'POST'])
    @app.route('/deliveries/<int:delivery_id>/edit', methods=['GET', 'POST'])
    def delivery(delivery_id=None):
        with db_context() as db:
            settings = configured(db)
            old = db.execute('SELECT * FROM deliveries WHERE id=?', (delivery_id,)).fetchone() if delivery_id else None
            if delivery_id and not old:
                abort(404)
            if request.method == 'GET':
                return render_template('delivery.html', delivery=old, submission_id=secrets.token_urlsafe(24),
                                       default_month=today().strftime('%Y-%m'))
            month = validate_period(request.form.get('month'), settings, today(), completed=False)
            raw_date = request.form.get('delivery_date', '').strip()
            if raw_date:
                try:
                    parsed_date = date.fromisoformat(raw_date)
                except ValueError as exc:
                    raise ValidationError('Ungültiges Lieferdatum.') from exc
                if parsed_date.strftime('%Y-%m') != month or parsed_date > today():
                    raise ValidationError('Lieferdatum muss im angegebenen Monat liegen und darf nicht zukünftig sein.')
            liters = number(request.form.get('liters'), 'Liefermenge', positive=True)
            if liters > number(settings['capacity_liters']):
                raise ValidationError('Die Liefermenge übersteigt das Tankvolumen.')
            price = number(request.form.get('unit_price'), 'Literpreis', optional=True)
            new = dict(month=month, delivery_date=raw_date or None, liters=str(liters),
                       unit_price=str(price) if price is not None else None,
                       note=request.form.get('note', '').strip()[:2000])
            if old:
                changed = db.execute('UPDATE deliveries SET month=?,delivery_date=?,liters=?,unit_price=?,note=?,revision=revision+1 WHERE id=? AND revision=?',
                                     (*new.values(), delivery_id, request.form.get('revision'))).rowcount
                if not changed:
                    abort(409, 'Die Lieferung wurde inzwischen geändert. Bitte neu laden.')
            else:
                token = request.form.get('submission_id', '')
                if not 16 <= len(token) <= 100:
                    raise ValidationError('Bitte das Lieferformular neu öffnen.')
                existing = db.execute('SELECT * FROM deliveries WHERE submission_id=?', (token,)).fetchone()
                if existing:
                    if any(existing[key] != value for key, value in new.items()):
                        abort(409, 'Dieses Formular wurde bereits mit anderen Werten gespeichert. Bitte eine neue Lieferung öffnen.')
                    return redirect(url_for('index', year=existing['month'][:4]))
                delivery_id = db.execute('INSERT INTO deliveries(month,delivery_date,liters,unit_price,note,submission_id) VALUES(?,?,?,?,?,?)',
                                         (*new.values(), token)).lastrowid
            audit(db, 'update' if old else 'create', 'delivery', delivery_id, old, new)
        flash('Lieferung gespeichert.')
        return redirect(url_for('index', year=month[:4]))

    @app.post('/delete/<kind>/<identifier>')
    @photo_operation
    def delete(kind, identifier):
        if request.form.get('confirm') != 'yes':
            raise ValidationError('Bitte das Löschen ausdrücklich bestätigen.')
        table, column = ('consumption', 'month') if kind == 'consumption' else ('deliveries', 'id') if kind == 'delivery' else (None, None)
        if table is None:
            abort(404)
        with db_context() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute(f'SELECT * FROM {table} WHERE {column}=?', (identifier,)).fetchone()
            if not old:
                abort(404)
            if str(old['revision']) != request.form.get('revision'):
                abort(409, 'Der Datensatz wurde inzwischen geändert. Bitte neu laden.')
            db.execute(f'DELETE FROM {table} WHERE {column}=?', (identifier,))
            if kind == 'consumption' and old['upload_id']:
                # The original photo has already been deleted after confirmation.
                # Deleting the reading must not create an unreviewable draft.
                db.execute("UPDATE uploads SET status='rejected' WHERE id=?", (old['upload_id'],))
            audit(db, 'delete', kind, identifier, before=old)
        flash('Eintrag gelöscht. Die Änderung bleibt im Änderungsprotokoll erhalten.')
        return redirect(url_for('index', year=old['month'][:4]))

    def upload_payload(row):
        return dict(id=row['id'], month=row['month'], status=row['status'],
                    photo_available=row['status'] == 'pending' and (photo_dir / row['filename']).is_file(),
                    candidates_kwh=json.loads(row['candidates']), warning=row['warning'],
                    review_url=url_for('review', upload_id=row['id']))

    @app.post('/api/uploads')
    def upload():
        g.upload_stage = 'Formular lesen'
        form, files = request.form, request.files
        g.upload_fields = {
            'text': [diagnostic_text(name, 80) for name in list(form)[:20]],
            'files': [diagnostic_text(name, 80) for name in list(files)[:20]],
        }
        g.upload_stage = 'Verbrauchsmonat und Tank prüfen'
        with db_context() as db:
            month = validate_period(form.get('month') or previous_month(today()), configured(db), today())
        g.upload_stage = 'Fotodatei lesen'
        source = files.get('image')
        if not source:
            raise ValidationError('Das Multipart-Dateifeld image mit einem Foto fehlt. In Kurzbefehle: Anfragetext „Formular“, Feldtyp „Datei“, Schlüssel „image“ und die Foto-Variable als Wert auswählen.')
        raw = source.read()
        digest = hashlib.sha256(raw).hexdigest()
        g.upload_stage = 'Doppelte Uploads prüfen'
        key = request.headers.get('Idempotency-Key') or None
        if key and (len(key) > 128 or not key.isascii()):
            raise ValidationError('Idempotency-Key muss aus höchstens 128 ASCII-Zeichen bestehen.')
        with db_context() as db:
            existing = db.execute('SELECT * FROM uploads WHERE sha256=? OR request_key=?', (digest, key)).fetchone()
            if existing:
                if existing['sha256'] != digest or existing['month'] != month:
                    abort(409, 'Foto oder Idempotency-Key wurde bereits für einen anderen Upload verwendet.')
                return jsonify(upload_payload(existing)), 200
        g.upload_stage = 'Bildformat prüfen'
        jpeg = normalize_image(raw)
        upload_id = uuid.uuid4().hex
        filename = upload_id + '.jpg'
        path = photo_dir / filename
        try:
            g.upload_stage = 'Bilddatei speichern'
            path.write_bytes(jpeg)
            g.upload_stage = 'Texterkennung'
            text, candidates, warning = recognize(path)
            g.upload_stage = 'Fotoentwurf speichern'
            with db_context() as db:
                db.execute('INSERT INTO uploads(id,sha256,request_key,month,filename,ocr_text,candidates,warning) VALUES(?,?,?,?,?,?,?,?)',
                           (upload_id, digest, key, month, filename, text, json.dumps(candidates), warning))
                row = db.execute('SELECT * FROM uploads WHERE id=?', (upload_id,)).fetchone()
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return jsonify(upload_payload(row)), 201

    @app.get('/api/uploads/<upload_id>')
    def upload_status(upload_id):
        with db_context() as db:
            row = db.execute('SELECT * FROM uploads WHERE id=?', (upload_id,)).fetchone()
            if not row:
                abort(404)
            return jsonify(upload_payload(row))

    @app.get('/uploads')
    def uploads():
        with db_context() as db:
            rows = db.execute("SELECT * FROM uploads ORDER BY status='pending' DESC,created_at DESC").fetchall()
            failures = [dict(row) for row in db.execute('SELECT * FROM upload_failures ORDER BY id DESC LIMIT 100')]
            for failure in failures:
                failure['fields'] = json.loads(failure['fields_json'])
            return render_template('uploads.html', uploads=rows, failures=failures)

    @app.get('/help/iphone-shortcut')
    def iphone_shortcut():
        return render_template('iphone_shortcut.html')

    @app.route('/uploads/new', methods=['GET', 'POST'])
    def web_upload():
        if request.method == 'GET':
            with db_context() as db:
                configured(db)
            return render_template('upload.html')
        response, status = upload()
        return redirect(url_for('review', upload_id=response.get_json()['id']))

    @app.route('/uploads/<upload_id>', methods=['GET', 'POST'])
    @photo_operation
    def review(upload_id):
        with db_context() as db:
            row = db.execute('SELECT * FROM uploads WHERE id=?', (upload_id,)).fetchone()
            if not row:
                abort(404)
            if request.method == 'GET':
                values = json.loads(row['candidates'])
                return render_template('review.html', upload=row, candidates=values,
                                       photo_available=row['status'] == 'pending' and (photo_dir / row['filename']).is_file(),
                                       candidate=values[0] if len(values) == 1 else '')
            if row['status'] != 'pending':
                abort(409, 'Dieses Foto wurde bereits bearbeitet.')
            action = request.form.get('action')
            if action not in ('confirm', 'reject'):
                raise ValidationError('Ungültige Aktion.')
            if action == 'confirm':
                month = validate_period(request.form.get('month'), configured(db), today())
                kwh = str(number(request.form.get('kwh'), 'Verbrauch'))
                db.execute('INSERT INTO consumption(month,kwh,upload_id) VALUES(?,?,?)', (month, kwh, upload_id))
                audit(db, 'create', 'consumption', month, after=dict(kwh=kwh, upload_id=upload_id))
            else:
                month = row['month']
            changed = db.execute('UPDATE uploads SET status=?,month=? WHERE id=? AND status=\'pending\'',
                                 ('confirmed' if action == 'confirm' else 'rejected', month, upload_id)).rowcount
            if not changed:
                abort(409, 'Dieses Foto wurde inzwischen bearbeitet.')
            audit(db, action, 'upload', upload_id, before=row)
        # Commit the booking first. Any validation or database failure above
        # preserves the photo. Backups hold the same cross-process lock.
        removed = remove_photo(row['filename'])
        flash('Verbrauch bestätigt und gebucht.' if action == 'confirm' else 'Foto verworfen; kein Verbrauch gebucht.')
        if not removed:
            flash('Das Foto konnte noch nicht gelöscht werden. Die Bereinigung wird beim nächsten App-Start erneut versucht.')
        return redirect(url_for('uploads'))

    @app.get('/photos/<upload_id>')
    def photo(upload_id):
        with db_context() as db:
            row = db.execute('SELECT filename,status FROM uploads WHERE id=?', (upload_id,)).fetchone()
            if not row:
                abort(404)
            if row['status'] != 'pending':
                abort(410, 'Das Foto wurde nach Abschluss der Prüfung gelöscht.')
            return send_from_directory(photo_dir, row['filename'], mimetype='image/jpeg')

    @app.get('/history')
    def history():
        with db_context() as db:
            rows = db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 200').fetchall()
            return render_template('history.html', entries=rows)

    @app.cli.command('import-ods')
    @click.argument('filename', type=click.Path(exists=True, dir_okay=False))
    @click.option('--apply', 'do_apply', is_flag=True, help='Import tatsächlich in die leere Datenbank schreiben.')
    def import_command(filename, do_apply):
        try:
            parsed = read_ods(Path(filename).read_bytes())
            click.echo(f"{len(parsed['consumption'])} Verbrauchswerte, {len(parsed['deliveries'])} Lieferungen; Beginn {parsed['settings']['start_month']}")
            if do_apply:
                with db_context() as db:
                    changed = apply_import(db, parsed, Path(filename).name)
                click.echo('Import abgeschlossen.' if changed else 'Bereits importiert; keine Änderung.')
            else:
                click.echo('Nur geprüft. Zum Importieren --apply ergänzen.')
        except ValidationError as exc:
            raise click.ClickException(str(exc)) from exc

    @app.cli.command('backup')
    @click.argument('destination', type=click.Path())
    @photo_operation
    def backup_command(destination):
        """Create a consistent SQLite snapshot with photos still awaiting review."""
        import shutil
        target = Path(destination).resolve()
        if target == data_dir or data_dir in target.parents:
            raise click.ClickException('Backup bitte außerhalb von DATA_DIR ablegen.')
        target.mkdir(parents=True, exist_ok=False)
        with db_context() as source, closing(sqlite3.connect(target / 'gas.sqlite3')) as dest:
            source.backup(dest)
            names = [r[0] for r in dest.execute("SELECT filename FROM uploads WHERE status='pending'")]
        (target / 'photos').mkdir()
        for name in names:
            shutil.copy2(photo_dir / name, target / 'photos' / name)
        click.echo(f'Backup abgeschlossen: {target}')

    return app
