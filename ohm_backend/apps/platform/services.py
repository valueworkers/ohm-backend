import logging
import calendar
from secrets import token_urlsafe
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.dispatch import Signal
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from apps.platform.tasks import send_tenant_welcome_email_task
from apps.accounts.models import User
from apps.accounts.services import AccountsService
from .models import (
    DocumentSequence, Domain, Invoice, InvoiceItem, Payment, Plan, Quotation,
    QuotationItem, Subscription, Tenant, TenantProvisioning,
    Quote, QuoteCounter, QuoteEvent, QuoteLine, QuoteTaxSettings,
)
from .serializers import DEFAULT_TRIAL_DAYS, DEFAULT_TRIAL_PLAN_ID, base_domain
from .utils import base_domain, schema_for_slug



logger = logging.getLogger(__name__)

# Sent after the tenant schema, domains and subscription exist, but before the
# request is marked provisioned. Product apps hook in here to seed data inside
# the tenant schema (branches, ops admin, tenant admin user). If a receiver
# raises, provisioning is rolled back and can be retried.
#   kwargs: provisioning (TenantProvisioning), tenant (Tenant)
tenant_provisioned = Signal()


CENT = Decimal("0.01")


class BillingError(Exception):
    """Raised when a billing transition or calculation is invalid."""


def _money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


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
    """Calculate line amounts and persisted subtotal/tax/total for a quote or invoice."""
    if isinstance(document, Quotation):
        lines = document.items.all()
    elif isinstance(document, Invoice):
        lines = document.items.all()
    else:
        raise TypeError("document must be a Quotation or Invoice")

    subtotal = Decimal("0.00")
    for line in lines:
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
    """Create one invoice from an accepted quote, copying all line and billing snapshots."""
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
    invoice = Invoice.objects.create(
        number=_next_document_number("INV"), tenant=tenant, subscription=subscription,
        quotation=quotation, billing_name=quotation.billing_name,
        billing_email=quotation.billing_email, gst_number=quotation.gst_number,
        billing_address=quotation.billing_address, currency=quotation.currency,
        subtotal=quotation.subtotal, discount=quotation.discount,
        tax_rate=quotation.tax_rate, tax_amount=quotation.tax_amount, total=quotation.total,
        due_date=due_date, period_start=period_start, period_end=period_end, notes=quotation.notes,
    )
    for item in quotation.items.all():
        InvoiceItem.objects.create(
            invoice=invoice, description=item.description, quantity=item.quantity,
            unit_price=item.unit_price, amount=item.amount,
        )
    return invoice


@transaction.atomic
def issue_invoice(invoice_id, *, due_date=None):
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
    if invoice.status != Invoice.Status.DRAFT:
        raise BillingError("Only a draft invoice can be issued.")
    invoice.status = Invoice.Status.ISSUED
    invoice.issue_date = timezone.localdate()
    if due_date is not None:
        invoice.due_date = due_date
    invoice.save(update_fields=["status", "issue_date", "due_date", "updated_at"])
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
    invoice = Invoice.objects.select_for_update().select_related("subscription", "quotation__plan").get(pk=invoice_id)
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

CENT = Decimal("0.01")


def recalculate(quote):
    lines = list(quote.lines.all())
    priced = [line for line in lines if line.unit_price is not None]
    quote.subtotal = sum((line.qty * line.unit_price for line in priced), Decimal("0")).quantize(CENT, rounding=ROUND_HALF_UP)
    buyer_state_code = quote.gstin[:2] if quote.gstin[:2].isdigit() else None
    buyer_state_name = quote.state.strip().casefold()
    tax_settings = QuoteTaxSettings.objects.filter(tenant_id=quote.tenant_id).first()
    seller_state_code = tax_settings.state_code if tax_settings else ""
    seller_state_name = tax_settings.state_name.strip().casefold() if tax_settings else ""

    if buyer_state_code and seller_state_code:
        state_is_known = True
        is_intrastate = buyer_state_code == seller_state_code
    elif buyer_state_name and seller_state_name:
        state_is_known = True
        is_intrastate = buyer_state_name == seller_state_name
    else:
        state_is_known = False
        is_intrastate = False
    tax = sum((line.qty * line.unit_price * line.gst_rate / Decimal("100") for line in priced), Decimal("0")).quantize(CENT, rounding=ROUND_HALF_UP)
    if not state_is_known:
        quote.cgst = quote.sgst = quote.igst = Decimal("0.00")
        quote.total = None
    elif is_intrastate:
        quote.cgst = (tax / 2).quantize(CENT, rounding=ROUND_HALF_UP)
        quote.sgst = tax - quote.cgst
        quote.igst = Decimal("0.00")
        quote.total = quote.subtotal + tax + (quote.freight or Decimal("0"))
    else:
        quote.cgst = quote.sgst = Decimal("0.00")
        quote.igst = tax
        quote.total = quote.subtotal + tax + (quote.freight or Decimal("0"))
    quote.save(update_fields=["subtotal", "cgst", "sgst", "igst", "total"])
    return quote


@transaction.atomic
def create_quote(user, data, lines, *, tenant=None):
    today = timezone.localdate()
    period = today.strftime("%y%m")
    counter, _ = QuoteCounter.objects.select_for_update().get_or_create(period=period)
    counter.last_seq += 1
    counter.save(update_fields=["last_seq"])
    quote = Quote.objects.create(
        number=f"SC-Q-{period}-{counter.last_seq:03d}",
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
def send_quote(quote, user, *, test_recipients=None):
    quote = Quote.objects.select_for_update().get(pk=quote.pk)
    if quote.status != Quote.Status.DRAFT:
        raise ValueError("Only draft quotes can be sent.")
    recalculate(quote)
    if quote.total is None:
        raise ValueError("Set a valid GSTIN or state before sending this quote.")
    recipients = test_recipients or [quote.buyer_email]
    subject = f"{'[TEST] ' if test_recipients else ''}Quote {quote.number} R{quote.revision}"
    message = f"Please find quote {quote.number} revision {quote.revision}. Total: {quote.total}. Valid until {quote.valid_until}."
    send_mail(subject, message, None, recipients, fail_silently=False)
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
    if quote.status != Quote.Status.SENT:
        raise ValueError("Only sent quotes can be revised.")
    lines = list(quote.lines.all())
    quote.pk = None
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
        line.quote = quote
        line.save()
    QuoteEvent.objects.create(quote=quote, kind="revised", detail=(f"from R{quote.revision - 1}" if quote.tenant_id is None else f"{user.email}: from R{quote.revision - 1}"), actor=user if quote.tenant_id is None else None)
    return quote


@transaction.atomic
def respond_to_quote(token, accepted):
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
