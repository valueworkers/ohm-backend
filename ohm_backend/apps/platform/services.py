import logging
import calendar
from secrets import token_urlsafe
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from rest_framework.exceptions import ValidationError
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, send_mail
from django.db import IntegrityError, transaction
from django.dispatch import Signal
from django.template.loader import render_to_string
from django.utils import timezone
from django.urls import reverse
from django.utils.html import format_html_join
from django_tenants.utils import get_public_schema_name, schema_context
from apps.platform.tasks import send_tenant_welcome_email_task
from apps.accounts.models import User
from apps.accounts.services import AccountsService
from .models import (
    DocumentSequence, Domain, Invoice, InvoiceItem,
    Payment, Plan, Quotation, Subscription, Tenant,
    TenantProvisioning, Quote, QuoteCounter, QuoteEvent,
    QuotationItem, QuoteLine, QuoteTaxSettings,InvoiceSequence
)
from .serializers import DEFAULT_TRIAL_DAYS, DEFAULT_TRIAL_PLAN_ID, base_domain
from .utils import base_domain, schema_for_slug
from . import gst


DEFAULT_INVOICE_SAC = "9983"
MAX_LINES = 30
LINE_FIELDS = (
    "line_type", "description", "hsn_sac", "specification", "quantity", "uom",
    "unit_price", "discount_percent", "lead_time", "gst_rate",
)
_ISSUER_FALLBACK = {
    "logo": "", "company_name": "", "company_address": "", "company_phone": "",
    "gstin": "", "state": "Karnataka",
    "contact_name": "", "contact_phone": "", "contact_email": "",
}
 
logger = logging.getLogger(__name__)

# Sent after the tenant schema, domains and subscription exist, but before the
# request is marked provisioned. Product apps hook in here to seed data inside
# the tenant schema (branches, ops admin, tenant admin user). If a receiver
# raises, provisioning is rolled back and can be retried.
#   kwargs: provisioning (TenantProvisioning), tenant (Tenant)
tenant_provisioned = Signal()


class BillingError(ValidationError):
    """Raised when a billing transition or calculation is invalid."""


def _money(value):
    return Decimal(str(value)).quantize(gst.CENT, rounding=ROUND_HALF_UP)


def _next_document_number(prefix, issued_on=None):
    """Allocate a year-scoped document number under a database row lock."""
    year = (issued_on or timezone.localdate()).year
    sequence, _ = DocumentSequence.objects.select_for_update().get_or_create(
        prefix=prefix, year=year, defaults={"last_value": 0},
    )
    sequence.last_value += 1
    sequence.save(update_fields=["last_value"])
    return f"{prefix}-{year}-{sequence.last_value:04d}"


def recalculate_billing_totals(document):
    """Quotation: flat tax_rate maths (unchanged). Invoice: delegates to the GST engine below."""
    if isinstance(document, Invoice):
        if document.status != Invoice.Status.DRAFT:
            raise BillingError("Only a draft invoice can be recalculated.")
        return save_invoice(document, None)
    if not isinstance(document, Quotation):
        raise TypeError("document must be a Quotation or Invoice")
 
    subtotal = Decimal("0.00")
    for line in document.items.all():
        amount = _money(Decimal(line.quantity) * line.unit_price)
        if line.amount != amount:
            line.amount = amount
            line.save(update_fields=["amount"])
        subtotal += amount
    subtotal = _money(subtotal)
    discount = _money(document.discount)
    if discount < 0 or discount > subtotal:
        raise BillingError("Discount must be between zero and the subtotal.")
    tax_rate = Decimal(document.tax_rate)
    if tax_rate < 0 or tax_rate > 100:
        raise BillingError("Tax rate must be between 0 and 100 percent.")
    taxable = subtotal - discount
    tax_amount = _money(taxable * tax_rate / Decimal("100"))
    document.subtotal = subtotal
    document.discount = discount
    document.tax_amount = tax_amount
    document.total = _money(taxable + tax_amount)
    document.save(update_fields=["subtotal", "discount", "tax_amount", "total", "updated_at"])
    return document


def _address_text(address):
    """Quotation.billing_address is JSON; Invoice.address is plain text."""
    if isinstance(address, dict):
        return "\n".join(str(v).strip() for k, v in address.items() if v and k != "state" and str(v).strip())
    return str(address or "")
 
 
@transaction.atomic
def create_quotation(*, plan, billing_name, billing_email, items=None, tenant=None,
                     provisioning_request=None, currency=None, discount=0,
                     tax_rate=None, billing_address=None, gst_number=None, valid_until=None,
                     notes="", terms="", created_by=None):
    """Create a numbered quotation and calculate its immutable monetary snapshot."""
    if bool(tenant) == bool(provisioning_request):
        raise BillingError("Provide exactly one of tenant or provisioning_request.")
    currency = (currency or plan.currency or "INR").upper()
    tax_rate = tax_rate if tax_rate is not None else plan.tax_rate
    if len(currency) != 3:
        raise BillingError("Currency must be a 3-letter code.")

    quote = Quotation.objects.create(
        number=_next_document_number("QT"), tenant=tenant,
        provisioning_request=provisioning_request, plan=plan,
        billing_period=plan.billing_period, plan_amount=plan.amount, setup_fee=plan.setup_fee,
        billing_name=billing_name, billing_email=billing_email,
        gst_number=gst_number, billing_address=billing_address or {},
        currency=currency, discount=_money(discount),
        tax_rate=tax_rate,
        valid_until=valid_until, notes=notes, terms=terms, created_by=created_by,
    )
    quote_items = list(items or [])
    if not quote_items:
        quote_items.append({
            "description": f"{plan.name} ({plan.get_billing_period_display()})",
            "quantity": 1, "unit_price": plan.amount,
        })
        if plan.setup_fee:
            quote_items.append({"description": f"{plan.name} setup fee", "quantity": 1, "unit_price": plan.setup_fee})
    for item in quote_items:
        quantity = int(item.get("quantity", 1))
        unit_price = _money(item["unit_price"])
        if quantity < 1 or unit_price < 0:
            raise BillingError("Quotation items require a positive quantity and nonnegative unit price.")
        QuotationItem.objects.create(
            quotation=quote, description=item["description"], quantity=quantity,
            unit_price=unit_price, amount=_money(quantity * unit_price),
        )
    recalculate_billing_totals(quote)
    return quote


