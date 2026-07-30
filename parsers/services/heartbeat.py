import threading

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone


class HeartbeatTicker:
    def __init__(self, targets=None, interval_seconds=None):
        self.targets = list(targets or [])
        self.interval_seconds = interval_seconds or settings.PARSER_HEARTBEAT_INTERVAL_SECONDS
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self.beat()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop()

    def add_target(self, model, pk, running_status):
        self.targets.append((model, pk, running_status))
        self.beat()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(self.interval_seconds, 1))
        close_old_connections()

    def beat(self):
        now = timezone.now()
        close_old_connections()
        try:
            for model, pk, running_status in list(self.targets):
                model.objects.filter(pk=pk, status=running_status).update(heartbeat_at=now)
        finally:
            close_old_connections()

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            self.beat()
