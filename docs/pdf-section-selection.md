# PDF section selection

The optional section preview lets HR mark specific extracted PDF lines as headings. It is intended for a PDF whose section structure a reviewer can identify; the application does not infer heading meaning or typography. Repeated headings can be distinguished by physical page and line number. A selected heading remains associated with following text until another selected heading, including across physical pages. Text before the first heading is left unlabelled.

The feature uses the existing HR-authenticated local API:

1. Send the PDF bytes to `POST /api/knowledge/pdf-section-preview?filename=policy.pdf`. The response contains extracted page text, a numbered array of lines for each page, page numbers, OCR page metadata, and a preview fingerprint. The preview is not stored as a knowledge document. If using OCR, use the same `ocr=true` and `ocr_language` values in both steps.
2. Review the text against the source. Prepare a JSON array of selected lines such as `[{"page":1,"line":0},{"page":2,"line":3}]`. Pages are one-based and lines are zero-based indexes into each page's returned `lines` array. At most 100 lines may be selected. Each must be a unique, nonempty printable line of at most 200 characters.
3. Upload the same PDF bytes to `POST /api/knowledge/documents` with the normal title, audience, filename and collection parameters, plus `pdf_selected_headings` containing that JSON array and `pdf_preview_sha256` containing the preview fingerprint. The server re-extracts the file and checks that the bytes, extraction output, OCR pages and selected language match the preview before storing it.

Changing the file, extraction output, or OCR settings requires a new preview. A wrong or duplicate selection, malformed metadata, or changed preview is rejected instead of silently producing section citations. A knowledge result carries the selected heading, physical page number, and OCR warning when applicable. The section label is extracted document text, not an instruction. Role filtering and source checks still apply. Previous stored documents, legacy citation identifiers, and regular uploads without this option are unchanged.

In the HR knowledge-document form, choose a PDF and optional OCR language, select **Preview PDF headings**, open the page groups, and check the lines that start sections. The preview shows up to 2,000 lines; a larger document can still be uploaded without selected headings. Change the file or OCR setting to discard a preview. The final upload uses the same source and makes the server recheck the preview fingerprint.

Text extraction or OCR may omit or reorder text, and a reviewer may select the wrong line. PDF page numbers may differ from printed numbers. The original PDF is not retained, so keep it separately for verification. These controls do not prove answer correctness or document completeness.