@transaction.atomic
def send_quotation(quotation_id):
    quotation = Quotation.objects.select_for_update().get(pk=quotation_id)
    if quotation.status != Quotation.Status.DRAFT:
        raise BillingError("Only a draft quotation can be sent.")
    if not quotation.items.exists():
        raise BillingError("A quotation must contain at least one line item before it is sent.")
    recalculate_billing_totals(quotation)
    quotation.status = Quotation.Status.SENT
    quotation.sent_at = timezone.now()
    quotation.save(update_fields=["status", "sent_at", "updated_at"])
    return quotation


@transaction.atomic
def accept_quotation(quotation_id):
    quotation = Quotation.objects.select_for_update().get(pk=quotation_id)
    if quotation.status == Quotation.Status.ACCEPTED:
        return quotation
    if quotation.status != Quotation.Status.SENT:
        raise BillingError("Only a sent quotation can be accepted.")
    if quotation.valid_until and quotation.valid_until < timezone.localdate():
        quotation.status = Quotation.Status.EXPIRED
        quotation.responded_at = timezone.now()
        quotation.save(update_fields=["status", "responded_at", "updated_at"])
        return quotation
    quotation.status = Quotation.Status.ACCEPTED
    quotation.responded_at = timezone.now()
    quotation.save(update_fields=["status", "responded_at", "updated_at"])
    return quotation


@transaction.atomic
def create_invoice_from_accepted_quotation(quotation_id, *, tenant=None, subscription=None,
                                           due_date=None, period_start=None, period_end=None):
    """Create one DRAFT invoice from an accepted quote. The number is assigned when it is issued."""
    quotation = Quotation.objects.select_for_update().select_related(
        "tenant", "provisioning_request__tenant", "plan"
    ).get(pk=quotation_id)
    if quotation.status != Quotation.Status.ACCEPTED:
        raise BillingError("The quotation must be accepted before invoicing.")
    existing = Invoice.objects.filter(quotation=quotation).first()
    if existing:
        return existing
    tenant = tenant or quotation.tenant or getattr(quotation.provisioning_request, "tenant", None)
    if not tenant:
        raise BillingError("A prospect quotation can be invoiced after its tenant has been provisioned.")
    if quotation.tenant_id and tenant.pk != quotation.tenant_id:
        raise BillingError("The invoice tenant must match the quotation recipient.")
    if subscription is None:
        subscription = Subscription.objects.filter(
            tenant=tenant, status__in=(Subscription.Status.TRIAL, Subscription.Status.ACTIVE),
        ).order_by("-starts_at").first()
    if subscription and subscription.tenant_id != tenant.pk:
        raise BillingError("The selected subscription does not belong to the invoice tenant.")

    address = quotation.billing_address or {}
    issue_date = timezone.localdate()
    invoice = Invoice(
        tenant=tenant, subscription=subscription, quotation=quotation,
        customer_name=quotation.billing_name, buyer_email=quotation.billing_email,
        gstin=(quotation.gst_number or "").strip().upper(), address=_address_text(address),
        currency=quotation.currency, issue_date=issue_date,
        period_start=period_start, period_end=period_end, notes=quotation.notes,
        terms=quotation.terms or "",
    )
    if isinstance(address, dict) and address.get("state"):
        invoice.state = address["state"]
    if due_date:
        invoice.due_days = max((due_date - issue_date).days, 0)

    # The quotation has a flat header discount; invoices only have line discounts, so spread it as a %.
    discount_pct = Decimal("0")
    if quotation.subtotal and quotation.discount:
        discount_pct = (quotation.discount / quotation.subtotal * 100).quantize(gst.CENT, rounding=ROUND_HALF_UP)
    items = [{
        "line_type": InvoiceItem.LineType.CUSTOM,
        "description": item.description,
        "hsn_sac": DEFAULT_INVOICE_SAC,
        "quantity": item.quantity,
        "unit_price": item.unit_price,
        "discount_percent": discount_pct,
        "gst_rate": quotation.tax_rate,
    } for item in quotation.items.all()]
    return save_invoice(invoice, items)

