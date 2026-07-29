from dataclasses import dataclass

from django.utils import timezone


@dataclass
class ParserResult:
    products_found: int = 0
    products_created: int = 0
    products_updated: int = 0
    prices_changed: int = 0
    errors_count: int = 0


class ParserError(Exception):
    pass


class ParserAlreadyRunningError(ParserError):
    pass


class BaseStoreParser:
    code = ""

    def __init__(self, parser_config, parser_run):
        self.parser_config = parser_config
        self.parser_run = parser_run

    def run(self):
        raise NotImplementedError

    def log(self, message):
        timestamp = timezone.now().strftime("%Y-%m-%d %H:%M:%S")
        self.append_log_line(f"[{timestamp}] {message}")

    def append_log_line(self, message):
        current_log = self.parser_run.log or ""
        separator = "\n" if current_log else ""
        self.parser_run.log = f"{current_log}{separator}{message}"
        self.parser_run.save(update_fields=["log"])

    def update_progress(
        self,
        *,
        products_found=None,
        products_created=None,
        products_updated=None,
        prices_changed=None,
        errors_count=None,
    ):
        update_fields = []
        values = {
            "products_found": products_found,
            "products_created": products_created,
            "products_updated": products_updated,
            "prices_changed": prices_changed,
            "errors_count": errors_count,
        }

        for field, value in values.items():
            if value is not None:
                setattr(self.parser_run, field, value)
                update_fields.append(field)

        if update_fields:
            self.parser_run.save(update_fields=update_fields)
