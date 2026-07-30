# Parser Infrastructure

The first Excel-based pipeline stage is intentionally separate from Django HTTP
requests.

## Railway

Use two separate Railway processes:

1. Scheduler, once per day at `00:00 Europe/Tallinn`:

```bash
python manage.py run_all_parsers
```

2. Worker, always running as a separate service:

```bash
python manage.py parser_worker
```

The Django web process must not execute parsers inside admin HTTP requests.
Admin actions create `ParserQueueJob` records only; `parser_worker` processes
those records.

## Storage

Excel exports are saved through Django storage as `ParserExport.file`.
For local development the default location is:

```text
media/parser_exports/
```

Configurable environment variables:

```text
MEDIA_ROOT
PARSER_EXPORT_WORK_DIR
PARSER_EXPORT_RETENTION_DAYS
```