@transaction.atomic
def issue_invoice(invoice, *, due_date=None):
    """Raise a draft invoice. Accepts an Invoice or its pk; assigns the next invoice number."""
    pk = invoice.pk if isinstance(invoice, Invoice) else invoice
    invoice = Invoice.objects.select_for_update().get(pk=pk)
    if invoice.status != Invoice.Status.DRAFT:
        raise BillingError("Only a draft invoice can be issued.")
    if not invoice.customer_name.strip():
        raise ValidationError({"customer_name": "Add the customer name."})
    if not invoice.items.exists():
        raise ValidationError({"items": "Add at least one line."})
    if not (invoice.issuer or {}).get("company_name"):
        raise ValidationError({"issuer": "Add the company name in the header."})
 
    if due_date is not None:
        invoice.due_date = due_date
    if not invoice.number:
        invoice.number = next_invoice_number(invoice.issue_date)
    invoice.status = Invoice.Status.ISSUED
    invoice.save()
    invoice.refresh_payment_status()   # "amount already received" -> partially paid / paid
    if invoice.status == Invoice.Status.PAID:
        _extend_subscription_from_invoice(invoice)
    return invoice
 
def _extend_subscription_from_invoice(invoice):
    if not invoice.subscription_id:
        return
    subscription = Subscription.objects.select_for_update().get(pk=invoice.subscription_id)
    today = timezone.localdate()
    was_active = subscription.status == Subscription.Status.ACTIVE
    subscription.status = Subscription.Status.ACTIVE
    if invoice.quotation_id:
        subscription.plan = invoice.quotation.plan
        subscription.price_billing_period = invoice.quotation.billing_period
        subscription.price_currency = invoice.quotation.currency
        subscription.price_amount = invoice.quotation.plan_amount
        subscription.price_setup_fee = invoice.quotation.setup_fee
        subscription.price_tax_rate = invoice.quotation.tax_rate
    if invoice.period_start:
        starts_on = invoice.period_start
    elif not subscription.ends_at or subscription.ends_at.date() < today:
        starts_on = today
    else:
        starts_on = subscription.ends_at.date() + timedelta(days=1)
    if not subscription.starts_at or not was_active:
        subscription.starts_at = timezone.make_aware(datetime.combine(starts_on, time.min))
    subscription.trial_ends_at = None
    if invoice.period_end:
        ends_on = invoice.period_end
    elif subscription.price_billing_period != Plan.BillingPeriod.ONE_TIME:
        base = max(today, subscription.ends_at.date()) if subscription.ends_at else today
        months = {
            Plan.BillingPeriod.MONTHLY: 1,
            Plan.BillingPeriod.QUARTERLY: 3,
            Plan.BillingPeriod.YEARLY: 12,
        }.get(subscription.price_billing_period)
        ends_on = None
        if months:
            month_index = base.month - 1 + months
            year = base.year + month_index // 12
            month = month_index % 12 + 1
            day = min(base.day, calendar.monthrange(year, month)[1])
            ends_on = date(year, month, day)
    else:
        ends_on = None
    if ends_on:
        if was_active and subscription.ends_at:
            ends_on = max(ends_on, subscription.ends_at.date())
        subscription.ends_at = timezone.make_aware(datetime.combine(ends_on, time.max))
    subscription.save(update_fields=["status", "plan", "price_billing_period", "price_currency", "price_amount", "price_setup_fee", "price_tax_rate", "starts_at", "ends_at", "trial_ends_at"])


@transaction.atomic
def record_gateway_payment(*, invoice_id, gateway, gateway_payment_id, amount, currency,
                           method, status=Payment.Status.SUCCEEDED, gateway_order_id="",
                           gateway_response=None, failure_reason="", recorded_by=None):
    """Record a verified gateway event idempotently and settle invoice/subscription state.

    The caller must validate the provider's webhook signature before calling this service.
    """
    if not gateway or not gateway_payment_id:
        raise BillingError("Gateway and gateway_payment_id are required for idempotent payment recording.")
    if status not in Payment.Status.values:
        raise BillingError("Unsupported payment status.")
    amount = _money(amount)
    if amount <= 0:
        raise BillingError("Payment amount must be greater than zero.")
    invoice = Invoice.objects.select_for_update(of=("self",)).select_related("subscription", "quotation__plan").get(pk=invoice_id)
    if invoice.status in {Invoice.Status.DRAFT, Invoice.Status.VOID}:
        raise BillingError("Payments can only be recorded against an issued invoice.")
    if currency.upper() != invoice.currency.upper():
        raise BillingError("Payment currency must match the invoice currency.")
    invoice_was_paid = invoice.status == Invoice.Status.PAID

    payment = Payment.objects.filter(gateway=gateway, gateway_payment_id=gateway_payment_id).first()
    if payment:
        if payment.invoice_id != invoice.pk or payment.amount != amount or payment.currency.upper() != currency.upper():
            raise BillingError("Gateway payment ID was already recorded with different payment details.")
        # Duplicate webhook delivery is a no-op. Permit pending -> terminal and
        # succeeded -> refunded transitions while rejecting stale regressions.
        allowed = {
            Payment.Status.PENDING: {Payment.Status.SUCCEEDED, Payment.Status.FAILED, Payment.Status.REFUNDED},
            Payment.Status.SUCCEEDED: {Payment.Status.REFUNDED},
            Payment.Status.FAILED: {Payment.Status.SUCCEEDED},
        }
        if status in allowed.get(payment.status, set()):
            if status == Payment.Status.SUCCEEDED and amount > invoice.balance_due:
                raise BillingError("Payment exceeds the invoice balance due.")
            payment.status = status
            payment.gateway_response = gateway_response or payment.gateway_response
            payment.failure_reason = failure_reason
            if status == Payment.Status.SUCCEEDED:
                payment.paid_at = timezone.now()
            payment.save(update_fields=["status", "gateway_response", "failure_reason", "paid_at"])
    else:
        if status == Payment.Status.REFUNDED:
            raise BillingError("A payment must be recorded as successful before it can be refunded.")
        if status == Payment.Status.SUCCEEDED and amount > invoice.balance_due:
            raise BillingError("Payment exceeds the invoice balance due.")
        try:
            with transaction.atomic():
                payment = Payment.objects.create(
                    invoice=invoice, amount=amount, currency=currency.upper(), method=method,
                    status=status, gateway=gateway, gateway_order_id=gateway_order_id,
                    gateway_payment_id=gateway_payment_id, gateway_response=gateway_response or {},
                    failure_reason=failure_reason,
                    paid_at=timezone.now() if status == Payment.Status.SUCCEEDED else None,
                    recorded_by=recorded_by,
                )
        except IntegrityError:
            payment = Payment.objects.get(gateway=gateway, gateway_payment_id=gateway_payment_id)
            if payment.invoice_id != invoice.pk or payment.amount != amount or payment.currency.upper() != currency.upper():
                raise BillingError("Gateway payment ID was concurrently recorded with different details.")

    invoice.refresh_payment_status()
    if invoice.status == Invoice.Status.PAID and not invoice_was_paid:
        invoice = Invoice.objects.select_related("subscription", "quotation__plan").get(pk=invoice.pk)
        _extend_subscription_from_invoice(invoice)
    return payment


