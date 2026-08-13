# Parser Infrastructure

The first Excel-based pipeline stage is intentionally separate from Django HTTP
requests.

## Railway

Use two separate Railway processes. The scheduler must only enqueue work and
exit; the always-on worker performs the long-running batch.

1. Scheduler, once per day:

```bash
python manage.py run_nightly_parsers
```

2. Worker, always running as a separate service:

```bash
python manage.py parser_worker
```

Railway cron expressions use UTC. Configure the Railway schedule with the
desired Tallinn UTC offset and adjust it when daylight-saving time changes.
`run_nightly_parsers` is idempotent while an all-parsers job is pending or
running, so overlapping scheduler calls do not duplicate a nightly batch.

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
