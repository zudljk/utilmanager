import io
import re
import subprocess
from decimal import Decimal

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


def candidates_from_text(text):
    # Prefer values explicitly followed by kWh; never add multiple displayed numbers.
    tokens = re.findall(r'(?<![\d.,])([0-9]+(?:[.,][0-9]+)*)\s*k\s*w\s*h\b', text, re.I)
    if not tokens:
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


def recognize(path):
    try:
        result = subprocess.run(['tesseract', str(path), 'stdout', '-l', 'eng', '--psm', '6'],
                                capture_output=True, text=True, timeout=25, check=True)
        text = result.stdout[:16000]
        values = candidates_from_text(text)
        warning = '' if len(values) == 1 else 'Kein eindeutiger Messwert erkannt. Bitte am Foto prüfen und manuell eintragen.'
        return text, values, warning
    except FileNotFoundError:
        return '', [], 'Tesseract ist nicht installiert. Das Foto kann manuell ausgewertet werden.'
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return '', [], 'Die Texterkennung konnte nicht abgeschlossen werden. Bitte den Wert am Foto ablesen.'
