from __future__ import annotations

import re
import calendar
from datetime import date, time, datetime, timedelta
from typing import Optional
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.db.models import Prefetch, QuerySet, Q
from django.db import transaction

from .constants import BookingStatus, PeriodChoices
from .models import PrimaryOrder, SecondaryOrder, TernaryOrder, Payment, Invoice


class DateParser:
    """Centralized date parsing for DAILY and HOURLY packages."""

    @staticmethod
    def parse_dates(period_type: str, raw_dates):
        if period_type == PeriodChoices.DAILY:
            return DateParser._parse_daily(raw_dates)
        elif period_type == PeriodChoices.HOURLY:
            return DateParser._parse_hourly(raw_dates)
        else:
            raise ValidationError(
                {"package": f"Unsupported period type: {period_type}"}
            )

    @staticmethod
    def _parse_daily(raw_dates) -> list:
        if not isinstance(raw_dates, list):
            raise ValidationError(
                {"dates": "For DAILY package, 'dates' must be a list of ISO date strings."}
            )
        try:
            return [date.fromisoformat(d) for d in raw_dates]
        except ValueError:
            raise ValidationError(
                {"dates": "Invalid date format. Use YYYY-MM-DD."}
            )

    @staticmethod
    def _parse_hourly(raw_dates) -> dict:
        if not isinstance(raw_dates, dict):
            raise ValidationError(
                {"dates": "For HOURLY package, 'dates' must be a dictionary."}
            )
        try:
            return {
                date.fromisoformat(d): [time.fromisoformat(t) for t in slots]
                for d, slots in raw_dates.items()
            }
        except ValueError:
            raise ValidationError(
                {"dates": "Invalid date or time format. Use YYYY-MM-DD and HH:MM:SS."}
            )

    @staticmethod
    def extract_datetime_bounds(period_type: str, parsed_dates) -> tuple:
        if period_type == PeriodChoices.DAILY:
            min_date = min(parsed_dates)
            max_date = max(parsed_dates)
            start_dt = timezone.make_aware(datetime.combine(min_date, time.min))
            end_dt = timezone.make_aware(datetime.combine(max_date, time.max))
        elif period_type == PeriodChoices.HOURLY:
            all_dates = list(parsed_dates.keys())
            min_date = min(all_dates)
            max_date = max(all_dates)
            start_dt = timezone.make_aware(datetime.combine(min_date, time.min))
            end_dt = timezone.make_aware(datetime.combine(max_date, time.max))
        else:
            raise ValueError(f"Unsupported period type: {period_type}")
        return start_dt, end_dt


class OrderQuerySet(QuerySet):
    """Custom QuerySet for PrimaryOrder."""

    def with_related(self):
        return self.select_related(
            "patient", "venue", "service", "package", "user"
        ).prefetch_related("secondary_orders__ternary_orders")

    def exclude_status(self, statuses: list):
        return self.exclude(status__in=statuses)

    def customer_visible(self, user):
        if getattr(user, "is_customer", False):
            return self.filter(
                Q(patient__registered_by=user) | Q(user=user)
            )
        return self

    def filter_by_status(self, statuses: list):
        return self.filter(status__in=statuses)

    def active(self):
        return self.exclude_status([BookingStatus.LOBBY, BookingStatus.HOLD])

    def pending(self):
        return self.filter_by_status([BookingStatus.LOBBY, BookingStatus.HOLD])

    def by_timeframe(self, timeframe: str):
        now = timezone.now()
        if timeframe == "ongoing":
            return self.filter(start_datetime__lte=now, end_datetime__gte=now)
        elif timeframe == "upcoming":
            return self.filter(start_datetime__gt=now)
        elif timeframe == "past":
            return self.filter(end_datetime__lt=now)
        return self


