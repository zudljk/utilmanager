import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class ValidationError(ValueError):
    pass


def number(value, label='Wert', *, positive=False, optional=False):
    text = str('' if value is None else value).strip().replace(' ', '').replace('\u00a0', '')
    if not text and optional:
        return None
    # Form input accepts decimal comma or decimal point, without grouping separators.
    if not re.fullmatch(r'\d+(?:[.,]\d{1,6})?', text):
        raise ValidationError(f'{label}: Bitte eine Zahl ohne Tausendertrennzeichen eingeben.')
    try:
        result = Decimal(text.replace(',', '.'))
    except InvalidOperation as exc:
        raise ValidationError(f'{label}: Ungültige Zahl.') from exc
    if result > Decimal('1000000000') or (positive and result <= 0):
        raise ValidationError(f'{label}: Wert außerhalb des erlaubten Bereichs.')
    return result


def month_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', value):
        raise ValidationError('Bitte einen gültigen Monat im Format YYYY-MM angeben.')
    if not 1900 <= int(value[:4]) <= 2200:
        raise ValidationError('Das Jahr muss zwischen 1900 und 2200 liegen.')
    return value


def shift_month(month, offset):
    year, mm = map(int, month.split('-'))
    year, mm = divmod(year * 12 + mm - 1 + offset, 12)
    return f'{year:04d}-{mm + 1:02d}'


def previous_month(today=None):
    return shift_month((today or date.today()).strftime('%Y-%m'), -1)


def validate_period(month, settings, today=None, completed=True):
    month = month_key(month)
    if month < settings['start_month']:
        raise ValidationError('Der Monat liegt vor dem Anfangsbestand.')
    limit = previous_month(today) if completed else (today or date.today()).strftime('%Y-%m')
    if month > limit:
        raise ValidationError('Verbrauch kann nur für abgeschlossene Monate erfasst werden.' if completed
                              else 'Lieferungen dürfen nicht in der Zukunft liegen.')
    return month


def formatted(value, digits=0):
    if value is None:
        return '–'
    value = Decimal(str(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return f'{value:,.{digits}f}'.replace(',', '_').replace('.', ',').replace('_', '.')


def quantity(value):
    return formatted(value, 6).rstrip('0').rstrip(',') if value is not None else '–'


def ledger(db, end_month):
    settings = db.execute('SELECT * FROM settings WHERE id=1').fetchone()
    if not settings:
        return []
    readings = {r['month']: dict(r) for r in db.execute('SELECT * FROM consumption')}
    comparisons = {r['month']: Decimal(r['kwh']) for r in db.execute('SELECT * FROM comparisons')}
    deliveries = {}
    # A historical valuation is not evidence of the purchase price of a delivery.
    prices = {r['month']: Decimal(r['unit_price']) for r in db.execute('SELECT * FROM historical_prices')}
    for r in db.execute('SELECT * FROM deliveries ORDER BY month, COALESCE(delivery_date,month),id'):
        deliveries.setdefault(r['month'], []).append(dict(r))
        if r['unit_price'] is not None:
            prices[r['month']] = Decimal(r['unit_price'])
    factor = Decimal(settings['factor'])
    capacity = Decimal(settings['capacity_liters'])
    # Keep the balance in kWh, avoiding repeated rounding of kWh/liter divisions.
    actual = Decimal(settings['opening_liters']) * factor
    projected = actual
    complete = True
    price = None
    used_estimate = False
    rows = []
    month = settings['start_month']
    while month <= end_month:
        reading = readings.get(month)
        kwh = Decimal(reading['kwh']) if reading else None
        prior_month = shift_month(month, -12)
        prior_reading = readings.get(prior_month)
        prior = Decimal(prior_reading['kwh']) if prior_reading else comparisons.get(month)
        supply = deliveries.get(month, [])
        liters = sum((Decimal(d['liters']) for d in supply), Decimal(0))
        price = prices.get(month, price)
        if kwh is None:
            complete = False
        if complete:
            actual += liters * factor - kwh
        else:
            actual = None
        demand = kwh if kwh is not None else prior
        if kwh is None:
            used_estimate = True
        if projected is not None and demand is not None:
            projected += liters * factor - demand
        else:
            projected = None
        rest = actual / factor if actual is not None else None
        forecast = projected / factor if projected is not None else None
        rows.append(dict(month=month, reading=reading, kwh=kwh, deliveries=supply,
                         delivery_liters=liters, rest=rest,
                         fill=rest / capacity * 100 if rest is not None else None,
                         prior=prior, forecast=forecast, estimated=used_estimate,
                         forecast_fill=max(Decimal(0), forecast / capacity * 100) if forecast is not None else None,
                         price=price, cost=demand / factor * price if demand is not None and price is not None else None,
                         cost_estimated=kwh is None))
        month = shift_month(month, 1)
    return rows
