from django.core.management.base import BaseCommand
from django.db import transaction

from apps.billing.models import PrimaryOrder


class Command(BaseCommand):
    help = "Update or create secondary orders from each primary order's date range."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Commit changes (default: dry run).")
        parser.add_argument("--patient", type=int, help="Only rebuild this patient's secondary orders.")
        parser.add_argument("--order-id", type=int, help="Only rebuild for specific primary order id.")

    def handle(self, *args, **options):
        orders = PrimaryOrder.objects.select_related("package").all().order_by("patient_id", "id")
        if options.get("patient") is not None:
            orders = orders.filter(patient_id=options["patient"])
        if options.get("order_id") is not None:
            orders = orders.filter(id=options["order_id"])

        checked = 0
        with transaction.atomic():
            for order in orders:
                self.stdout.write(f"Checking primary #{order.pk} ({order.order_id}): {order.secondary_orders.count()} secondary orders")

                before = order.secondary_orders.count()
                if order.raw_dates:
                    order.generate_secondary_from_random_dates(order.raw_dates, prune=True)
                else:
                    order.generate_secondary_full_range_dates(prune=True)
                after = order.secondary_orders.count()
                checked += 1
                if before != after:
                    self.stdout.write(
                        self.style.WARNING(
                            f"patient:{order.patient_id} primary id:{order.pk}\t "
                            f"{before} -> {after}"
                        )
                    )

            if not options["apply"]:
                transaction.set_rollback(True)

        mode = "APPLIED" if options["apply"] else "DRY RUN (rolled back)"
        self.stdout.write(self.style.SUCCESS(f"[{mode}] {checked} primary order(s) checked."))
