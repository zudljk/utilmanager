import csv
import io
import re
import subprocess
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from PIL import Image, ImageOps, UnidentifiedImageError

from .domain import ValidationError


def normalize_image(data):
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValidationError('Bitte das Foto als JPEG, PNG oder WebP senden. HEIC vorher in JPEG umwandeln.')
            if source.width * source.height > 25_000_000:
                raise ValidationError('Bitte das Foto auf höchstens 25 Megapixel verkleinern.')
            result = ImageOps.exif_transpose(source).convert('RGB')
            result.thumbnail((2400, 2400))
            output = io.BytesIO()
            result.save(output, format='JPEG', quality=92)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValidationError('Das Foto konnte nicht gelesen werden. Bitte JPEG oder PNG verwenden.') from exc


def candidates_from_text(text, *, require_unit=False):
    # Prefer values explicitly followed by kWh; never add multiple displayed numbers.
    matches = re.finditer(r'(?<![\d.,])([0-9]+(?:[.,][0-9]+)*)\s*k\s*w\s*h\b', text, re.I)
    # Never turn an unresolved "1 9 5 kWh" into a suggestion of just 5 kWh.
    tokens = [match[1] for match in matches
              if not re.search(r'[\d.,][ \t]+$', text[:match.start()])]
    if not tokens and not require_unit:
        tokens = re.findall(r'^\s*([0-9]+(?:[.,][0-9]+)*)\s*$', text, re.M)
    values = []
    for token in tokens:
        if ',' in token:
            token = token.replace('.', '').replace(',', '.')
        elif re.fullmatch(r'\d{1,3}(?:\.\d{3})+', token):
            token = token.replace('.', '')
        if not re.fullmatch(r'\d+(?:\.\d{1,6})?', token):
            continue
        value = Decimal(token)
        if value <= 1_000_000 and str(value) not in values:
            values.append(str(value))
    return values[:20]


def text_from_tsv(data):
    """Rejoin split digits only when their geometry describes one number."""
    lines = {}
    for row in csv.DictReader(io.StringIO(data), delimiter='\t', quoting=csv.QUOTE_NONE):
        if row.get('level') != '5' or not (row.get('text') or '').strip():
            continue
        key = tuple(row[name] for name in ('page_num', 'block_num', 'par_num', 'line_num'))
        word = dict(text=row['text'].strip(), **{name: int(row[name]) for name in ('left', 'top', 'width', 'height')})
        line = lines.setdefault(key, [])
        if line:
            last = line[-1]
            height = min(last['height'], word['height'])
            gap = word['left'] - last['left'] - last['width']
            same_baseline = abs(last['top'] + last['height'] - word['top'] - word['height']) <= height * .25
            if (last['text'].isdigit() and word['text'].isdigit()
                    and (len(last['text']) == 1 or len(word['text']) == 1)
                    and 0 <= gap <= height * .5 and same_baseline
                    and height >= max(last['height'], word['height']) * .7):
                last['text'] += word['text']
                last['width'] = word['left'] + word['width'] - last['left']
                continue
        line.append(word)
    return '\n'.join(' '.join(word['text'] for word in line) for line in lines.values())[:16000]


def recognize(path):
    deadline = monotonic() + 25
    readings = []
    interrupted = False

    def read(source, label):
        nonlocal interrupted
        remaining = deadline - monotonic()
        if remaining <= 0:
            interrupted = True
            return
        try:
            result = subprocess.run(
                ['tesseract', str(Path(source).resolve()), 'stdout', '-l', 'eng', '--psm', '6', 'tsv'],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=remaining, check=True)
            readings.append((label, text_from_tsv(result.stdout)))
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            interrupted = True

    try:
        read(path, 'Originalbild')
        with Image.open(path) as image:
            gray = ImageOps.grayscale(image)
        histogram = gray.histogram()
        dark_display = sum(histogram[:128]) > gray.width * gray.height / 2
        # Keep the original photo for review. Only temporary OCR copies isolate
        # the bright display text from blue bars, grid lines and dark background.
        if dark_display or not any(candidates_from_text(text, require_unit=True) for _, text in readings):
            with TemporaryDirectory(prefix='utilmanager-ocr-') as directory:
                for cutoff in (200, 210):
                    if monotonic() >= deadline:
                        interrupted = True
                        break
                    prepared = gray.point(lambda pixel: 0 if pixel > cutoff else 255)
                    target = Path(directory) / 'display.png'
                    prepared.save(target)
                    read(target, f'Helle Displayschrift (Schwelle {cutoff})')
    except FileNotFoundError:
        return '', [], 'Tesseract oder die Bilddatei ist nicht verfügbar. Das Foto kann manuell ausgewertet werden.'
    except OSError:
        interrupted = True

    values = []
    for _, text in readings:
        for value in candidates_from_text(text, require_unit=True):
            if value not in values:
                values.append(value)
    # Bare-number fallback belongs only to the original image: thresholding can
    # remove units, decimal separators and parts of dates, creating false values.
    if not values and readings and readings[0][0] == 'Originalbild':
        values = candidates_from_text(readings[0][1])
    text = '\n\n'.join(f'[{label}]\n{result}' for label, result in readings)[:16000]
    warning = '' if len(values) == 1 else 'Kein eindeutiger Messwert erkannt. Bitte am Foto prüfen und manuell eintragen.'
    if interrupted:
        warning = 'Die Texterkennung konnte nicht vollständig abgeschlossen werden. Bitte den Wert am Foto prüfen.'
    return text, values[:20], warning
