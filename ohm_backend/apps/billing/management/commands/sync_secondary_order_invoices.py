from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from apps.billing.models import Invoice, PrimaryOrder, SecondaryOrder


class Command(BaseCommand):
    help = (
        "Resolve overlapping SecondaryOrder bookings, delete duplicate "
        "invoices, and sync invoice period/amount from SecondaryOrder."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without writing to the database.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print per-record detail while processing.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Also delete conflicting SecondaryOrders that have their own "
                "TernaryOrders attached (data loss). Without this flag those "
                "conflicts are only reported."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        verbose = options["verbose"]
        force = options["force"]

        self.stdout.write(self.style.NOTICE(
            f"Starting sync_secondary_order_invoices "
            f"(dry_run={dry_run}, force={force})"
        ))

        conflicts_deleted, conflicts_skipped = self._resolve_conflicting_bookings(
            dry_run=dry_run, verbose=verbose, force=force
        )
        duplicate_invoices_deleted = self._delete_duplicate_invoices(
            dry_run=dry_run, verbose=verbose
        )
        updated_count = self._sync_invoices(dry_run=dry_run, verbose=verbose)

        self.stdout.write(self.style.SUCCESS(
            "Done. "
            f"Conflicting bookings removed: {conflicts_deleted}, "
            f"skipped (has ternary data, use --force): {conflicts_skipped}, "
            f"duplicate invoices deleted: {duplicate_invoices_deleted}, "
            f"invoices updated: {updated_count}"
            f"{' (dry run — no changes written)' if dry_run else ''}"
        ))

    # ── Step 1: overlapping SecondaryOrder bookings ─────────────────────────
    def _resolve_conflicting_bookings(self, dry_run, verbose, force):
        deleted_count = 0
        skipped_count = 0

        orders_by_primary = defaultdict(list)
        for so in (
            SecondaryOrder.objects
            .select_related("primary_order")
            .annotate(ternary_count=Count("ternary_orders"))
            .order_by("start_datetime", "end_datetime")
        ):
            orders_by_primary[so.primary_order_id].append(so)

        for primary_id, orders in orders_by_primary.items():
            kept = None
            for candidate in orders:
                if kept is None:
                    kept = candidate
                    continue

                overlaps = (
                    candidate.start_datetime < kept.end_datetime
                    and kept.start_datetime < candidate.end_datetime
                )

                if not overlaps:
                    kept = candidate
                    continue

                # Decide which of {kept, candidate} to keep: prefer more
                # ternary line items, tie-break by earliest created_at.
                if candidate.ternary_count > kept.ternary_count or (
                    candidate.ternary_count == kept.ternary_count
                    and candidate.created_at < kept.created_at
                ):
                    to_delete, kept = kept, candidate
                else:
                    to_delete = candidate

                if to_delete.ternary_count > 0 and not force:
                    skipped_count += 1
                    if verbose:
                        self.stdout.write(self.style.WARNING(
                            f"PrimaryOrder {primary_id}: conflict between "
                            f"SecondaryOrder {kept.order_id or kept.id} "
                            f"[{kept.start_datetime} - {kept.end_datetime}] and "
                            f"{to_delete.order_id or to_delete.id} "
                            f"[{to_delete.start_datetime} - {to_delete.end_datetime}] "
                            f"— skipped, {to_delete.order_id or to_delete.id} has "
                            f"{to_delete.ternary_count} ternary order(s). Use --force "
                            f"to delete anyway."
                        ))
                    continue

                if verbose:
                    self.stdout.write(
                        f"PrimaryOrder {primary_id}: keeping SecondaryOrder "
                        f"{kept.order_id or kept.id} "
                        f"[{kept.start_datetime} - {kept.end_datetime}], deleting "
                        f"{to_delete.order_id or to_delete.id} "
                        f"[{to_delete.start_datetime} - {to_delete.end_datetime}]"
                        f"{' (had ternary data, --force)' if to_delete.ternary_count else ''}"
                    )

                if not dry_run:
                    with transaction.atomic():
                        to_delete.delete()

                deleted_count += 1

            if kept and kept.primary_order_id and not dry_run:
                with transaction.atomic():
                    kept.recalculate_subtotal()

        return deleted_count, skipped_count

    # ── Step 2: duplicate invoices per secondary_order ──────────────────────
    def _delete_duplicate_invoices(self, dry_run, verbose):
        deleted_count = 0

        duplicate_so_ids = (
            Invoice.objects
            .filter(secondary_order__isnull=False)
            .values("secondary_order_id")
            .annotate(cnt=Count("id"))
            .filter(cnt__gt=1)
            .values_list("secondary_order_id", flat=True)
        )

        for so_id in duplicate_so_ids:
            invoices = list(
                Invoice.objects
                .filter(secondary_order_id=so_id)
                .order_by("-updated_at", "-id")
            )
            keep, extras = invoices[0], invoices[1:]

            if verbose:
                self.stdout.write(
                    f"SecondaryOrder {so_id}: keeping invoice "
                    f"{keep.invoice_number or keep.id}, "
                    f"deleting {[i.invoice_number or i.id for i in extras]}"
                )

            if not dry_run:
                with transaction.atomic():
                    Invoice.objects.filter(
                        id__in=[i.id for i in extras]
                    ).delete()

            deleted_count += len(extras)

        return deleted_count

    # ── Step 3: sync invoice fields from SecondaryOrder ─────────────────────
    def _sync_invoices(self, dry_run, verbose):
        updated_count = 0

        invoices_qs = (
            Invoice.objects
            .select_related("secondary_order")
            .filter(secondary_order__isnull=False)
        )

        for inv in invoices_qs.iterator():
            so = inv.secondary_order

            new_start = so.start_datetime
            new_end = so.end_datetime
            new_subtotal = so.subtotal

            changed = (
                inv.period_start != new_start
                or inv.period_end != new_end
                or inv.subtotal != new_subtotal
            )

            if changed:
                if verbose:
                    self.stdout.write(
                        f"Invoice {inv.invoice_number or inv.id}: "
                        f"period_start {inv.period_start} -> {new_start}, "
                        f"period_end {inv.period_end} -> {new_end}, "
                        f"subtotal {inv.subtotal} -> {new_subtotal}"
                    )

                inv.period_start = new_start
                inv.period_end = new_end
                inv.subtotal = new_subtotal

                if not dry_run:
                    with transaction.atomic():
                        inv.save()

                updated_count += 1

        return updated_count
