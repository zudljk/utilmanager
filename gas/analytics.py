"""Monthly actuals and a seasonal forecast, with dependency-free SVG geometry."""
from decimal import Decimal, ROUND_CEILING

from .domain import ledger, shift_month


def monthly_analysis(db, today):
    current = today.strftime('%Y-%m')
    first = shift_month(current, -12)
    settings = db.execute('SELECT * FROM settings WHERE id=1').fetchone()
    if not settings:
        return []
    historical = {r['month']: r for r in ledger(db, shift_month(current, -1))}
    readings = {r['month']: Decimal(r['kwh']) for r in db.execute('SELECT month,kwh FROM consumption')}
    supplies = {}
    for r in db.execute('SELECT month,liters FROM deliveries'):
        supplies[r['month']] = supplies.get(r['month'], Decimal(0)) + Decimal(r['liters'])
    factor = Decimal(settings['factor'])
    anchor = historical.get(shift_month(current, -1))
    balance = anchor['rest'] * factor if anchor and anchor['rest'] is not None else None
    if settings['start_month'] == current:
        balance = Decimal(settings['opening_liters']) * factor
    rows = []
    for offset in range(24):
        month = shift_month(first, offset)
        projected = month >= current
        prior_month = shift_month(month, -12)
        if projected:
            demand = readings.get(prior_month)
            if balance is not None and demand is not None:
                balance += supplies.get(month, Decimal(0)) * factor - demand
            else:
                balance = None
            rest = balance / factor if balance is not None else None
        else:
            actual = historical.get(month, {})
            demand, rest = actual.get('kwh'), actual.get('rest')
        rows.append(dict(month=month, kwh=demand, rest=rest, projected=projected,
                         prior_month=prior_month if projected else None,
                         delivery_liters=supplies.get(month, Decimal(0))))
    return rows


def chart_geometry(rows):
    """Separate axes; missing samples split paths rather than bridging gaps."""
    chart = dict(series=[], ticks=[], labels=[], boundary=550)
    for field, label, unit in [('kwh', 'Verbrauch', 'kWh'), ('rest', 'Restmenge', 'l')]:
        values = [r[field] for r in rows if r[field] is not None]
        low = min([Decimal(0)] + values)
        high = max([Decimal(1)] + values)
        # Round the span outward to a readable step, including negative stocks.
        span = high - low
        magnitude = Decimal(10) ** (span.adjusted() - 1)
        step = (span / 5 / magnitude).to_integral_value(rounding=ROUND_CEILING) * magnitude
        low = (low // step - (1 if low % step else 0)) * step
        high = (high / step).to_integral_value(rounding=ROUND_CEILING) * step
        tick_digits = max(0, -((high - low) / 5).normalize().as_tuple().exponent)
        chart['ticks'].append(dict(field=field, digits=tick_digits, values=[
            dict(y=round(330 - i * 270 / 5, 2), value=low + (high - low) * i / 5)
            for i in range(6)]))
        points = []
        paths = {False: [], True: []}
        for i, row in enumerate(rows):
            x = round(90 + i * 920 / 23, 2)
            value = row[field]
            y = round(330 - float((value - low) / (high - low)) * 270, 2) if value is not None else None
            point = dict(x=x, y=y, value=value, month=row['month'], projected=row['projected'])
            points.append(point)
        for projected in (False, True):
            segment = []
            # Connect the final actual point to the first forecast point.
            samples = points[:12] if not projected else points[11:]
            for point in samples:
                if point['y'] is None:
                    if segment:
                        paths[projected].append(' '.join(segment))
                    segment = []
                else:
                    segment.append(f"{point['x']},{point['y']}")
            if segment:
                paths[projected].append(' '.join(segment))
        chart['series'].append(dict(field=field, label=label, unit=unit, paths=paths, points=points))
    chart['labels'] = [dict(x=90 + i * 40, month=r['month']) for i, r in enumerate(rows)]
    return chart
