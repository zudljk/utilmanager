import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageOps

from gas.ocr import candidates_from_text, normalize_image, recognize, text_from_tsv


SAMPLE = Path(__file__).resolve().parent / 'fixtures' / 'IMG_0003.jpeg'


def tsv(words):
    header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    return header + ''.join(
        f'5\t1\t1\t1\t{line}\t{index}\t{left}\t{top}\t{width}\t{height}\t90\t{text}\n'
        for index, (text, left, top, width, height, line) in enumerate(words, 1))


class OCRTest(unittest.TestCase):
    def test_split_digits_merge_only_with_matching_geometry(self):
        words = [('1', 1260, 784, 40, 103, 1), ('9', 1340, 783, 76, 105, 1),
                 ('5', 1431, 782, 72, 105, 1), ('kWh', 1526, 778, 117, 108, 1)]
        self.assertEqual(candidates_from_text(text_from_tsv(tsv(words))), ['195'])
        split = [('1', 10, 0, 20, 50, 1), ('95', 45, 0, 60, 50, 1)]
        self.assertEqual(text_from_tsv(tsv(split)), '195')
        for other in [('95', 100, 0, 60, 50, 1),  # distant field
                      ('95', 45, 0, 20, 15, 1),  # different size
                      ('95', 45, 50, 60, 50, 2),  # another line
                      ('95', 45, 50, 60, 50, 1)]:  # different baseline
            self.assertNotIn('195', text_from_tsv(tsv([split[0], other])))
        self.assertEqual(text_from_tsv(tsv([('3000', 0, 0, 100, 50, 1),
                                           ('400', 110, 0, 80, 50, 1)])), '3000 400')

    def test_unresolved_digit_spacing_does_not_produce_truncated_reading(self):
        for text in ('1 9 5 kWh', '1 95 kWh', '3000 400 kWh'):
            self.assertEqual(candidates_from_text(text), [])
        self.assertEqual(candidates_from_text('195 kWh\n94 kWh'), ['195', '94'])
        self.assertEqual(candidates_from_text('2026\n1950', require_unit=True), [])

    def test_variants_preserve_conflicts_share_timeout_and_remove_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'photo.jpg'
            Image.new('RGB', (400, 200), 'black').save(source)
            original = source.read_bytes()
            paths, timeouts = [], []
            clock = [0]
            outputs = iter(['195 kWh', '94 kWh', '1950'])

            def run(command, **kwargs):
                paths.append(Path(command[1]))
                timeouts.append(kwargs['timeout'])
                clock[0] += 8
                return subprocess.CompletedProcess(command, 0, tsv([(next(outputs), 0, 0, 100, 50, 1)]), '')

            with patch('gas.ocr.subprocess.run', side_effect=run), patch('gas.ocr.monotonic', side_effect=lambda: clock[0]):
                text, values, warning = recognize(source)
            self.assertEqual(values, ['195', '94'])
            self.assertTrue(warning)
            self.assertIn('Schwelle 210', text)
            self.assertEqual(timeouts, [25, 17, 9])
            self.assertTrue(all(not path.exists() for path in paths[1:]))
            self.assertEqual(source.read_bytes(), original)

    def test_ocr_timeout_preserves_prior_suggestions_with_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'photo.jpg'
            Image.new('RGB', (400, 200), 'black').save(source)
            clock = [0]
            calls = [0]

            def run(command, **kwargs):
                calls[0] += 1
                if calls[0] == 1:
                    clock[0] = 1
                    return subprocess.CompletedProcess(command, 0, tsv([('195 kWh', 0, 0, 100, 50, 1)]), '')
                clock[0] = 25
                raise subprocess.TimeoutExpired(command, kwargs['timeout'])

            with patch('gas.ocr.subprocess.run', side_effect=run), patch('gas.ocr.monotonic', side_effect=lambda: clock[0]):
                _, values, warning = recognize(source)
            self.assertEqual(values, ['195'])
            self.assertIn('nicht vollständig', warning)
            self.assertEqual(calls[0], 2)

    @unittest.skipUnless(SAMPLE.exists() and shutil.which('tesseract'), 'Lokales Beispielfoto und Tesseract erforderlich')
    def test_real_display_photo_and_shortcut_sized_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            payloads = [SAMPLE.read_bytes()]
            with Image.open(SAMPLE) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((1600, 1600))
                output = io.BytesIO()
                image.save(output, format='JPEG')
                payloads.append(output.getvalue())
            for index, payload in enumerate(payloads):
                with self.subTest(version=index):
                    source = Path(directory) / 'photo.jpg'
                    source.write_bytes(normalize_image(payload))
                    text, values, warning = recognize(source)
                    self.assertEqual(values, ['195'], text)
                    self.assertEqual(warning, '')


if __name__ == '__main__':
    unittest.main()
