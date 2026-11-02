"""Compare local OCR segmentation on fictional rasterized samples on Windows."""

import base64
import json
import os
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

SAMPLES = {
    'sparse': [('eng', 'PYTHON DEVELOPER', 100, 120), ('ara', 'مهندس برمجيات', 100, 320)],
    'uneven': [('eng', 'PYTHON DEVELOPER', 50, 100), ('ara', 'مهندس برمجيات', 100, 530)],
    'single': [('ara', 'مهندس برمجيات', 100, 300)],
    'columns': [('eng', 'PYTHON DEVELOPER', 100, 100), ('eng', 'EXPERIENCE FIVE YEARS', 100, 200),
                ('ara', 'مهندس برمجيات', 1000, 100), ('ara', 'خبرة خمس سنوات', 1000, 200)],
}


def render_png(path: Path, lines: list[tuple[str, str, int, int]]) -> None:
    # All content and coordinates are fixed synthetic values, never user input.
    script = [
        'Add-Type -AssemblyName System.Drawing',
        '$bitmap = New-Object System.Drawing.Bitmap 2000, 800',
        '$graphics = [System.Drawing.Graphics]::FromImage($bitmap)',
        '$graphics.Clear([System.Drawing.Color]::White)',
        "$font = New-Object System.Drawing.Font 'Arial', 75, ([System.Drawing.FontStyle]::Regular), ([System.Drawing.GraphicsUnit]::Pixel)",
        '$format = New-Object System.Drawing.StringFormat',
        '$format.FormatFlags = [System.Drawing.StringFormatFlags]::DirectionRightToLeft',
    ]
    for language, text, x, y in lines:
        if language == 'ara':
            script.extend([f'$rect = New-Object System.Drawing.RectangleF {x}, {y}, 800, 120',
                           f"$graphics.DrawString('{text}', $font, [System.Drawing.Brushes]::Black, $rect, $format)"])
        else:
            script.append(f"$graphics.DrawString('{text}', $font, [System.Drawing.Brushes]::Black, {x}, {y})")
    target = str(path).replace("'", "''")
    script.extend([f"$bitmap.Save('{target}', [System.Drawing.Imaging.ImageFormat]::Png)",
                   '$format.Dispose()', '$font.Dispose()', '$graphics.Dispose()', '$bitmap.Dispose()'])
    encoded = base64.b64encode('\n'.join(script).encode('utf-16le')).decode('ascii')
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


def rendered_page(path: Path) -> bytes:
    # Match the application path: raster image -> scanned PDF -> PDFium at scale 2.
    pdf = BytesIO()
    with Image.open(path) as image:
        image.convert('RGB').save(pdf, format='PDF', resolution=150)
    document = pdfium.PdfDocument(pdf.getvalue())
    try:
        page = document[0]
        try:
            bitmap = page.render(scale=2)
            try:
                output = BytesIO()
                bitmap.to_pil().save(output, format='PNG')
                return output.getvalue()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()


def main() -> None:
    if os.name != 'nt':
        raise SystemExit('This diagnostic uses Windows System.Drawing and Arial.')
    executable = os.environ.get('TAWZEEF_TESSERACT_PATH') or shutil.which('tesseract')
    executable = executable or r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    allowed = {'path', 'systemroot', 'windir', 'temp', 'tmp', 'tessdata_prefix'}
    environment = {key: value for key, value in os.environ.items() if key.casefold() in allowed}
    models = Path(__file__).resolve().parents[1] / '.tooling' / 'tessdata'
    environment.setdefault('TESSDATA_PREFIX', str(models))
    with tempfile.TemporaryDirectory(prefix='tawzeef-ocr-layout-') as temporary:
        for name, lines in SAMPLES.items():
            path = Path(temporary) / f'{name}.png'
            render_png(path, lines)
            image = rendered_page(path)
            results = {}
            for mode in ('3', '6', '11'):
                result = subprocess.run([executable, 'stdin', 'stdout', '-l', 'eng+ara', '--psm', mode],
                    input=image, env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=40, check=True)
                results[mode] = result.stdout.decode('utf-8').strip()
            print(json.dumps({'sample': name, 'psm': results}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
