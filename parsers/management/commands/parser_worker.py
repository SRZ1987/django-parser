import time

from django.core.management.base import BaseCommand

from parsers.services.batch_runner import process_next_queue_job
from parsers.services.recovery import recover_stale_parser_state


class Command(BaseCommand):
    help = "Process parser queue jobs in a standalone worker process."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--sleep", type=float, default=5.0)

    def handle(self, *args, **options):
        result = recover_stale_parser_state()
        self.stdout.write(f"Recovered stale parser state: runs={result.runs}, jobs={result.jobs}, batches={result.batches}")
        while True:
            job = process_next_queue_job()
            if job:
                self.stdout.write(f"Processed ParserQueueJob ID: {job.pk}; status={job.status}")
            elif options["once"]:
                self.stdout.write("No pending parser jobs.")
                return
            if options["once"]:
                return
            time.sleep(options["sleep"])
