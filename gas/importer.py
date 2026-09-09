"""Read the supplied ODS directly; never execute spreadsheet formulas."""
import hashlib
import io
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal

from .db import audit
from .domain import ValidationError, month_key, number, shift_month

NS = {'table': 'urn:oasis:names:tc:opendocument:xmlns:table:1.0',
      'text': 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'}


def read_ods(data):
    if len(data) > 12 * 1024 * 1024:
        raise ValidationError('Die ODS-Datei ist zu groß.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo('content.xml')
            if info.file_size > 16 * 1024 * 1024:
                raise ValidationError('Die entpackte Tabelle ist zu groß.')
            xml = archive.read(info)
        if b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:
            raise ValidationError('XML-Dokumenttypdeklarationen werden nicht unterstützt.')
        root = ET.fromstring(xml)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValidationError('Keine lesbare ODS-Datei.') from exc
    sheet = root.find('.//table:table', NS)
    if sheet is None:
        raise ValidationError('Das erste Tabellenblatt fehlt.')
    rows = {}
    row_index = 1
    for row in sheet.findall('.//table:table-row', NS):
        cells = {}
        col = 1
        for cell in row:
            if not cell.tag.endswith('table-cell'):
                continue
            attrs = {k.split('}')[-1]: v for k, v in cell.attrib.items()}
            attrs['text'] = ' '.join(''.join(p.itertext()) for p in cell.findall('text:p', NS))
            repeat = int(attrs.get('number-columns-repeated', '1'))
            for cc in range(col, min(col + repeat, 13)):
                cells[cc] = attrs
            col += repeat
        repeat_rows = int(row.get('{'+NS['table']+'}number-rows-repeated', '1'))
        if repeat_rows != 1 and any(c.get('value') or c.get('date-value') for c in cells.values()):
            raise ValidationError('Wiederholte befüllte Zeilen werden nicht unterstützt.')
        rows[row_index] = cells
        row_index += repeat_rows
    headers = rows.get(4, {})
    if headers.get(1, {}).get('text') != 'Monat' or 'Lieferung' not in headers.get(2, {}).get('text', '') or 'Verbrauch' not in headers.get(4, {}).get('text', ''):
        raise ValidationError('Erwartet wird das Gasverbrauch-Blatt mit der Kopfzeile in Zeile 4.')
    get = lambda row, col: rows.get(row, {}).get(col, {}).get('value')
    settings = dict(capacity_liters=str(number(get(1, 2), 'Tankvolumen', positive=True)),
                    factor=str(number(get(1, 6), 'Umrechnungsfaktor', positive=True)),
                    opening_liters=str(number(get(5, 2), 'Anfangsbestand')))
    if Decimal(settings['opening_liters']) > Decimal(settings['capacity_liters']):
        raise ValidationError('Der Anfangsbestand übersteigt das Tankvolumen.')
    consumption, deliveries, comparisons, prices = [], [], [], []
    balance = Decimal(settings['opening_liters']) * Decimal(settings['factor'])
    seen = set()
    previous_price = None
    complete = True
    for rn, cells in rows.items():
        raw_date = cells.get(1, {}).get('date-value')
        if rn < 6 or raw_date is None:
            continue
        month = month_key(raw_date[:7])
        if month in seen or (seen and month != shift_month(last_month, 1)):
            raise ValidationError(f'Zeile {rn}: Doppelte oder nicht aufeinanderfolgende Monate.')
        seen.add(month)
        last_month = month
        if 'start_month' not in settings:
            settings['start_month'] = month
        raw_kwh = cells.get(4, {}).get('value')
        if cells.get(4, {}).get('formula'):
            raise ValidationError(f'D{rn}: Verbrauch enthält eine Formel; manuelle Prüfung erforderlich.')
        kwh = number(raw_kwh, 'Verbrauch', optional=True)
        liters = number(cells.get(2, {}).get('value'), 'Lieferung', positive=True, optional=True)
        if kwh is not None:
            consumption.append(dict(month=month, kwh=str(kwh), note=f'ODS-Import, Zeile {rn}'))
        else:
            complete = False
        if liters is not None:
            deliveries.append(dict(month=month, liters=str(liters), note=f'ODS-Import, Zeile {rn}; Lieferdatum nur monatsgenau'))
        # Retain early comparison values, never deduct them as actual consumption.
        prior = cells.get(8, {})
        if prior.get('value') is not None and (not prior.get('formula') or rn < 18):
            comparisons.append(dict(month=month, kwh=str(number(prior['value'])), source=f'ODS H{rn}'))
        price = number(cells.get(11, {}).get('value'), 'Bewertungspreis', optional=True)
        if price is not None and price != previous_price:
            prices.append(dict(month=month, unit_price=str(price)))
            previous_price = price
        balance += (liters or Decimal(0)) * Decimal(settings['factor']) - (kwh or Decimal(0))
        cached = cells.get(5, {}).get('value')
        if complete and cached is not None and abs(balance - Decimal(cached)) > Decimal('0.01'):
            raise ValidationError(f'E{rn}: Bestandsrechnung weicht von der Datei ab; Import abgebrochen.')
    if not consumption:
        raise ValidationError('Keine Verbrauchswerte gefunden.')
    return dict(settings=settings, consumption=consumption, deliveries=deliveries,
                comparisons=comparisons, prices=prices, sha256=hashlib.sha256(data).hexdigest())


def apply_import(db, parsed, filename):
    # Take the write lock before checking emptiness: concurrent imports cannot merge.
    db.execute('BEGIN IMMEDIATE')
    if db.execute('SELECT 1 FROM imports WHERE sha256=?', (parsed['sha256'],)).fetchone():
        return False
    if db.execute('SELECT 1 FROM settings').fetchone():
        raise ValidationError('Import nur in eine noch nicht eingerichtete Datenbank möglich.')
    s = parsed['settings']
    db.execute('INSERT INTO settings VALUES(1,?,?,?,?)',
               (s['capacity_liters'], s['factor'], s['opening_liters'], s['start_month']))
    db.executemany('INSERT INTO consumption(month,kwh,note) VALUES(:month,:kwh,:note)', parsed['consumption'])
    for index, d in enumerate(parsed['deliveries']):
        db.execute('INSERT INTO deliveries(month,liters,note,submission_id) VALUES(?,?,?,?)',
                   (d['month'], d['liters'], d['note'], f"ods:{parsed['sha256']}:{index}"))
    db.executemany('INSERT INTO historical_prices VALUES(:month,:unit_price)', parsed['prices'])
    db.executemany('INSERT INTO comparisons VALUES(:month,:kwh,:source)', parsed['comparisons'])
    db.execute('INSERT INTO imports(sha256,filename) VALUES(?,?)', (parsed['sha256'], filename))
    audit(db, 'import', 'ods', parsed['sha256'], after={
        'consumption': len(parsed['consumption']), 'deliveries': len(parsed['deliveries']), 'filename': filename})
    return True
