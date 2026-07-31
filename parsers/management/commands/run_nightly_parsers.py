from django.core.management.base import BaseCommand, CommandError

from parsers.adapters.registry import ADAPTERS
from parsers.models import ParserBatch, ParserConfig, ParserRun
from parsers.services.batch_runner import ParserBatchAlreadyRunning, run_all_parsers
from parsers.services.recovery import recover_stale_parser_state


class Command(BaseCommand):
    help = "Run all enabled production parsers as a scheduled nightly batch."

    def handle(self, *args, **options):
        self.stdout.write("Nightly parser batch starting...")
        self.stdout.write("Recovering stale parser state...")
        recovered = recover_stale_parser_state()
        self.stdout.write(f"Recovered stale parser state: runs={recovered.runs}, jobs={recovered.jobs}, batches={recovered.batches}")

        running_batch = ParserBatch.objects.filter(status=ParserBatch.STATUS_RUNNING).select_related("current_parser").first()
        if running_batch:
            current_parser = f"; current_parser={running_batch.current_parser.code}" if running_batch.current_parser else ""
            self.stdout.write(
                self.style.WARNING(
                    f"Parser batch is already running: id={running_batch.pk}{current_parser}. Nightly run skipped."
                )
            )
            return

        enabled_configs = list(
            ParserConfig.objects.filter(is_enabled=True, code__in=ADAPTERS.keys())
            .select_related("shop")
            .order_by("run_order", "name")
        )
        if not enabled_configs:
            raise CommandError("No enabled production parsers are registered for nightly run.")

        self.stdout.write(f"Enabled parsers: {len(enabled_configs)}")
        for config in enabled_configs:
            self.stdout.write(f"Starting {config.code.upper()}...")

        try:
            batch = run_all_parsers(trigger=ParserRun.TRIGGER_SCHEDULE)
        except ParserBatchAlreadyRunning as exc:
            self.stdout.write(self.style.WARNING(f"{exc} Nightly run skipped."))
            return
        except Exception as exc:
            raise CommandError(f"Nightly parser batch failed: {exc}") from exc

        batch.refresh_from_db()
        self.stdout.write(f"ParserBatch ID: {batch.pk}; status={batch.status}")
        if batch.status == ParserBatch.STATUS_SUCCESS:
            self.stdout.write(self.style.SUCCESS("Nightly parser batch completed successfully."))
            return

        raise CommandError(f"Nightly parser batch completed with status={batch.status}.")