class SecondaryOrderHelper:
    """Helper methods for SecondaryOrder operations."""

    @staticmethod
    def get_matching_secondary_order(
        primary_order: PrimaryOrder,
        start_dt: datetime,
        end_dt: datetime,
    ) -> SecondaryOrder:
        try:
            return primary_order.secondary_orders.get(
                start_datetime__lte=start_dt,
                end_datetime__gte=end_dt,
            )
        except SecondaryOrder.DoesNotExist:
            raise SecondaryOrder.DoesNotExist(
                f"No secondary order found for {start_dt.year}-{start_dt.month:02d}. "
                "Ensure the service date falls within the booking range."
            )

    @staticmethod
    def cascade_status_change(
        primary_order: PrimaryOrder,
        new_status: str,
        specific_secondary_id: int = None,
        specific_ternary_id: int = None,
    ) -> None:
        secondary_ids = primary_order.secondary_orders.values_list("id", flat=True)

        if new_status == BookingStatus.CANCELLED and not specific_secondary_id and not specific_ternary_id:
            SecondaryOrder.objects.filter(id__in=secondary_ids).update(status=new_status)
            TernaryOrder.objects.filter(secondary_order_id__in=secondary_ids).update(status=new_status)
        elif specific_ternary_id:
            pass
        elif specific_secondary_id:
            TernaryOrder.objects.filter(
                secondary_order_id=specific_secondary_id
            ).update(status=new_status)
        else:
            SecondaryOrder.objects.filter(id__in=secondary_ids).update(status=new_status)
            TernaryOrder.objects.filter(secondary_order_id__in=secondary_ids).update(status=new_status)


class MonthAvailabilityChecker:
    """Checks daily availability and existing bookings for a patient across a month."""

    def __init__(
        self,
        patient_id: int,
        month: int,
        year: int,
        exclude_order_id: Optional[int] = None,
    ):
        self.patient_id = patient_id
        self.month = month
        self.year = year
        self.exclude_order_id = exclude_order_id
        self.last_day = calendar.monthrange(year, month)[1]
        self.month_start = datetime(year, month, 1, 0, 0, 0)
        self.month_end = datetime(year, month, self.last_day, 23, 59, 59)

    def check(self) -> dict:
        qs = self._build_queryset()

        month_first = date(self.year, self.month, 1)
        month_last = date(self.year, self.month, self.last_day)
        date_bookings: dict[date, list[dict]] = {}

        for sec in qs:
            po = sec.primary_order
            ternary_orders = [
                {
                    "ternary_order_id": t.pk,
                    "order_id": t.order_id,
                    "start_datetime": t.start_datetime,
                    "end_datetime": t.end_datetime,
                    "status": t.status,
                    "service_name": t.service.name if t.service else "—",
                    "package_name": t.package.name if t.package else "—",
                    "venue_name": t.venue.name if t.venue else "—",
                }
                for t in sec.ternary_orders.all()
            ]

            booking = {
                "secondary_order_id": sec.pk,
                "order_id": sec.order_id,
                "start_datetime": sec.start_datetime,
                "end_datetime": sec.end_datetime,
                "status": sec.status,
                "service_id": po.service_id,
                "service_name": po.service.name if po.service else "—",
                "package_name": po.package.name if po.package else "—",
                "primary_order_id": po.order_id,
                "booking_type": po.booking_type,
                "ternary_orders": ternary_orders,
            }

            cursor = max(sec.start_datetime.date(), month_first)
            end = min(sec.end_datetime.date(), month_last)
            while cursor <= end:
                date_bookings.setdefault(cursor, []).append(booking)
                cursor += timedelta(days=1)

        today = timezone.now().date()
        calendar_rows = []
        past_days = 0
        occupied_days = 0

        for day in range(1, self.last_day + 1):
            d = date(self.year, self.month, day)
            is_past = d < today
            bookings = date_bookings.get(d, [])
            is_avail = not bookings

            if is_past:
                past_days += 1
            if not is_avail:
                occupied_days += 1

            calendar_rows.append({
                "date": d,
                "is_available": is_avail,
                "is_past": is_past,
                "bookings": bookings,
            })

        return {
            "patient_id": self.patient_id,
            "month": self.month,
            "year": self.year,
            "month_label": date(self.year, self.month, 1).strftime("%B %Y"),
            "total_days": self.last_day,
            "available_days": self.last_day - occupied_days,
            "occupied_days": occupied_days,
            "past_days": past_days,
            "calendar": calendar_rows,
        }

    def _build_queryset(self):
        active_ternaries = Prefetch(
            "ternary_orders",
            queryset=TernaryOrder.objects.exclude(status=BookingStatus.CANCELLED)
            .select_related("service", "venue", "package")
            .order_by("start_datetime"),
        )

        qs = (
            SecondaryOrder.objects.filter(
                primary_order__patient_id=self.patient_id,
                start_datetime__lt=self.month_end,
                end_datetime__gt=self.month_start,
            )
            .exclude(primary_order__status=BookingStatus.CANCELLED)
            .exclude(status=BookingStatus.CANCELLED)
            .select_related(
                "primary_order",
                "primary_order__service",
                "primary_order__package",
                "primary_order__venue",
            )
            .prefetch_related(active_ternaries)
            .order_by("start_datetime")
        )

        if self.exclude_order_id:
            qs = qs.exclude(primary_order_id=self.exclude_order_id)

        return qs


