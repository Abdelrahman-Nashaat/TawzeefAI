# OCR layout diagnostic

This is a small synthetic diagnostic of page segmentation, not an accuracy benchmark. It compares Tesseract's automatic page segmentation (PSM 3, the application's current default), single-block mode (PSM 6), and sparse-text mode (PSM 11). The page examples, coordinates, and text are fixed in `backend/ocr_layout_diagnostic.py` and do not contain real applicant data.

## Reproduce

On Windows, install Tesseract and both pinned language models as described in [bilingual OCR setup](bilingual-ocr.md). With the OCR Python extra installed and the project root as the working directory:

```powershell
cd backend
uv run --locked --extra ocr python ocr_layout_diagnostic.py
```

The script renders four fictional examples with Windows System.Drawing and Arial, saves each raster image into a temporary scanned PDF, renders the PDF with PDFium at the application's scale of 2, and sends the resulting PNG to Tesseract over stdin. It runs all three modes with `eng+ara`. Temporary files are removed on normal exit. Output is one JSON object per sample, with Unicode escaped so that it remains inspectable on Windows consoles. If `TESSDATA_PREFIX` is set, it is respected; otherwise the script uses `.tooling/tessdata`. Results can vary with the installed font, Tesseract, language data, and platform.

## Observed run

On Windows using Tesseract `5.4.0.20240606` and the pinned `tessdata_fast` models on 2 November 2026:

| Synthetic layout | PSM 3 | PSM 6 | PSM 11 |
| --- | --- | --- | --- |
| Sparse, two widely separated language lines | Both lines returned | Both lines returned | Both lines returned |
| Uneven vertical spacing | Both lines returned | Both lines returned | Both lines returned |
| One Arabic line | Line returned | Line returned | Line returned |
| Two bilingual columns, two rows | The two columns were merged into each row | The two columns were merged into each row | Four separate lines were returned in row order |

For the two-column example, modes 3 and 6 began with the same combined line, shown here with the Arabic text represented by JSON Unicode escapes:

```text
PYTHON DEVELOPER \u200f\u0645\u0647\u0646\u062f\u0633 \u0628\u0631\u0645\u062c\u064a\u0627\u062a\u200e
```

Mode 11 returned the English and Arabic phrases as separate lines. Separating lines in this particular example does not prove the reading order or relationships between fields in a real résumé are correct. A separate development experiment on an original raster image found that PSM 11 omitted Arabic text from a denser layout; the run above is limited to the fixed PDF-rendered examples in the script.

These cases are too few to select a universally safer mode. Automatic mode remains unchanged. OCR may omit or alter content, combine separate columns, or change reading order; users must review the original document. Future work should evaluate a wider, independently reviewed range of synthetic and consented document layouts before changing the default or proposing automatic mode selection.
