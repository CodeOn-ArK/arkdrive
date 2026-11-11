Place the official English model here as `eng.traineddata` to run the required
best-model comparison. No model bytes were available in the sandbox at the time
of the fix-round implementation: shell network sockets are denied.

Upstream: https://github.com/tesseract-ocr/tessdata_best
Download: https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/eng.traineddata
License: https://github.com/tesseract-ocr/tessdata_best/blob/main/LICENSE

From the repository root, after obtaining the model:

```sh
OMP_THREAD_LIMIT=1 PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 wm/tests/ocr_corpus.py --output bench/w7-ocr-candidates.json
OMP_THREAD_LIMIT=1 PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 wm/tests/ocr_corpus.py --output bench/w7-ocr-candidates.json --shapes-only
```

The report records the actual file's SHA-256. A missing best model makes the
default comparison exit 2 and sets `comparison_complete: false`. The explicit
`--allow-missing-best` option supports exploratory fast-model measurements; it
does not declare the comparison complete or qualify that missing candidate.