class ProvisioningError(Exception):
    pass


def _claim(request_id):
    """APPROVED -> PROVISIONING under a row lock, so two workers can't provision one request."""
    with transaction.atomic():
        p = TenantProvisioning.objects.select_for_update().get(pk=request_id)
        if p.status != TenantProvisioning.Status.APPROVED:
            raise ProvisioningError(f"Request is {p.status}; expected approved.")
        p.status = TenantProvisioning.Status.PROVISIONING
        p.provision_error = ""
        p.save(update_fields=["status", "provision_error", "updated_at"])
    return p


def _assign_owner_role(tenant, applicant):
    """Idempotent: mirror the applicant into the tenant schema and give them TENANT_ADMIN."""
    with schema_context(tenant.schema_name):
        AccountsService.ensure_tenant_admin(applicant)


def _assign_roles_to_tenant_owners(tenant):
    """Mirror every public-schema tenant owner and grant TENANT_ADMIN in the tenant schema."""
    with schema_context(get_public_schema_name()):
        owners = list(tenant.owners.all())
        for owner in owners:
            AccountsService.assign_public_tenant_owner_role(tenant, owner)

    with schema_context(tenant.schema_name):
        for owner in owners:
            AccountsService.ensure_tenant_admin(owner)

def _get_or_create_tenant(p):
    """Returns (tenant, created). Creating the Tenant also creates and migrates its schema."""
    if not p.requested_plan_id or not p.requested_plan.is_active:
        raise ProvisioningError("An active subscription plan is required to provision this tenant.")
    schema = schema_for_slug(p.slug)
    existing = Tenant.objects.filter(schema_name=schema).first()
    if existing:
        # Leftover from a crashed attempt of this same request is reusable; anything else is a clash.
        if existing.owners.filter(pk=p.applicant_id).exists():
            return existing, False
        raise ProvisioningError(f"Schema '{schema}' already belongs to another tenant.")

    modules = []
    if p.requested_plan_id:
        modules = list(p.requested_plan.features.filter(is_active=True).values_list("code", flat=True))

    tenant = Tenant(
        schema_name=schema,
        name=p.organization_name,
        status=Tenant.Status.PROVISIONING,  # stays here until the owner completes setup and activates
        modules=modules,
        logo=p.logo.name if p.logo else "",
        contact_email=p.email,
        contact_phone=p.contact_phone,
        gst_number=p.gst_number or None,
    )
    tenant.save()
    tenant.owners.add(p.applicant)
    _assign_owner_role(tenant, p.applicant)
    return tenant, True


def _ensure_domain(tenant, domain, **defaults):
    obj, _ = Domain.objects.get_or_create(domain=domain, defaults={"tenant": tenant, **defaults})
    if obj.tenant_id != tenant.pk:
        raise ProvisioningError(f"Domain '{domain}' already belongs to another tenant.")
    return obj

def _create_domains_and_subscription(p, tenant):
    now = timezone.now()
    # Platform subdomain: we own the DNS, so it is trusted and primary immediately.
    _ensure_domain(tenant, f"{p.slug}.{base_domain()}", is_primary=True, verified=True, verified_at=now)
    # Custom domain: stays unverified until the TXT check (verify_pending_domains) passes.
    if p.custom_domain:
        _ensure_domain(tenant, p.custom_domain, is_primary=False, verified=False)

    subscription, created = Subscription.objects.get_or_create(
        tenant=tenant,
        plan_id=p.requested_plan_id,
        defaults={
            "price_billing_period": p.requested_plan.billing_period,
            "price_currency": p.requested_plan.currency,
            "price_amount": p.requested_plan.amount,
            "price_setup_fee": p.requested_plan.setup_fee,
            "price_tax_rate": p.requested_plan.tax_rate,
            "status": (
                Subscription.Status.TRIAL
                if p.requested_plan_id == DEFAULT_TRIAL_PLAN_ID
                else Subscription.Status.ACTIVE
            ),
            "starts_at": now,
            "trial_ends_at": (
                now + timedelta(days=DEFAULT_TRIAL_DAYS)
                if p.requested_plan_id == DEFAULT_TRIAL_PLAN_ID
                else None
            ),
            "billing_email": p.email,
        },
    )


