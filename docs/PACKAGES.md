# Job packages

A job package is a folder (or a `.zip` of one) that holds a manifest plus the
code and data files the job needs. Submitting it is one step, whichever way
you do it:

```powershell
scripts\pc.ps1 run examples\packages\wordcount --wait      # folder, zipped for you
scripts\pc.ps1 run my-job.zip                               # a zip you made
scripts\pc.ps1 run my-job --dry-run                         # show the resolved spec, submit nothing
```

```python
job = c.run_package(r"D:\jobs\wordcount")                   # Job or Pipeline
print(job.wait().result())
```

Dashboard: **Jobs → Run package** and pick a `.zip`. The API is
`POST /api/packages` (multipart field `file`, optional `?dry_run=true`).

## Manifest

The manifest is `job.yaml`, `job.yml` or `job.json` at the package root. It is
an ordinary job spec (`docs/SPEC.md` §5):

```yaml
name: resize photos
type: media
params:
  op: resize
  width: 800
  inputs: "@photos/*.jpg"        # every match is uploaded
options:
  timeout: 300
priority: 1
```

or a pipeline (§5.1) under a `jobs:` list:

```yaml
name: primes then report
jobs:
  - key: count
    type: prime_range
    params: { start: 1, end: 2000000 }
  - key: report
    type: python_map
    depends_on: [count]
    params:
      code: "@report.py"
      fn_name: describe
      items: [ { $from_job: count, path: count } ]
```

## Referencing files

**`"@path"` shorthand**, allowed directly under `params`, is resolved by the
plugin's declared parameter type (see `GET /api/plugins`):

| param type | `"@path"` becomes |
|---|---|
| `code`, `str` | the file's text, inlined |
| `file` | the file is uploaded; the param becomes its file id |
| `files` | one glob or a list of globs; every match is uploaded; the param becomes a list of ids |
| `json` | the parsed content (`.yaml` / `.yml` are parsed as YAML) |
| `int`, `float`, `bool`, `select` | not allowed |
| not declared by the plugin | parsed if the file ends in `.json` / `.yaml` / `.yml`, otherwise text |

**Explicit forms** work anywhere, including inside nested dicts and lists
(for example inside a `custom` job's `impls`):

```yaml
params:
  impls:
    python:
      code: { $text: main.py }
      args: { $json: settings.yaml }
  inputs: [ { $file: data.bin } ]
  args_list:
    - { extra: { $files: "more/*.csv" } }
```

`$text` inlines, `$file` uploads one file, `$files` uploads every match of a
glob (or list of globs), `$json` parses.

Rules: paths are relative to the package root and may not leave it; a glob
that matches nothing is an error; the same file is uploaded once even if
referenced twice; `__pycache__`, `.git` and `*.pyc` are never packed or
extracted. Zips made from a folder (`myjob/job.yaml`, `myjob/a.py`) are fine:
the common top folder is stripped.

## Task code and the files

For `python_map` the task function receives every input path in order via
`ctx.inputs`, so a common pattern is to map over indexes:

```yaml
type: python_map
params:
  code: "@count.py"
  fn_name: count_words
  inputs: "@texts/*.txt"
  items: [0, 1, 2]
  chunk_size: 1
```

```python
def count_words(item, ctx):
    with open(ctx.inputs[int(item)]) as fh:
        return len(fh.read().split())
```

For `shell` and `media` jobs the inputs appear as `PC_INPUT_0`, `PC_INPUT_1`,
… and anything written to `PC_OUTPUT_DIR` is uploaded as an output.

## Limits

Up to 1 GiB unpacked and 10 000 files per package. Uploading a big zip
through the dashboard keeps it in browser memory; use the CLI for large ones.
Two ready-made packages live in `examples/packages/`.
