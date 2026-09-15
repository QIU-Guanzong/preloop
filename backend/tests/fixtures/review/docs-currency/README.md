# Docs currency review fixtures

Synthetic fixtures for `backend/presets/016-docs-currency-review.yaml`
and `backend/tests/test_docs_currency_review_preset.py`. Nothing here
comes from a real deployment.

- `projects/<name>/` is a tiny project: some documentation plus the code
  the documentation talks about.
- `results/result-<name>.json` is the `preloop.review.docscurrency/v1`
  result a run over that project is contracted to produce.
- `schemas/docscurrency-v1.json` is the JSON Schema for that envelope.

The four projects cover the four behaviours the lens is judged on:

| project | what it exercises |
| --- | --- |
| `drifted-build-command` | a README naming a build command the code does not have: one high severity drift row, failing verdict |
| `accurate` | documentation that matches the code: no drift rows, passing verdict |
| `not-checkable` | claims the checkout cannot decide: reported with a reason, never counted as holding |
| `prose-quality` | badly written but accurate documentation: zero findings, passing verdict |

## Recorded searches

Every claim records the search that classified it, so the test can
re-run it against the project and confirm the recorded matches. Two
search kinds, both cheap and deterministic:

- `filename:<repo-relative path>` looks the path up in the project file
  listing. A hit is recorded as `<path>:0`, because a file's existence
  is not a line.
- anything else is a regular expression matched line by line against
  every file in `scope`. `scope` `.` is the whole project. A scope that
  ends in `/` is a directory prefix. Any other scope is an exact file
  path, so `widget` does not match `widget-plus/...` and
  `pyproject.toml` does not match `pyproject.toml.bak`. A hit is
  recorded as `<path>:<line>`.

A drifted claim is an absence claim: its recorded search is expected to
return no match in the scope it declares.
