"""Locate large, bright digits in the middle of the boiler display, before OCR."""


def central_digits(gray):
    """Return a crop box, or None when no dominant central glyph group exists.

    Work in relative image coordinates so camera resolution and modest framing
    changes do not select a fixed pixel box. No recognized number or month is
    involved in choosing the region.
    """
    small = gray.copy()
    small.thumbnail((1200, 1200))
    width, height = small.size
    histogram = small.histogram()
    total = 0
    for bright, count in enumerate(histogram):
        total += count
        if total >= width * height * .997:
            break
    cutoff = max(96, int(bright * .78))
    # The main value is central. Exclude the title, footer buttons and the far
    # right column even when OCR finds these easier to read than the main value.
    left, top, right, bottom = int(width * .1), int(height * .25), int(width * .9), int(height * .65)
    area = small.crop((left, top, right, bottom))
    w, h = area.size
    pixels = bytearray(area.point(lambda pixel: int(pixel > cutoff)).tobytes())
    boxes = []
    for start, foreground in enumerate(pixels):
        if not foreground:
            continue
        pixels[start] = 0
        stack = [start]
        x0 = x1 = start % w
        y0 = y1 = start // w
        count = 0
        while stack:
            index = stack.pop()
            x, y = index % w, index // w
            count += 1
            x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
            for neighbor in (index - w if y else -1, index + w if y < h - 1 else -1,
                             index - 1 if x else -1, index + 1 if x < w - 1 else -1):
                if neighbor >= 0 and pixels[neighbor]:
                    pixels[neighbor] = 0
                    stack.append(neighbor)
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        # Discard clipped objects, diagram lines, solid bars and small labels.
        if (0 < x0 and x1 < w - 1 and 0 < y0 and y1 < h - 1
                and bh >= max(12, height * .04) and .1 <= bw / bh <= 4
                and .12 <= count / (bw * bh) <= .85):
            boxes.append((x0 + left, y0 + top, x1 + left + 1, y1 + top + 1))
    if not boxes:
        return None
    tallest = max(box[3] - box[1] for box in boxes)
    boxes = [box for box in boxes if box[3] - box[1] >= tallest * .7]
    groups = []
    for box in sorted(boxes):
        for group in groups:
            last = group[-1]
            if (0 <= box[0] - last[2] <= tallest * .9
                    and abs(box[3] - last[3]) <= tallest * .3):
                group.append(box)
                break
        else:
            groups.append([box])
    # Two equally large regions are ambiguous, even if only one is legible.
    if len(groups) != 1:
        return None
    group = groups[0]
    box = (min(b[0] for b in group), min(b[1] for b in group),
           max(b[2] for b in group), max(b[3] for b in group))
    if not .2 * width <= (box[0] + box[2]) / 2 <= .8 * width:
        return None
    # Tiny padding preserves strokes without including the smaller kWh label.
    sx, sy = gray.width / width, gray.height / height
    return (max(0, int((box[0] - 1) * sx)), max(0, int((box[1] - 1) * sy)),
            min(gray.width, int((box[2] + 1) * sx)), min(gray.height, int((box[3] + 1) * sy)))