def _rollback_tenant(tenant):
    try:
        tenant.auto_drop_schema = True
        tenant.delete(force_drop=True)  # drops the schema; Domain/Subscription cascade
    except Exception:
        logger.exception("Could not roll back tenant %s; clean up the schema manually.", tenant.schema_name)


def provision_tenant(provisioning):
    """Turn an approved onboarding request into a tenant. Safe to retry after a failure.

    Success: request -> PROVISIONED, Tenant.status stays PROVISIONING (awaiting the owner).
    Failure: request -> APPROVED with provision_error set, partial work rolled back.
    """
    p = _claim(provisioning.pk)
    created_tenant = None
    try:
        with schema_context(get_public_schema_name()):
            tenant, created = _get_or_create_tenant(p)
            if created:
                created_tenant = tenant
            with transaction.atomic():
                _create_domains_and_subscription(p, tenant)
            tenant_provisioned.send(sender=TenantProvisioning, provisioning=p, tenant=tenant)
            _assign_roles_to_tenant_owners(tenant)
            with transaction.atomic():
                applicant = User.objects.filter(pk=p.applicant_id, is_active=False).first()
                if applicant:
                    applicant.is_active = True
                    applicant.save(update_fields=["is_active"])
                # Also activate any co-owner accounts that were created as stubs.
                co_emails = [e.strip().lower() for e in (p.co_owner_emails or []) if e.strip()]
                for co_owner in User.objects.filter(email__in=co_emails, is_active=False):
                    co_owner.is_active = True
                    co_owner.save(update_fields=["is_active"])
                p.tenant = tenant
                p.status = TenantProvisioning.Status.PROVISIONED
                p.provision_error = ""
                p.save(update_fields=["tenant", "status", "provision_error", "updated_at"])
    except Exception as exc:
        logger.exception("Provisioning failed for request %s", p.pk)
        if created_tenant is not None:
            _rollback_tenant(created_tenant)
        TenantProvisioning.objects.filter(pk=p.pk).update(
            status=TenantProvisioning.Status.APPROVED,
            tenant=None,
            provision_error=str(exc)[:2000],
            updated_at=timezone.now(),
        )
        raise ProvisioningError(str(exc)) from exc
    transaction.on_commit(lambda: send_tenant_welcome_email_task.delay(str(p.pk)))
    return tenant


def get_seller_tax_settings(tenant_id):
    """Return (state_code, state_name, gstin) for the public control plane or one tenant."""
    cfg = QuoteTaxSettings.objects.filter(tenant_id=tenant_id).first()
    if not cfg:
        return "", "", ""
    code = gst.state_code_from_gstin(cfg.gstin) or cfg.state_code or gst.state_code_from_name(cfg.state_name)
    name = gst.STATE_CODES.get(code, cfg.state_name)
    return code, name, cfg.gstin


def quote_terms(quote):
    """Validity is always the first term (the UI promises this); then one term per line."""
    lines = [f"Validity: {quote.valid_days} days from {quote.quote_date:%d %b %Y} (expires {quote.valid_until:%d %b %Y})."]
    lines += [t.strip() for t in (quote.terms or "").splitlines() if t.strip()]
    return lines


def recalculate(quote):
    """Indian GST maths, line by line.

    taxable = qty x price - line discount
    same state    -> CGST = SGST = taxable x rate / 200   (UTGST instead of SGST for UTs w/o legislature)
    other state   -> IGST = taxable x rate / 100
    freight/packing are added to the taxable value and taxed at charges_gst_rate.
    The grand total is rounded to the nearest rupee and the difference stored in round_off.
    """
    # Query fresh: quote.lines.all() can return a stale prefetch cache right after the serializer replaced the lines.
    lines = list(QuoteLine.objects.filter(quote=quote).order_by("position"))

    seller_code, seller_name, seller_gstin = get_seller_tax_settings(quote.tenant_id)
    buyer_code = gst.resolve_state_code(quote.gstin, quote.state)
    stype = gst.supply_type(seller_code, buyer_code)

    gross = discount_total = taxable_total = cgst = sgst = igst = gst.ZERO
    for line in lines:
        if line.unit_price is None:                      # "on request" line: priced later
            values = dict(taxable_value=gst.ZERO, cgst_amount=gst.ZERO, sgst_amount=gst.ZERO, igst_amount=gst.ZERO, line_total=gst.ZERO)
        else:
            line_gross = gst.money(line.qty * line.unit_price)
            line_disc = gst.money(line_gross * line.discount_percent / 100)
            taxable = line_gross - line_disc
            c, s, i = gst.split_tax(taxable, line.gst_rate, stype)
            gross += line_gross
            discount_total += line_disc
            taxable_total += taxable
            cgst, sgst, igst = cgst + c, sgst + s, igst + i
            values = dict(taxable_value=taxable, cgst_amount=c, sgst_amount=s, igst_amount=i, line_total=taxable + c + s + i)
        if any(getattr(line, k) != v for k, v in values.items()):
            for k, v in values.items():
                setattr(line, k, v)
            line.save(update_fields=list(values))

    charges = gst.money((quote.freight or 0) + (quote.packing or 0))
    if charges:
        c, s, i = gst.split_tax(charges, quote.charges_gst_rate, stype)
        taxable_total += charges
        cgst, sgst, igst = cgst + c, sgst + s, igst + i

    quote.subtotal = gross
    quote.discount_total = discount_total
    quote.taxable_amount = taxable_total
    quote.supply_type = stype
    quote.place_of_supply_code = buyer_code
    quote.state_code = buyer_code
    quote.seller_gstin, quote.seller_state_code, quote.seller_state_name = seller_gstin, seller_code, seller_name
    if stype == gst.UNKNOWN:                             # can't split tax without both states
        quote.cgst = quote.sgst = quote.igst = quote.round_off = gst.ZERO
        quote.total = None
    else:
        raw_total = taxable_total + cgst + sgst + igst
        quote.cgst, quote.sgst, quote.igst = cgst, sgst, igst
        quote.total = raw_total.quantize(Decimal("1"), rounding=ROUND_HALF_UP).quantize(gst.CENT)
        quote.round_off = quote.total - raw_total
    quote.save(update_fields=[
        "subtotal", "discount_total", "taxable_amount", "cgst", "sgst", "igst", "round_off", "total",
        "supply_type", "place_of_supply_code", "state_code", "seller_gstin", "seller_state_code", "seller_state_name",
    ])
    return quote


