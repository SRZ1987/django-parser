from django.apps import AppConfig


class ParsersConfig(AppConfig):
    name = 'parsers'

    def ready(self):
        import parsers.checks  # noqa: F401
