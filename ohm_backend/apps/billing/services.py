"""
apps/billing/services.py
─────────────────────────
Business logic for quotes, invoices, GST calculations, and email delivery.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from secrets import token_urlsafe

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import format_html_join

from apps.platform import gst
from .constants import (
    InvoiceStatus,
    PaymentMethod,
    PAYMENT_METHOD_NORMALIZE,
    QuoteLineKind,
    QuoteStatus,
    QuoteSupplyType,
    Uom,
)
from .models import (
    DocumentSequence,
    Invoice,
    InvoiceItem,
    InvoiceSequence,
    Payment,
    Quote,
    QuoteCounter,
    QuoteEvent,
    QuoteLine,
    QuoteTaxSettings,
)

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_LINES = 50


class BillingError(Exception):
    pass


def _money(value) -> Decimal:
    if value is None:
        return ZERO
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def default_issuer() -> dict:
    return getattr(
        settings,
        "INVOICE_DEFAULT_ISSUER",
        {
            "logo": "",
            "company_name": "Healthcare & Wellness Services",
            "company_address": "India",
            "company_phone": "",
            "gstin": "",
            "state": "Karnataka",
            "contact_name": "Billing Dept",
            "contact_phone": "",
            "contact_email": getattr(settings, "DEFAULT_FROM_EMAIL", "billing@example.com"),
        },
    )


def default_terms() -> str:
    return getattr(
        settings,
        "INVOICE_DEFAULT_TERMS",
        "1. Payment due as agreed on invoice issue date.\n"
        "2. Service delivery subject to availability.\n"
        "3. Applicable taxes charged per government regulations.\n"
        "4. Subject to local jurisdiction.",
    )


def default_due_days() -> int:
    return getattr(settings, "INVOICE_DEFAULT_DUE_DAYS", 15)


def get_seller_tax_settings() -> tuple[str, str, str]:
    """Return (state_code, state_name, gstin) from QuoteTaxSettings or settings."""
    tax_cfg = QuoteTaxSettings.objects.first()
    if tax_cfg and tax_cfg.state_code:
        return tax_cfg.state_code, tax_cfg.state_name, tax_cfg.gstin
    issuer = default_issuer()
    state_name = issuer.get("state", "Karnataka")
    state_code = gst.resolve_state_code(issuer.get("gstin", ""), state_name) or "29"
    return state_code, state_name, issuer.get("gstin", "")


def next_invoice_number(issue_date=None, *, lock: bool = True) -> str:
    """Gap-free invoice numbering per year (e.g. 101/2026)."""
    issue_date = issue_date or timezone.localdate()
    year = issue_date.year
    with transaction.atomic():
        if lock:
            seq, _ = InvoiceSequence.objects.select_for_update().get_or_create(year=year)
            seq.last_number += 1
            seq.save(update_fields=["last_number"])
            number = seq.last_number
        else:
            seq = InvoiceSequence.objects.filter(year=year).first()
            number = (seq.last_number + 1) if seq else 1
    return f"{number:04d}/{year}"


def next_quote_number(period=None) -> str:
    """Generate the next quote number in Q-YYMM-XXXX format."""
    period = period or timezone.localdate().strftime("%y%m")
    with transaction.atomic():
        counter, _ = QuoteCounter.objects.select_for_update().get_or_create(period=period)
        counter.last_seq += 1
        counter.save(update_fields=["last_seq"])
        seq_num = counter.last_seq
    return f"Q-{period}-{seq_num:04d}"


# ---------------------------------------------------------------------------
# Quote Calculations and Actions
# ---------------------------------------------------------------------------

def recalculate_quote(quote: Quote) -> Quote:
    """Recalculate GST line items and totals for a Quote."""
    seller_code, seller_name, seller_gstin = get_seller_tax_settings()
    quote.seller_state_code = seller_code
    quote.seller_state_name = seller_name
    quote.seller_gstin = seller_gstin

    buyer_code = gst.resolve_state_code(quote.gstin, quote.state)
    quote.state_code = buyer_code or ""
    quote.place_of_supply_code = buyer_code or seller_code

    stype, _ = gst.determine_supply_type(seller_code, quote.place_of_supply_code)
    quote.supply_type = stype

    lines = list(quote.lines.all())
    subtotal = ZERO
    discount_total = ZERO
    taxable_sum = ZERO
    cgst_sum = ZERO
    sgst_sum = ZERO
    igst_sum = ZERO

    for line in lines:
        qty = Decimal(str(line.qty or 1))
        unit_price = Decimal(str(line.unit_price or 0)) if line.unit_price is not None else ZERO
        disc_pct = Decimal(str(line.discount_percent or 0))
        gst_rate = Decimal(str(line.gst_rate or 0))

        gross = _money(qty * unit_price)
        disc_amt = _money(gross * (disc_pct / Decimal("100")))
        taxable = max(gross - disc_amt, ZERO)

        subtotal += gross
        discount_total += disc_amt
        taxable_sum += taxable

        tax_breakdown = gst.compute_tax(taxable, gst_rate, stype)
        line.taxable_value = taxable
        line.cgst_amount = tax_breakdown["cgst"]
        line.sgst_amount = tax_breakdown["sgst"]
        line.igst_amount = tax_breakdown["igst"]
        line.line_total = taxable + line.cgst_amount + line.sgst_amount + line.igst_amount
        line.save()

        cgst_sum += line.cgst_amount
        sgst_sum += line.sgst_amount
        igst_sum += line.igst_amount

    # Charges
    freight = Decimal(str(quote.freight or 0))
    packing = Decimal(str(quote.packing or 0))
    charges = freight + packing
    charges_gst_rate = Decimal(str(quote.charges_gst_rate or 18))
    charges_tax = gst.compute_tax(charges, charges_gst_rate, stype)

    quote.subtotal = subtotal
    quote.discount_total = discount_total
    quote.taxable_amount = taxable_sum + charges
    quote.cgst = cgst_sum + charges_tax["cgst"]
    quote.sgst = sgst_sum + charges_tax["sgst"]
    quote.igst = igst_sum + charges_tax["igst"]

    raw_total = quote.taxable_amount + quote.cgst + quote.sgst + quote.igst
    rounded = raw_total.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    quote.round_off = rounded - raw_total
    quote.total = rounded
    quote.save()
    return quote


@transaction.atomic
def create_quote(
    *,
    company: str,
    buyer_email: str,
    state: str,
    lines: list[dict],
    user=None,
    patient=None,
    venue=None,
    service=None,
    resource=None,
    booking_entity=None,
    **kwargs,
) -> Quote:
    """Create a new Draft Quote with its lines and initial event."""
    number = kwargs.pop("number", None) or next_quote_number()
    quote = Quote.objects.create(
        number=number,
        company=company,
        buyer_email=buyer_email,
        state=state,
        patient=patient,
        venue=venue,
        service=service,
        resource=resource,
        booking_entity=booking_entity,
        created_by=user,
        created_by_email=user.email if user else "",
        **kwargs,
    )
    for idx, item in enumerate(lines, start=1):
        QuoteLine.objects.create(quote=quote, position=idx, **item)
    recalculate_quote(quote)
    QuoteEvent.objects.create(
        quote=quote,
        kind="created",
        actor=user,
        detail=f"Quote created with {len(lines)} line(s)",
    )
    return quote


@transaction.atomic
def send_quote(quote: Quote, user=None, *, test_recipients=None, response_url=None) -> Quote:
    """Send a Quote to the buyer by email."""
    quote = Quote.objects.select_for_update().get(pk=quote.pk)
    if quote.status != QuoteStatus.DRAFT:
        raise ValueError("Only draft quotes can be sent.")
    recalculate_quote(quote)

    recipients = test_recipients or [quote.buyer_email]
    subject = f"{'[TEST] ' if test_recipients else ''}Quote {quote.number} R{quote.revision}"
    money_fmt = lambda v: f"₹{v:,.2f}"

    item_rows = [
        {
            "serial": l.position,
            "name": l.item,
            "description": l.specification or "",
            "hsn_sac": l.hsn_sac or "—",
            "uom": l.get_uom_display(),
            "qty": l.qty,
            "unit_price": "On request" if l.unit_price is None else money_fmt(l.unit_price),
            "discount_percent": l.discount_percent,
            "gst_rate": l.gst_rate,
            "taxable": money_fmt(l.taxable_value),
            "amount": money_fmt(l.line_total),
        }
        for l in quote.lines.all()
    ]

    tax_rows = []
    if quote.supply_type == QuoteSupplyType.INTER_STATE:
        tax_rows.append(("IGST", money_fmt(quote.igst)))
    else:
        tax_rows.append(("CGST", money_fmt(quote.cgst)))
        tax_rows.append((quote.state_tax_label, money_fmt(quote.sgst)))

    context = {
        "quote_number": quote.number,
        "revision": f"R{quote.revision}",
        "quote_date": quote.quote_date.strftime("%d %b %Y"),
        "valid_until": quote.valid_until.strftime("%d %b %Y"),
        "customer_name": quote.company,
        "customer_attention": quote.attention,
        "customer_address": quote.address,
        "gstin": quote.gstin or "—",
        "seller_gstin": quote.seller_gstin or "—",
        "customer_region": f"{quote.state} ({quote.state_code})" if quote.state_code else quote.state,
        "items": item_rows,
        "subtotal": money_fmt(quote.subtotal),
        "discount_total": money_fmt(quote.discount_total),
        "taxable_amount": money_fmt(quote.taxable_amount),
        "tax_rows": tax_rows,
        "total": money_fmt(quote.total or 0),
        "notes": quote.notes,
        "terms": quote.terms,
        "accept_url": response_url or "",
    }

    body = (
        f"Please find quote {quote.number} revision {quote.revision}.\n"
        f"Total: ₹{quote.total or 0:,.2f}\n"
        f"Valid until: {quote.valid_until.strftime('%d %b %Y')}\n"
    )

    email = EmailMultiAlternatives(
        subject,
        body,
        settings.DEFAULT_FROM_EMAIL,
        recipients,
        cc=None if test_recipients else (quote.cc_emails or None),
        bcc=None if test_recipients else (quote.bcc_emails or None),
    )
    try:
        html_content = render_to_string("billing/quote-email.html", context)
        email.attach_alternative(html_content, "text/html")
    except Exception:
        pass

    email.send(fail_silently=False)

    if test_recipients:
        QuoteEvent.objects.create(
            quote=quote,
            kind="test_sent",
            detail=", ".join(recipients)[:300],
            actor=user,
        )
        return quote

    quote.status = QuoteStatus.SENT
    quote.sent_at = timezone.now()
    quote.save(update_fields=["status", "sent_at"])
    QuoteEvent.objects.create(
        quote=quote,
        kind="sent",
        detail=quote.buyer_email,
        actor=user,
    )
    return quote


@transaction.atomic
def revise_quote(quote: Quote, user=None) -> Quote:
    """Duplicate a SENT quote into a new DRAFT revision."""
    if quote.status != QuoteStatus.SENT:
        raise ValueError("Only sent quotes can be revised.")

    lines = list(quote.lines.all())
    quote.pk = None
    quote._state.adding = True
    quote.revision += 1
    quote.status = QuoteStatus.DRAFT
    quote.sent_at = None
    quote.accepted_at = None
    quote.accept_token = token_urlsafe(32)
    quote.quote_date = timezone.localdate()
    quote.created_by = user
    quote.created_by_email = user.email if user else ""
    quote.save()

    for line in lines:
        line.pk = None
        line._state.adding = True
        line.quote = quote
        line.save()

    recalculate_quote(quote)
    QuoteEvent.objects.create(
        quote=quote,
        kind="revised",
        actor=user,
        detail=f"Revision R{quote.revision} created",
    )
    return quote


@transaction.atomic
def respond_to_quote(token: str, action: str) -> Quote:
    """Handle accept / reject from client link."""
    quote = Quote.objects.select_for_update().get(accept_token=token)
    if quote.status != QuoteStatus.SENT:
        raise ValueError("Only sent quotes can be accepted or rejected.")
    if quote.valid_until < timezone.localdate():
        quote.status = QuoteStatus.EXPIRED
        quote.save(update_fields=["status"])
        QuoteEvent.objects.create(quote=quote, kind="expired", detail="Validity period passed")
        return quote

    if action == "accept":
        quote.status = QuoteStatus.ACCEPTED
        quote.accepted_at = timezone.now()
        quote.save(update_fields=["status", "accepted_at"])
        QuoteEvent.objects.create(quote=quote, kind="accepted", detail="Accepted by customer")
    elif action == "reject":
        quote.status = QuoteStatus.REJECTED
        quote.save(update_fields=["status"])
        QuoteEvent.objects.create(quote=quote, kind="rejected", detail="Rejected by customer")
    else:
        raise ValueError(f"Unknown quote action: {action}")
    return quote


# ---------------------------------------------------------------------------
# Invoice Services
# ---------------------------------------------------------------------------

@transaction.atomic
def save_invoice(invoice: Invoice, items_data: list[dict] | None = None, *, raise_now: bool = False) -> Invoice:
    """Save an invoice and calculate its itemized GST amounts."""
    if invoice.pk and invoice.status not in {InvoiceStatus.DRAFT, InvoiceStatus.ISSUED}:
        raise BillingError("Only draft or newly issued invoices can be updated.")

    if not invoice.issuer:
        invoice.issuer = default_issuer()

    seller_code, _, seller_gstin = get_seller_tax_settings()
    if seller_gstin:
        invoice.issuer = {**invoice.issuer, "gstin": seller_gstin}

    buyer_code = gst.resolve_state_code(invoice.gstin, invoice.state)
    stype, _ = gst.determine_supply_type(seller_code, buyer_code or seller_code)
    invoice.state_code = buyer_code or ""
    invoice.is_interstate = (stype == gst.INTER_STATE)

    if not invoice.terms:
        invoice.terms = default_terms()
    invoice.due_days = invoice.due_days or default_due_days()
    invoice.due_date = invoice.issue_date + timedelta(days=invoice.due_days)

    invoice.save()

    if items_data is not None:
        invoice.items.all().delete()
        new_items = []
        for idx, item in enumerate(items_data, start=1):
            qty = Decimal(str(item.get("quantity", 1)))
            rate = Decimal(str(item.get("unit_price", 0)))
            disc_pct = Decimal(str(item.get("discount_percent", 0)))
            gst_rate = Decimal(str(item.get("gst_rate", 18)))

            gross = _money(qty * rate)
            disc_amt = _money(gross * (disc_pct / Decimal("100")))
            taxable = max(gross - disc_amt, ZERO)
            tax_dict = gst.compute_tax(taxable, gst_rate, stype)

            new_items.append(
                InvoiceItem(
                    invoice=invoice,
                    position=idx,
                    line_type=item.get("line_type", "custom_service"),
                    description=item.get("description", "Item"),
                    hsn_sac=item.get("hsn_sac", ""),
                    specification=item.get("specification", ""),
                    quantity=qty,
                    uom=item.get("uom", Uom.PCS),
                    unit_price=rate,
                    discount_percent=disc_pct,
                    lead_time=item.get("lead_time", ""),
                    gst_rate=gst_rate,
                    discount_amount=disc_amt,
                    taxable_value=taxable,
                    cgst_amount=tax_dict["cgst"],
                    sgst_amount=tax_dict["sgst"],
                    igst_amount=tax_dict["igst"],
                    amount=taxable + tax_dict["cgst"] + tax_dict["sgst"] + tax_dict["igst"],
                )
            )
        InvoiceItem.objects.bulk_create(new_items)
        invoice.recalculate_from_items()

    if raise_now:
        invoice = issue_invoice(invoice)

    return invoice


@transaction.atomic
def issue_invoice(invoice: Invoice, *, due_date=None) -> Invoice:
    """Issue a draft invoice, assigning the next sequential number."""
    if invoice.status not in {InvoiceStatus.DRAFT, InvoiceStatus.ISSUED}:
        raise BillingError("Only a draft invoice can be issued.")

    if not invoice.invoice_number:
        invoice.invoice_number = next_invoice_number(invoice.issue_date)

    if due_date:
        invoice.due_date = due_date
    elif not invoice.due_date:
        invoice.due_date = invoice.issue_date + timedelta(days=invoice.due_days)

    invoice.status = InvoiceStatus.ISSUED
    invoice.save(update_fields=["invoice_number", "due_date", "status", "updated_at"])
    invoice.refresh_payment_status()
    return invoice


@transaction.atomic
def void_invoice(invoice: Invoice) -> Invoice:
    """Mark an invoice as VOID."""
    if invoice.status == InvoiceStatus.VOID:
        raise BillingError("Invoice is already void.")
    invoice.status = InvoiceStatus.VOID
    invoice.save(update_fields=["status", "updated_at"])
    return invoice


@transaction.atomic
def record_payment(
    invoice: Invoice,
    *,
    amount: Decimal,
    method: str,
    reference: str = "",
    paid_at=None,
    user=None,
) -> Payment:
    """Record a verified payment against an issued invoice."""
    if invoice.status in {InvoiceStatus.DRAFT, InvoiceStatus.VOID}:
        raise BillingError("Payments cannot be recorded against a draft or void invoice.")

    raw_method = str(method or "").strip().lower()
    method = PAYMENT_METHOD_NORMALIZE.get(raw_method, raw_method.upper() if raw_method else PaymentMethod.CASH)

    payment = Payment.objects.create(
        invoice=invoice,
        patient=invoice.patient,
        amount=amount,
        currency=invoice.currency,
        method=method,
        reference=reference,
        paid_date=paid_at or timezone.now(),
        is_verified=True,
        recorded_by=user,
    )
    invoice.recalculate_payments()
    return payment


@transaction.atomic
def create_invoice_from_quote(quote: Quote, *, issue_now: bool = False) -> Invoice:
    """Convert an accepted or draft Quote into an Invoice with line items."""
    recalculate_quote(quote)

    invoice = Invoice.objects.create(
        quote=quote,
        patient=quote.patient,
        venue=quote.venue,
        service=quote.service,
        resource=quote.resource,
        booking_entity=quote.booking_entity,
        customer_name=quote.company,
        attention=quote.attention,
        address=quote.address,
        buyer_email=quote.buyer_email,
        cc_emails=quote.cc_emails,
        bcc_emails=quote.bcc_emails,
        gstin=quote.gstin,
        state=quote.state,
        state_code=quote.state_code,
        is_interstate=(quote.supply_type == QuoteSupplyType.INTER_STATE),
        ship_to=quote.ship_to,
        your_ref=quote.number,
        terms=quote.terms or default_terms(),
        notes=quote.notes,
        status=InvoiceStatus.DRAFT,
    )

    items_data = [
        {
            "description": l.item,
            "hsn_sac": l.hsn_sac,
            "specification": l.specification,
            "quantity": l.qty,
            "uom": l.uom,
            "unit_price": l.unit_price or ZERO,
            "discount_percent": l.discount_percent,
            "gst_rate": l.gst_rate,
        }
        for l in quote.lines.all()
    ]
    return save_invoice(invoice, items_data, raise_now=issue_now)


# ---------------------------------------------------------------------------
# Email Delivery
# ---------------------------------------------------------------------------

def send_invoice_email(invoice: Invoice, user=None, *, test_recipients=None) -> Invoice:
    """
    Send an Invoice to the customer / buyer via email.
    Attaches responsive HTML layout and optional generated PDF.
    """
    recipients = test_recipients or ([invoice.buyer_email] if invoice.buyer_email else [])
    if not recipients and invoice.patient and invoice.patient.email:
        recipients = [invoice.patient.email]

    if not recipients:
        raise ValueError("Cannot send invoice: No recipient email address found.")

    subject = f"{'[TEST] ' if test_recipients else ''}Invoice {invoice.invoice_number or 'DRAFT'}"
    money_fmt = lambda v: f"₹{v:,.2f}"

    item_rows = []
    if invoice.pk:
        item_rows = [
            {
                "serial": it.position,
                "description": it.description,
                "specification": it.specification,
                "hsn_sac": it.hsn_sac or "—",
                "uom": it.get_uom_display(),
                "quantity": it.quantity,
                "unit_price": money_fmt(it.unit_price),
                "discount_percent": it.discount_percent,
                "gst_rate": it.gst_rate,
                "taxable_value": money_fmt(it.taxable_value),
                "amount": money_fmt(it.amount),
            }
            for it in invoice.items.all()
        ]

    issuer = invoice.issuer or default_issuer()
    tax_rows = []
    if invoice.is_interstate:
        tax_rows.append(("IGST", money_fmt(invoice.igst_amount)))
    else:
        tax_rows.append(("CGST", money_fmt(invoice.cgst_amount)))
        tax_rows.append(("SGST", money_fmt(invoice.sgst_amount)))

    target_name = ""
    if invoice.venue:
        target_name = f"Venue: {invoice.venue.name}"
    elif invoice.service:
        target_name = f"Service: {invoice.service.name}"
    elif invoice.resource:
        target_name = f"Resource: {invoice.resource.name}"

    context = {
        "invoice_number": invoice.invoice_number or "DRAFT",
        "status": invoice.get_status_display(),
        "issue_date": invoice.issue_date.strftime("%d %b %Y"),
        "due_date": invoice.due_date.strftime("%d %b %Y") if invoice.due_date else "On receipt",
        "customer_name": invoice.customer_name or (invoice.patient.get_full_name() if invoice.patient else "Customer"),
        "customer_attention": invoice.attention,
        "customer_address": invoice.address,
        "customer_gstin": invoice.gstin or "—",
        "customer_state": invoice.state,
        "ship_to": invoice.ship_to or "Same as billing address",
        "your_ref": invoice.your_ref or "—",
        "target_entity": target_name,
        "seller_name": issuer.get("company_name", "Healthcare Services"),
        "seller_address": issuer.get("company_address", ""),
        "seller_gstin": issuer.get("gstin", "—"),
        "contact_email": user.email if user else issuer.get("contact_email", ""),
        "items": item_rows,
        "subtotal": money_fmt(invoice.subtotal),
        "discount_amount": money_fmt(invoice.discount_amount),
        "tax_rows": tax_rows,
        "tax_amount": money_fmt(invoice.tax_amount),
        "total_amount": money_fmt(invoice.total_amount),
        "paid_amount": money_fmt(invoice.paid_amount),
        "balance_due": money_fmt(invoice.balance_due),
        "terms": invoice.terms,
        "notes": invoice.notes,
        "currency": invoice.currency,
    }

    plain_text = (
        f"Dear {context['customer_name']},\n\n"
        f"Please find your invoice {context['invoice_number']}.\n"
        f"Issue Date: {context['issue_date']}\n"
        f"Due Date: {context['due_date']}\n"
        f"Total Amount: ₹{invoice.total_amount:,.2f}\n"
        f"Balance Due: ₹{invoice.balance_due:,.2f}\n\n"
        f"Thank you for choosing {context['seller_name']}."
    )

    email = EmailMultiAlternatives(
        subject,
        plain_text,
        settings.DEFAULT_FROM_EMAIL,
        recipients,
        cc=None if test_recipients else (invoice.cc_emails or None),
        bcc=None if test_recipients else (invoice.bcc_emails or None),
    )

    try:
        html_body = render_to_string("billing/invoice-email.html", context)
        email.attach_alternative(html_body, "text/html")
    except Exception as e:
        logger.warning("Could not render HTML invoice template: %s", e)

    if invoice.pdf and not test_recipients:
        try:
            invoice.pdf.seek(0)
            email.attach(
                f"Invoice-{invoice.invoice_number or invoice.id}.pdf",
                invoice.pdf.read(),
                "application/pdf",
            )
        except Exception as e:
            logger.warning("Failed to attach PDF to invoice email: %s", e)

    email.send(fail_silently=False)

    if not test_recipients:
        invoice.sent_at = timezone.now()
        invoice.save(update_fields=["sent_at"])

    return invoice