@transaction.atomic
def create_quote(user, data, lines, *, tenant=None):
    today = timezone.localdate()
    number = (data.pop("number", "") or "").strip()
    if number:                                           # user typed their own quote no.
        if Quote.objects.filter(number=number).exists():
            raise ValueError(f"Quote number {number} is already in use.")
    else:
        period = today.strftime("%y%m")
        counter, _ = QuoteCounter.objects.select_for_update().get_or_create(period=period)
        counter.last_seq += 1
        counter.save(update_fields=["last_seq"])
        number = f"Q-{period}-{counter.last_seq:03d}"
    quote = Quote.objects.create(
        number=number,
        quote_date=today,
        tenant=tenant,
        created_by=user if tenant is None else None,
        created_by_email=user.email,
        **data,
    )
    for index, line in enumerate(lines, 1):
        QuoteLine.objects.create(quote=quote, position=index, **line)
    recalculate(quote)
    QuoteEvent.objects.create(quote=quote, kind="created", actor=user if tenant is None else None, detail="" if tenant is None else user.email)
    return quote


@transaction.atomic
def send_quote(quote, user, *, test_recipients=None, response_url=None):
    quote = Quote.objects.select_for_update().get(pk=quote.pk)
    if quote.status != Quote.Status.DRAFT:
        raise ValueError("Only draft quotes can be sent.")
    recalculate(quote)
    if quote.total is None:
        raise ValueError("Set a valid GSTIN or state before sending this quote.")
    recipients = test_recipients or [quote.buyer_email]
    subject = f"{'[TEST] ' if test_recipients else ''}Quote {quote.number} R{quote.revision}"
    message = (
        f"Please find quote {quote.number} revision {quote.revision}. Total: {quote.total}. "
        f"Valid until {quote.valid_until}. Use the Accept or Reject buttons in an AMP-enabled email client."
    )
    email = EmailMultiAlternatives(
        subject, message, settings.DEFAULT_FROM_EMAIL, recipients,
        cc=None if test_recipients else (quote.cc_emails or None),
        bcc=None if test_recipients else (quote.bcc_emails or None),
    )
    if not test_recipients:
        response_url = response_url or reverse("public-quote-response", kwargs={"token": quote.accept_token})
        money_fmt = lambda v: f"₹{v:,.2f}"
        item_rows = [
            {
                "serial": line.position,
                "name": line.item,
                "description": line.specification or "",
                "meta": line.hsn_sac or line.get_kind_display(),
                "hsn_sac": line.hsn_sac,
                "uom": line.get_uom_display(),
                "qty": line.qty,
                "unit_price": "On request" if line.unit_price is None else money_fmt(line.unit_price),
                "discount_percent": line.discount_percent,
                "gst_rate": line.gst_rate,
                "taxable": "—" if line.unit_price is None else money_fmt(line.taxable_value),
                "amount": "On request" if line.unit_price is None else money_fmt(line.line_total),
            }
            for line in quote.lines.all()
        ]
        # Same-state: CGST + SGST/UTGST rows. Other state: one IGST row.
        if quote.supply_type == Quote.SupplyType.INTER_STATE:
            tax_rows = [("IGST", money_fmt(quote.igst))]
        else:
            tax_rows = [("CGST", money_fmt(quote.cgst)), (quote.state_tax_label, money_fmt(quote.sgst))]
        address_lines = [line for line in quote.address.splitlines() if line.strip()]
        email_context = {
            "quote_number": quote.number,
            "revision": f"R{quote.revision}",
            "quote_date": quote.quote_date.strftime("%d %b %Y"),
            "valid_until": quote.valid_until.strftime("%d %b %Y"),
            "customer_name": quote.company,
            "customer_attention": quote.attention,
            "customer_lines": format_html_join("<br />", "{}", ((line,) for line in address_lines)),
            "gstin": quote.gstin or "—",
            "seller_gstin": quote.seller_gstin or "—",
            "customer_region": f"{quote.state} ({quote.state_code})" if quote.state_code else quote.state,
            "place_of_supply": f"{quote.state} ({quote.place_of_supply_code})",
            "supply_type": quote.get_supply_type_display(),
            "ship_to": quote.ship_to or "Same as above",
            "your_contact_name": user.get_full_name() or "O-HM Senior Care",
            "your_contact_email": user.email or settings.DEFAULT_FROM_EMAIL,
            "your_ref": quote.your_ref or "—",
            "items": item_rows,
            "subtotal": money_fmt(quote.subtotal),
            "discount_total": money_fmt(quote.discount_total),
            "taxable_amount": money_fmt(quote.taxable_amount),
            "packing_value": "—" if quote.packing is None else money_fmt(quote.packing),
            "freight_value": "—" if quote.freight is None else money_fmt(quote.freight),
            "tax_rows": tax_rows,
            "gst": money_fmt(quote.cgst + quote.sgst + quote.igst),
            "round_off": f"{quote.round_off:+,.2f}",
            "total": money_fmt(quote.total),
            "notes": quote.notes,
            "terms": quote_terms(quote),
            "accept_url": response_url,
        }
        email.attach_alternative(render_to_string("ohm-quotation-email.html", email_context), "text/html")
        email.attach_alternative(render_to_string("ohm-quotation-email.amp.html", email_context), "text/x-amp-html")
    email.send(fail_silently=False)
    if test_recipients:
        QuoteEvent.objects.create(quote=quote, kind="test_sent", detail=", ".join(recipients)[:300], actor=user if quote.tenant_id is None else None)
        return quote
    quote.status, quote.sent_at = Quote.Status.SENT, timezone.now()
    quote.save(update_fields=["status", "sent_at"])
    Quote.objects.filter(number=quote.number, revision__lt=quote.revision, status=Quote.Status.SENT).update(status=Quote.Status.SUPERSEDED)
    QuoteEvent.objects.create(quote=quote, kind="sent", detail=quote.buyer_email, actor=user if quote.tenant_id is None else None)
    return quote

