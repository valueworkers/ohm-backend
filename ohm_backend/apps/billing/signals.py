"""
apps/billing/signals.py
───────────────────────
Post-save / post-delete receivers that keep Invoice and PrimaryOrder
totals in sync whenever a Payment or SecondaryOrder is modified.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Invoice, Payment, SecondaryOrder, TernaryOrder


# ---------------------------------------------------------------------------
# Invoice ← Payment
# ---------------------------------------------------------------------------

@receiver(post_save, sender=Payment)
@receiver(post_delete, sender=Payment)
def sync_invoice_on_payment_change(sender, instance, **kwargs):
    """Recompute Invoice totals whenever a Payment row is saved or deleted."""
    if instance.invoice_id:
        try:
            instance.invoice.recalculate_payments()
        except Invoice.DoesNotExist:
            pass


# ---------------------------------------------------------------------------
# PrimaryOrder ← SecondaryOrder
# ---------------------------------------------------------------------------

@receiver(post_save, sender=SecondaryOrder)
@receiver(post_delete, sender=SecondaryOrder)
def sync_primary_on_secondary_change(sender, instance, **kwargs):
    """Roll SecondaryOrder changes up to the parent PrimaryOrder."""
    if instance.primary_order_id:
        try:
            instance.primary_order.refresh_from_secondaries()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Invoice ← TernaryOrder (create / update invoice on add-on save)
# ---------------------------------------------------------------------------

@receiver(post_save, sender=TernaryOrder)
def create_or_update_invoice_for_ternary(sender, instance, created, **kwargs):
    """Keep the ternary Invoice in sync with the TernaryOrder."""
    # Only trigger on a full save (not targeted update_fields saves)
    if kwargs.get("update_fields"):
        return
    Invoice.create_or_update_for_ternary(instance)
