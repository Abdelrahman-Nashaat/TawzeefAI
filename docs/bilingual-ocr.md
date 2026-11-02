# Bilingual scanned-PDF OCR

On Windows, install Tesseract 5 and run this from the project directory:

```powershell
& .\setup-ocr-languages.ps1
& .\start-demo.ps1 -Ocr
```

The setup script downloads English and Arabic models from the official [Tesseract fast language-data repository](https://github.com/tesseract-ocr/tessdata_fast) at revision `87416418657359cb625c412a48b6e1d6d41c29bd` and checks their pinned SHA-256 hashes. Files are stored under `.tooling/tessdata`, which is excluded from Git. The script refuses to overwrite an existing file with different content. Tesseract itself must be installed separately. If `TESSDATA_PREFIX` is already set, the startup script respects it instead of using this directory. Its configured directory must contain both `eng.traineddata` and `ara.traineddata` for the bilingual option.

Choose **English and Arabic** for each scanned PDF upload. The server returns an error if the requested models are missing; selecting a language does not establish that it is installed. OCR is optional and processes only pages without an extracted text layer, with a maximum of five pages per upload. It can miss or alter text, especially on mixed-language pages or poor scans, and does not see image text embedded on a page that already has extracted text. Check the extracted text and OCR-marked citations against the original document. The original PDF is not retained by the application.

These are fast integer models for Tesseract's LSTM engine; the upstream [data documentation](https://github.com/tesseract-ocr/tessdoc/blob/main/Data-Files.md) describes the available model families and their tradeoffs. One sparse mixed-language synthetic layout missed the Arabic text with default segmentation, while a denser synthetic scan was recognized. These examples do not establish accuracy on real résumés or policies.
