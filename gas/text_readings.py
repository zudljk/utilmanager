"""Interpret iPhone OCR text without confusing a column value with the selection."""
import re

from .ocr import candidates_from_text


MAX_TEXT_LENGTH = 16000
MAX_TEXT_REQUEST = 64 * 1024
NUMBER = r'\d{1,7}(?:[.,]\d{1,6})*'
MONTH_LABEL = re.compile(r'(?:Jan|Feb|Mär|März|Mrz|Apr|Mai|Jun|Jul|Aug|Sep|Sept|Okt|Nov|Dez)\.?', re.I)


def unit_kind(unit):
    compact = re.sub(r'\s+', '', unit).casefold().replace('vv', 'w')
    if compact == 'kwh':
        return 'exact'
    # Do not reinterpret other actual units as a misspelling of kWh.
    if compact in ('mwh', 'wh', 'w', 'mw', 'l', 'liter'):
        return None
    target = 'kwh'
    if len(compact) == 3 and sum(a != b for a, b in zip(compact, target)) == 1:
        return 'uncertain'
    if len(compact) == 2 and any(target[:i] + target[i+1:] == compact for i in range(3)):
        return 'uncertain'
    if len(compact) == 4 and any(compact[:i] + compact[i+1:] == target for i in range(4)):
        return 'uncertain'
    return None


def extract_text_reading(text):
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    lines = [line.strip() for line in lines]
    nonempty = [(i, line) for i, line in enumerate(lines) if line]
    if not nonempty or not re.fullmatch(r'Gas\s*verbrauch\s*:?', nonempty[0][1], re.I):
        return [], 'Die Überschrift „Gasverbrauch“ fehlt. Bitte den übertragenen Text prüfen und den Wert manuell eintragen.'

    values = []
    uncertain = False
    for i, line in nonempty:
        match = re.fullmatch(rf'({NUMBER})[ \t]*([A-Za-z|?][A-Za-z|? \t]{{0,11}})', line)
        if not match:
            continue
        kind = unit_kind(match[2])
        isolated = i > 0 and i < len(lines)-1 and not lines[i-1] and not lines[i+1]
        if kind is None or (kind == 'uncertain' and not isolated):
            continue
        parsed = candidates_from_text(match[1] + ' kWh', require_unit=True)
        for value in parsed:
            if value not in values:
                values.append(value)
        uncertain |= kind == 'uncertain'

    if not values:
        # An entirely missing unit is usable only as an isolated number after
        # the month headings and before the current month's split number/unit.
        # Never join "94\nKWh", even if more blank lines separate the two.
        month_rows = [i for i, line in nonempty if MONTH_LABEL.fullmatch(line)]
        split_values = [i for (i, value), (_, unit) in zip(nonempty, nonempty[1:])
                        if re.fullmatch(NUMBER, value) and unit_kind(unit)]
        if len(month_rows) >= 3 and split_values:
            start, end = max(month_rows), min(split_values)
            for i, line in nonempty:
                if (start < i < end and re.fullmatch(NUMBER, line)
                        and not lines[i-1] and i+1 < len(lines) and not lines[i+1]):
                    for value in candidates_from_text(line):
                        if value not in values:
                            values.append(value)
            uncertain = bool(values)

    if len(values) != 1:
        return values[:20], 'Kein eindeutiger Monatswert im Text. Kleine Spaltenwerte mit der Einheit in der nächsten Zeile werden nicht übernommen. Bitte den Wert manuell prüfen.'
    note = 'Die Einheit ist unklar oder fehlt; kWh wurde nur als Vorschlag angenommen. ' if uncertain else ''
    return values, note + 'Bitte Messwert und Verbrauchsmonat prüfen. Der Monat wurde nicht aus dem OCR-Text bestimmt.'
