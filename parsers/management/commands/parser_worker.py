import time

from django.core.management.base import BaseCommand

from parsers.services.batch_runner import process_next_queue_job


class Command(BaseCommand):
    help = "Process parser queue jobs in a standalone worker process."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--sleep", type=float, default=5.0)

    def handle(self, *args, **options):
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
