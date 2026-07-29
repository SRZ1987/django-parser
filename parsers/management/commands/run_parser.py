from django.core.management.base import BaseCommand, CommandError

from parsers.models import ParserRun
from parsers.services.base import ParserAlreadyRunningError, ParserError
from parsers.services.runner import run_parser


class Command(BaseCommand):
    help = "Run a store parser synchronously."

    def add_arguments(self, parser):
        parser.add_argument("code")
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        code = options["code"]
        force = options["force"]

        try:
            parser_run = run_parser(code, trigger=ParserRun.TRIGGER_COMMAND, force=force)
        except (ParserAlreadyRunningError, ParserError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"ParserRun ID: {parser_run.pk}")
        self.stdout.write(f"Status: {parser_run.status}")
        self.stdout.write(f"Products found: {parser_run.products_found}")
        self.stdout.write(f"Products created: {parser_run.products_created}")
        self.stdout.write(f"Products updated: {parser_run.products_updated}")
        self.stdout.write(f"Prices changed: {parser_run.prices_changed}")
        self.stdout.write(f"Errors count: {parser_run.errors_count}")

        if parser_run.status == ParserRun.STATUS_SUCCESS:
            self.stdout.write(self.style.SUCCESS("Parser finished successfully."))
            return

        raise CommandError(parser_run.error_message or "Parser finished with an error.")
