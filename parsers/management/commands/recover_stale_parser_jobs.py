from django.core.management.base import BaseCommand

from parsers.services.recovery import recover_stale_parser_state


class Command(BaseCommand):
    help = "Mark stale parser runs, queue jobs, and batches as failed."

    def handle(self, *args, **options):
        result = recover_stale_parser_state()
        self.stdout.write(
            self.style.SUCCESS(
                f"Recovered stale parser state: runs={result.runs}, jobs={result.jobs}, batches={result.batches}"
            )
        )
