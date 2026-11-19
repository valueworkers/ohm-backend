from django.core.management.base import BaseCommand
from django.db import transaction

from apps.billing.models import Invoice, SecondaryOrder


def handle_secondary_order_update(instance):
    def _update():
        if instance.primary_order_id:
            instance.primary_order.recalculate_total()
        Invoice.create_or_update_for_secondary(instance)

    transaction.on_commit(_update)


class Command(BaseCommand):
    help = "Trigger update logic for SecondaryOrders"

    def handle(self, *args, **kwargs):
        orders = SecondaryOrder.objects.filter(invoices__isnull=False)
        total_invoices = Invoice.objects.all()
        before_count = total_invoices.count()

        self.stdout.write(self.style.SUCCESS(f"Total Number of Secondary order : {orders.count()}"))
        self.stdout.write(self.style.SUCCESS(f"Total Number of Invoices {total_invoices.count()}"))

        for order in orders:
            handle_secondary_order_update(order)

        self.stdout.write(self.style.SUCCESS(f"Total Number of Invoices generated : {total_invoices.count()-before_count}"))
