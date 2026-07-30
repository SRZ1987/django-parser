from django.core.management.base import BaseCommand, CommandError

from parsers.models import ParserRun
from parsers.services.batch_runner import ParserBatchAlreadyRunning, run_all_parsers


class Command(BaseCommand):
    help = "Run all enabled Excel-based parsers sequentially."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        try:
            batch = run_all_parsers(trigger=ParserRun.TRIGGER_COMMAND, force=options["force"])
        except ParserBatchAlreadyRunning as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"ParserBatch ID: {batch.pk}; status={batch.status}"))
