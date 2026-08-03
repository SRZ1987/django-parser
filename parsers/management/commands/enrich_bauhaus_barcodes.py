from django.core.management.base import BaseCommand, CommandError

from catalog.models import ProductOffer
from parsers.services.bauhaus_barcode_enricher import enrich_bauhaus_offer_barcodes


class Command(BaseCommand):
    help = "Fill missing BAUHAUS barcodes from product pages."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int)
        parser.add_argument("--offer-id", type=int)
        parser.add_argument("--retry-missing", action="store_true")
        parser.add_argument("--concurrency", type=int)
        parser.add_argument(
            "--retune",
            action="store_true",
            help="Ignore the saved limit and start from maximum concurrency.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit is not None and limit <= 0:
            raise CommandError("--limit must be greater than zero.")
        if options["concurrency"] is not None and options["concurrency"] <= 0:
            raise CommandError("--concurrency must be greater than zero.")

        offers = ProductOffer.objects.filter(shop__code="bauhaus", barcode="")
        if options["offer_id"] is not None:
            offers = offers.filter(pk=options["offer_id"])
        if not options["retry_missing"]:
            offers = offers.filter(barcode_checked_at__isnull=True)

        offer_ids = offers.order_by("pk").values_list("pk", flat=True)
        if limit is not None:
            offer_ids = offer_ids[:limit]

        result = enrich_bauhaus_offer_barcodes(
            list(offer_ids),
            retry_missing=options["retry_missing"],
            log_callback=self.stdout.write,
            concurrency=options["concurrency"],
            retune=options["retune"],
        )
        self.stdout.write(self.style.SUCCESS(result.summary()))
