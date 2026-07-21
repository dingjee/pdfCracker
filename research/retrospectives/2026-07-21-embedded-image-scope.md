# Embedded-image scope retrospective - 2026-07-21

## Trigger

The workflow needs images and caption anchors in Markdown, but rasterizing arbitrary page
regions would turn scanned or visually complex PDFs into an expensive and poorly bounded
vision problem. A second issue is that PDF image objects can represent shadows, backgrounds,
and translucent decorations rather than semantic content.

## Durable decision

- Extract only image objects that are actually embedded in the PDF.
- Never rasterize a page or region as an image-extraction fallback in the current scope.
- Preserve every decodable embedded object in the asset manifest, while independently
  classifying whether it should appear in Markdown.
- Use geometry, display area, aspect ratio, pixel entropy, transparency, repetition, and
  nearby text evidence before inserting an object into the reading copy.
- Route ambiguous objects to review. Treat scanned or otherwise inseparable page imagery as
  an explicit unsupported/deferred case rather than silently escalating vision usage.

## Regression coverage

- `tests/test_images.py` verifies embedded-object extraction without raster fallback.
- The same test module verifies that a translucent low-information layer remains an auditable
  asset but is classified as decoration and excluded from Markdown.
- `tests/test_audit.py` blocks publication when a declared extracted asset is missing.