@transaction.atomic
def revise_quote(quote, user):
    """Copy a SENT quote into a new DRAFT revision (same number, revision + 1)."""
    if quote.status != Quote.Status.SENT:
        raise ValueError("Only sent quotes can be revised.")
    lines = list(quote.lines.all())
    previous_revision = quote.revision
    quote.pk = None
    quote._state.adding = True
    quote.revision += 1
    quote.status = Quote.Status.DRAFT
    quote.sent_at = quote.accepted_at = None
    quote.accept_token = token_urlsafe(32)
    quote.quote_date = timezone.localdate()
    quote.created_by = user if quote.tenant_id is None else None
    quote.created_by_email = user.email
    quote.save()
    for line in lines:
        line.pk = None
        line._state.adding = True
        line.quote = quote
        line.save()
    recalculate(quote)          # re-apply today's seller GSTIN/state and rounding to the new draft
    QuoteEvent.objects.create(
        quote=quote, kind="revised",
        detail=(f"from R{previous_revision}" if quote.tenant_id is None else f"{user.email}: from R{previous_revision}"),
        actor=user if quote.tenant_id is None else None,
    )
    return quote


@transaction.atomic
def respond_to_quote(token, accepted):
    """Buyer accepts or rejects via the public link or AMP email. Expired quotes flip to EXPIRED instead."""
    quote = Quote.objects.select_for_update().get(accept_token=token)
    if quote.status != Quote.Status.SENT:
        raise ValueError("This quote is no longer available for a response.")
    if quote.valid_until < timezone.localdate():
        quote.status = Quote.Status.EXPIRED
        quote.save(update_fields=["status"])
        QuoteEvent.objects.create(quote=quote, kind="expired", detail="Response attempted after expiry")
        return quote
    quote.status = Quote.Status.ACCEPTED if accepted else Quote.Status.REJECTED
    quote.accepted_at = timezone.now() if accepted else None
    quote.save(update_fields=["status", "accepted_at"])
    QuoteEvent.objects.create(quote=quote, kind=quote.status)
    return quote


def default_issuer() -> dict:
    base = {**_ISSUER_FALLBACK, **getattr(settings, "INVOICE_DEFAULT_ISSUER", {})}
    seller_code, seller_name, seller_gstin = get_seller_tax_settings(None)
    if seller_gstin and not base.get("gstin"):
        base["gstin"] = seller_gstin
    if seller_name and (not base.get("state") or base.get("state") == "Karnataka"):
        base["state"] = seller_name
    return base
 
 
def default_terms() -> str:
    return getattr(settings, "INVOICE_DEFAULT_TERMS", "")
 
 
def default_due_days() -> int:
    return getattr(settings, "INVOICE_DEFAULT_DUE_DAYS", 15)
 
 
def next_invoice_number(issue_date, *, lock: bool = True) -> str:
    """e.g. 459/202626. lock=False only previews and does not consume a number."""
    year = issue_date.year
    if lock:
        seq, _ = InvoiceSequence.objects.select_for_update().get_or_create(year=year)
        seq.last_number += 1
        seq.save(update_fields=["last_number"])
        n = seq.last_number
    else:
        seq = InvoiceSequence.objects.filter(year=year).first()
        n = (seq.last_number if seq else 0) + 1
    return f"{n}/{year}{year % 100:02d}"
 
 
