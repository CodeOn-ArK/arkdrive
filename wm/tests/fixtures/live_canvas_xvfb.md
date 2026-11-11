# Real X-rendered fx_canvas capture

`live_canvas_xvfb.png` is Raj's unmodified 560×280 capture from a live xvfb
session running `wm/fixtures/fx_canvas` on ArK2, supplied 2026-11-11 as
`~/live_canvas.png`. Source SHA-256:
`55736729c3fcbcbe99246b04dd91ef323ce5021a021eb4e61ee14d7df6beb52c`.

It reads `ArkDrive canvas`, `Alpha 123`, `Bravo 456`, `Target: W0`.
A pointer is present above/over the Bravo region. The standing headless test
asserts all four strings on the **whole window, pointer included**, as well as
the original diagnostic line crops (excluding the pointer for Bravo and
preserving the entire W0 line, crop x=8..240, y=200..248). Per-band refinement
retains Tesseract's Bravo line when its contaminated band abstains, while
independently refining the clean Alpha and W0 bands.
No source pixel is repainted or normalized. Rendering-mode corpus cases are
synthetic and are reported separately. No second real capture was generated;
the supplied capture plus eight complete synthetic modes provide the measured
variety without opening a live session.
