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
        ordering = ["name"]

    def __str__(self):
        return self.name


class ParserRun(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"

    TRIGGER_SCHEDULE = "schedule"
    TRIGGER_ADMIN = "admin"
    TRIGGER_COMMAND = "command"

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

    parser = models.ForeignKey(ParserConfig, related_name="runs", on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    trigger = models.CharField(max_length=20, choices=TRIGGER_CHOICES)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    products_found = models.PositiveIntegerField(default=0)
    products_created = models.PositiveIntegerField(default=0)
    products_updated = models.PositiveIntegerField(default=0)
    prices_changed = models.PositiveIntegerField(default=0)
    errors_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    log = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.parser.code} #{self.pk} ({self.status})"

# Create your models here.
