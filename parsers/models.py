from datetime import time

from django.db import models


class ParserConfig(models.Model):
    STATUS_NEVER = "never"
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = [
        (STATUS_NEVER, "Never"),
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
    ]

    shop = models.OneToOneField(
        "catalog.Shop",
        related_name="parser_config",
        on_delete=models.CASCADE,
    )
    name = models.CharField(max_length=150)
    code = models.SlugField(max_length=50, unique=True)
    is_enabled = models.BooleanField(default=True)
    run_order = models.PositiveIntegerField(default=100)
    run_time = models.TimeField(default=time(3, 0))
    is_running = models.BooleanField(default=False)
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_finished_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_NEVER,
    )
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["run_order", "name"]

    def __str__(self):
        return self.name


class ParserRun(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"

    TRIGGER_ADMIN = "admin"
    TRIGGER_COMMAND = "command"
    TRIGGER_SCHEDULE = "schedule"

    STAGE_PENDING = "pending"
    STAGE_PARSING = "parsing"
    STAGE_EXCEL_VALIDATION = "excel_validation"
    STAGE_DATABASE_IMPORT = "database_import"
    STAGE_COMPLETED = "completed"

    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]
    TRIGGER_CHOICES = [
        (TRIGGER_SCHEDULE, "Schedule"),
        (TRIGGER_ADMIN, "Admin"),
        (TRIGGER_COMMAND, "Command"),
    ]
    STAGE_CHOICES = [
        (STAGE_PENDING, "Pending"),
        (STAGE_PARSING, "Parsing"),
        (STAGE_EXCEL_VALIDATION, "Excel validation"),
        (STAGE_DATABASE_IMPORT, "Database import"),
        (STAGE_COMPLETED, "Completed"),
    ]

    parser = models.ForeignKey(ParserConfig, related_name="runs", on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    stage = models.CharField(max_length=30, choices=STAGE_CHOICES, default=STAGE_PENDING)
    trigger = models.CharField(max_length=20, choices=TRIGGER_CHOICES)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    excel_rows_count = models.PositiveIntegerField(default=0)
    products_found = models.PositiveIntegerField(default=0)
    products_created = models.PositiveIntegerField(default=0)
    products_updated = models.PositiveIntegerField(default=0)
    prices_changed = models.PositiveIntegerField(default=0)
    skipped_rows = models.PositiveIntegerField(default=0)
    errors_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    log = models.TextField(blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    cancel_requested = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["parser", "status"],
                condition=models.Q(status="running"),
                name="unique_running_parser_run_per_parser",
            )
        ]

    def __str__(self):
        return f"{self.parser.code} #{self.pk} ({self.status})"


class ParserExport(models.Model):
    parser_run = models.OneToOneField(
        ParserRun,
        related_name="export",
        on_delete=models.CASCADE,
    )
    shop = models.ForeignKey(
        "catalog.Shop",
        related_name="parser_exports",
        on_delete=models.CASCADE,
    )
    file = models.FileField(upload_to="parser_exports/%Y/%m/%d/")
    original_filename = models.CharField(max_length=255)
    rows_count = models.PositiveIntegerField(default=0)
    file_size = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    imported_at = models.DateTimeField(null=True, blank=True)
    import_success = models.BooleanField(default=False)
    validation_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.original_filename


class ParserBatch(models.Model):
    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_PARTIAL = "partial"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"

    TRIGGER_SCHEDULE = ParserRun.TRIGGER_SCHEDULE
    TRIGGER_ADMIN = "admin"
    TRIGGER_COMMAND = "command"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_PARTIAL, "Partial"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]
    TRIGGER_CHOICES = ParserRun.TRIGGER_CHOICES

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    trigger = models.CharField(max_length=20, choices=TRIGGER_CHOICES, default=TRIGGER_COMMAND)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    current_parser = models.ForeignKey(
        ParserConfig,
        null=True,
        blank=True,
        related_name="active_batches",
        on_delete=models.SET_NULL,
    )
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    log = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["status"],
                condition=models.Q(status="running"),
                name="unique_running_parser_batch",
            )
        ]

    def __str__(self):
        return f"Parser batch #{self.pk} ({self.status})"


class ParserBatchLock(models.Model):
    name = models.CharField(max_length=50, unique=True, default="nightly_parser_batch")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Parser batch lock"
        verbose_name_plural = "Parser batch locks"

    def __str__(self):
        return self.name


class ParserQueueJob(models.Model):
    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    parser_config = models.ForeignKey(
        ParserConfig,
        null=True,
        blank=True,
        related_name="queue_jobs",
        on_delete=models.CASCADE,
    )
    batch = models.ForeignKey(
        ParserBatch,
        null=True,
        blank=True,
        related_name="queue_jobs",
        on_delete=models.SET_NULL,
    )
    parser_run = models.OneToOneField(
        ParserRun,
        null=True,
        blank=True,
        related_name="queue_job",
        on_delete=models.SET_NULL,
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    trigger = models.CharField(max_length=20, choices=ParserRun.TRIGGER_CHOICES, default=ParserRun.TRIGGER_ADMIN)
    run_all = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    log = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        target = "all parsers" if self.run_all else self.parser_config
        return f"Parser job #{self.pk} ({target})"

# Create your models here.
