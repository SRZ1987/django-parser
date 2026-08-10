from django.core.management.base import BaseCommand

from main.price_alerts import send_shopping_list_price_alerts


class Command(BaseCommand):
    help = "Check enabled shopping lists and send price change alerts."

    def handle(self, *args, **options):
        result = send_shopping_list_price_alerts(log_callback=self.stdout.write)
        self.stdout.write(
            self.style.SUCCESS(
                "Price alert check completed: "
                f"lists={result.lists_checked}, emails={result.emails_sent}, "
                f"changes={result.changes_found}, errors={result.errors_count}"
            )
        )
