from django.conf import settings
from django.core.checks import Warning, register


@register()
def check_parser_heartbeat_settings(app_configs, **kwargs):
    warnings = []
    heartbeat_interval = settings.PARSER_HEARTBEAT_INTERVAL_SECONDS
    stale_settings = [
        ("PARSER_STALE_RUN_MINUTES", settings.PARSER_STALE_RUN_MINUTES),
        ("PARSER_STALE_JOB_MINUTES", settings.PARSER_STALE_JOB_MINUTES),
        ("PARSER_STALE_BATCH_MINUTES", settings.PARSER_STALE_BATCH_MINUTES),
    ]

    for setting_name, minutes in stale_settings:
        if minutes * 60 < heartbeat_interval:
            warnings.append(
                Warning(
                    f"{setting_name} is shorter than PARSER_HEARTBEAT_INTERVAL_SECONDS.",
                    hint="Increase the stale timeout or reduce the heartbeat interval to avoid false stale recovery.",
                    id="parsers.W001",
                )
            )
    return warnings
