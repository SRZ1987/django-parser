from pathlib import Path

from django.conf import settings
from django.utils import timezone


def export_work_paths(parser_code):
    settings.PARSER_EXPORT_WORK_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = timezone.localtime(timezone.now()).strftime("%Y-%m-%d_%H-%M-%S")
    stem = f"{parser_code}_{timestamp}"
    tmp_path = Path(settings.PARSER_EXPORT_WORK_DIR) / f"{stem}.tmp.xlsx"
    final_path = Path(settings.PARSER_EXPORT_WORK_DIR) / f"{stem}.xlsx"
    return tmp_path, final_path