class PaymentMappingService:
    AUTO_MAP_THRESHOLD = Decimal("90.00")
    REVIEW_THRESHOLD = Decimal("65.00")
    AMBIGUITY_MARGIN = Decimal("10.00")

    def __init__(self, payment):
        self.payment = payment

    def run(self, dry_run: bool = False):
        if self.payment.invoice_id:
            return {"status": "ALREADY_MAPPED", "payment_id": self.payment.id, "invoice_id": self.payment.invoice_id}

        candidates = self._find_candidates()
        scored = [r for r in (self._score_invoice(inv) for inv in candidates) if r["score"] > 0]
        scored.sort(key=lambda x: x["score"], reverse=True)

        if not scored:
            return {"status": "UNMAPPED", "payment_id": self.payment.id, "dry_run": True} if dry_run else self._mark_unmapped()

        best = scored[0]
        runner_up = scored[1] if len(scored) > 1 else None

        if best["score"] >= self.AUTO_MAP_THRESHOLD:
            if runner_up and (best["score"] - runner_up["score"]) < self.AMBIGUITY_MARGIN:
                return {"status": "REVIEW", "payment_id": self.payment.id, "dry_run": True} if dry_run else self._mark_review(best, scored)
            return {
                "status": "AUTO_MAPPED",
                "payment_id": self.payment.id,
                "invoice_id": best["invoice"].id,
                "confidence": float(best["score"]),
                "reason": best["reason"],
                "dry_run": True,
            } if dry_run else self._auto_map(best)

        if best["score"] >= self.REVIEW_THRESHOLD:
            return {"status": "REVIEW", "payment_id": self.payment.id, "dry_run": True} if dry_run else self._mark_review(best, scored)

        return {"status": "UNMAPPED", "payment_id": self.payment.id, "dry_run": True} if dry_run else self._mark_unmapped()

    def _find_candidates(self):
        return Invoice.objects.filter(
            status__in=["ISSUED", "PARTIALLY_PAID", "OVERDUE", "UNPAID"]
        ).select_related("patient", "user")

    def _score_invoice(self, invoice):
        score = Decimal("0.0")
        reason = {}

        if self._reference_contains_invoice(self.payment.reference, invoice.invoice_number):
            score += Decimal("80.0")
            reason["reference_invoice_match"] = True

        if invoice.patient and self._patient_from_reference(self.payment.reference, invoice.patient):
            score += Decimal("40.0")
            reason["patient_match"] = True

        if invoice.remaining_amount == self.payment.amount:
            score += Decimal("30.0")
            reason["amount_match"] = True
        elif invoice.remaining_amount >= self.payment.amount:
            score += Decimal("10.0")
            reason["amount_fits_remaining"] = True

        return {
            "invoice": invoice,
            "score": min(score, Decimal("100.0")),
            "reason": reason,
        }

    @transaction.atomic
    def _auto_map(self, result):
        payment = Payment.objects.select_for_update().select_related("invoice").get(pk=self.payment.pk)
        if payment.invoice_id:
            return {"status": "ALREADY_MAPPED", "payment_id": payment.id, "invoice_id": payment.invoice_id}

        invoice = Invoice.objects.select_for_update().get(pk=result["invoice"].pk)
        invoice.recalculate_payments()
        if invoice.remaining_amount < payment.amount:
            stale_candidate = {
                **result,
                "reason": {
                    **result["reason"],
                    "amount_fits_remaining": False,
                    "balance_changed_during_mapping": True,
                },
            }
            return self._mark_review(stale_candidate, [stale_candidate])

        patient = invoice.patient
        payment.invoice = invoice
        payment.patient = patient
        payment.mapping_status = "AUTO_MAPPED"
        payment.mapping_meta = {
            "source": self._get_source(result["reason"]),
            "confidence": float(result["score"]),
            "reason": result["reason"],
        }
        payment.save(update_fields=["invoice", "patient", "mapping_status", "mapping_meta", "updated_at"])
        invoice.recalculate_payments()

        return {
            "status": "AUTO_MAPPED",
            "payment_id": payment.id,
            "invoice_id": invoice.id,
            "patient_id": patient.id if patient else None,
            "confidence": float(result["score"]),
            "reason": result["reason"],
        }

    def _mark_review(self, best, candidates):
        mapping_reason = {
            "best_candidate": {
                "invoice_id": best["invoice"].id,
                "invoice_number": best["invoice"].invoice_number,
                "score": float(best["score"]),
                "reason": best["reason"],
            },
            "other_candidates": [
                {
                    "invoice_id": x["invoice"].id,
                    "invoice_number": x["invoice"].invoice_number,
                    "score": float(x["score"]),
                    "reason": x["reason"],
                }
                for x in candidates[1:5]
            ],
        }
        self.payment.mapping_status = "REVIEW"
        self.payment.mapping_meta = mapping_reason
        self.payment.save(update_fields=["mapping_status", "mapping_meta", "updated_at"])
        return {
            "status": "REVIEW",
            "payment_id": self.payment.id,
            "source": self._get_source(best["reason"]),
            "confidence": float(best["score"]),
            "reason": mapping_reason,
        }

    def _mark_unmapped(self):
        self.payment.mapping_status = "UNMAPPED"
        self.payment.save(update_fields=["mapping_status", "updated_at"])
        return {"status": "UNMAPPED", "payment_id": self.payment.id}

    @staticmethod
    def _normalize(value):
        if not value:
            return ""
        return re.sub(r"[^A-Z0-9]", "", str(value).upper())

    def _reference_contains_invoice(self, reference, invoice_number):
        ref = self._normalize(reference)
        inv = self._normalize(invoice_number)
        return bool(ref and inv and inv in ref)

    def _patient_from_reference(self, reference, patient):
        if not reference or not patient:
            return False
        normalized_reference = self._normalize(reference)
        if hasattr(patient, "patient_id") and patient.patient_id and self._normalize(patient.patient_id) in normalized_reference:
            return True
        if hasattr(patient, "phone") and patient.phone and patient.phone in normalized_reference:
            return True
        first = getattr(patient, "first_name", "") or ""
        last = getattr(patient, "last_name", "") or ""
        full_name = self._normalize(f"{first}{last}")
        if full_name and full_name in normalized_reference:
            return True
        return False

    @staticmethod
    def _get_source(reason):
        if reason.get("reference_invoice_match"):
            return "REFERENCE_INVOICE"
        if reason.get("patient_match") and reason.get("amount_match"):
            return "PATIENT_AMOUNT"
        if reason.get("amount_match"):
            return "AMOUNT_DATE"
        return "AUTO"
