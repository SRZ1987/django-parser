from django.core.management.base import BaseCommand, CommandError

from parsers.adapters.registry import ADAPTERS
from parsers.models import ParserConfig, ParserRun
from parsers.services.batch_runner import ParserBatchAlreadyRunning, enqueue_all_parsers
from parsers.services.recovery import recover_stale_parser_state


class Command(BaseCommand):
    help = "Run all enabled production parsers as a scheduled nightly batch."

    def handle(self, *args, **options):
        self.stdout.write("Nightly parser batch starting...")
        self.stdout.write("Recovering stale parser state...")
        recovered = recover_stale_parser_state()
        self.stdout.write(f"Recovered stale parser state: runs={recovered.runs}, jobs={recovered.jobs}, batches={recovered.batches}")

        enabled_configs = list(
            ParserConfig.objects.filter(is_enabled=True, code__in=ADAPTERS.keys())
            .select_related("shop")
            .order_by("run_order", "name")
        )
        if not enabled_configs:
            raise CommandError("No enabled production parsers are registered for nightly run.")

        self.stdout.write(f"Enabled parsers: {len(enabled_configs)}")
        try:
            job, created = enqueue_all_parsers(trigger=ParserRun.TRIGGER_SCHEDULE)
        except ParserBatchAlreadyRunning as exc:
            self.stdout.write(self.style.WARNING(f"{exc} Nightly run skipped."))
            return
        except Exception as exc:
            raise CommandError(f"Nightly parser batch could not be queued: {exc}") from exc

        self.stdout.write(f"ParserQueueJob ID: {job.pk}; status={job.status}")
        if created:
            self.stdout.write(self.style.SUCCESS("Nightly parser batch queued for parser-worker."))
            return
        self.stdout.write(self.style.WARNING("An all-parsers job is already pending or running. Nightly run skipped."))