@transaction.atomic
def save_invoice(invoice: Invoice, items, *, raise_now: bool = False) -> Invoice:
    """Recalculate GST + totals exactly like recalculate(quote), persist lines, optionally raise."""
    if invoice.pk and invoice.status != Invoice.Status.DRAFT:
        raise BillingError("Only draft invoices can be edited.")
    if not invoice.issuer:
        invoice.issuer = default_issuer()
    issuer = invoice.issuer
 
    # Seller = the platform's QuoteTaxSettings (same source as platform quotes); issuer header is the fallback.
    seller_code, _name, seller_gstin = get_seller_tax_settings(invoice.tenant_id)
    if not seller_code:
        seller_code = gst.resolve_state_code(issuer.get("gstin", ""), issuer.get("state", ""))
    if seller_gstin and not issuer.get("gstin"):
        invoice.issuer = {**issuer, "gstin": seller_gstin}
 
    buyer_code = gst.resolve_state_code(invoice.gstin, invoice.state)
    if not buyer_code:
        raise ValidationError({"state": "Select the customer's state or enter a valid GSTIN."})
    if not seller_code:
        raise ValidationError({"issuer": "Set the seller GSTIN/state in quote tax settings or the header."})
    stype = gst.supply_type(seller_code, buyer_code)   # intra_state / intra_ut / inter_state
    invoice.state_code = buyer_code
    invoice.state = gst.STATE_CODES[buyer_code]
    invoice.is_interstate = stype == gst.INTER_STATE
 
    if invoice.pk is None and not invoice.terms:
        invoice.terms = default_terms()
    invoice.due_days = invoice.due_days if invoice.due_days is not None else default_due_days()
    invoice.due_date = invoice.issue_date + timedelta(days=invoice.due_days)
 
    if items is None:  # recalculation of a saved draft: reuse its lines
        items = [{k: getattr(i, k) for k in LINE_FIELDS} for i in invoice.items.all()]
    if len(items) > MAX_LINES:
        raise ValidationError({"items": f"An invoice can have at most {MAX_LINES} lines."})
 
    computed = []
    for pos, row in enumerate(items, start=1):
        calc = gst.compute_line(
            quantity=row["quantity"], unit_price=row["unit_price"],
            discount_percent=row.get("discount_percent") or 0, gst_rate=row["gst_rate"], stype=stype,
        )
        computed.append({**{k: row[k] for k in LINE_FIELDS if k in row}, **calc, "position": pos})
 
    for field, value in gst.sum_lines(computed).items():
        setattr(invoice, field, value)
    if invoice.amount_received is None:
        invoice.amount_received = Decimal("0.00")
    if computed and invoice.amount_received > invoice.total:
        raise ValidationError({"amount_received": "Cannot exceed the invoice total."})
 
    invoice.save()
    invoice.items.all().delete()
    InvoiceItem.objects.bulk_create([InvoiceItem(invoice=invoice, **c) for c in computed])
    if hasattr(invoice, "_prefetched_objects_cache"):
        invoice._prefetched_objects_cache.clear()
 
    if raise_now:
        invoice = issue_invoice(invoice)
    return invoice
 
 
@transaction.atomic
def void_invoice(invoice: Invoice) -> Invoice:
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == Invoice.Status.VOID:
        raise BillingError("Invoice is already void.")
    if invoice.payments.filter(status=Payment.Status.SUCCEEDED).exists():
        raise BillingError("Refund the recorded payments before voiding this invoice.")
    invoice.status = Invoice.Status.VOID
    invoice.save(update_fields=["status", "updated_at"])
    return invoice
 
 
@transaction.atomic
def record_payment(invoice: Invoice, *, amount, method, reference="", paid_at=None, user=None) -> Payment:
    """Manual payment (cash / cheque / bank transfer / UPI). Gateway payments use record_gateway_payment."""
    invoice = Invoice.objects.select_for_update(of=("self",)).select_related("subscription", "quotation__plan").get(pk=invoice.pk)
    if invoice.status in {Invoice.Status.DRAFT, Invoice.Status.VOID}:
        raise BillingError("Payments can only be recorded against an issued invoice.")
    amount = _money(amount)
    if amount > invoice.balance_due:
        raise ValidationError({"amount": f"Cannot exceed the balance due ({invoice.balance_due})."})
    was_paid = invoice.status == Invoice.Status.PAID
    payment = Payment.objects.create(
        invoice=invoice, amount=amount, currency=invoice.currency, method=method,
        status=Payment.Status.SUCCEEDED, reference=reference,
        paid_at=paid_at or timezone.now(), recorded_by=user,
    )
    invoice.refresh_payment_status()
    if invoice.status == Invoice.Status.PAID and not was_paid:
        _extend_subscription_from_invoice(invoice)     # same settlement as a gateway payment
    return payment
 
 
def mark_overdue_invoices() -> int:
    """Run daily (Celery beat). Moves unpaid, past-due invoices to OVERDUE."""
    n = 0
    for inv in Invoice.objects.filter(status=Invoice.Status.ISSUED, due_date__lt=timezone.localdate()):
        inv.refresh_payment_status()
        n += 1
    return n