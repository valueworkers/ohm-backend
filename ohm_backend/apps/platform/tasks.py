import logging

import dns.resolver
from celery import Task, current_app, shared_task
from django.conf import settings
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

logger = logging.getLogger(__name__)


class TenantTask(Task):
    """Base for tenant-scoped tasks: call with kwargs={'schema_name': ...}; runs inside that schema."""

    abstract = True

    def __call__(self, *args, **kwargs):
        schema = kwargs.pop("schema_name", None)
        if not schema:
            raise ValueError("Tenant tasks require a schema_name kwarg")
        with schema_context(schema):
            return super().__call__(*args, **kwargs)


@shared_task
def fanout(task_name):
    """Beat helper: run a TenantTask once per active tenant."""
    from .models import Tenant

    with schema_context(get_public_schema_name()):
        schemas = list(
            Tenant.objects.filter(status=Tenant.Status.ACTIVE)
            .exclude(schema_name=get_public_schema_name())
            .values_list("schema_name", flat=True)
        )
    for schema in schemas:
        current_app.send_task(task_name, kwargs={"schema_name": schema})


# Limits stay below services.PROVISION_STALE_AFTER (15 min) so a killed run can never overlap a retry.
# The soft limit raises inside the task, which triggers the normal rollback + "approved" reset.
@shared_task(soft_time_limit=600, time_limit=780)
def provision_onboarding_request(request_id):
    """Create the tenant for an approved onboarding request (runs in the public schema)."""
    from .services import provision_approved_request

    try:
        provision_approved_request(request_id)
    except ValueError as exc:
        # Not approved / already provisioning / already provisioned: nothing to do.
        logger.warning("Skipping provisioning for %s: %s", request_id, exc)
    # Any other exception is recorded in provision_error and the request returns to
    # "approved"; it propagates so Celery logs it. Root can retry via POST /api/platform/tenants/.


def _txt_values(name):
    try:
        answers = dns.resolver.resolve(name, "TXT")
    except Exception:
        return set()
    return {b"".join(r.strings).decode() for r in answers}


@shared_task
def verify_pending_domains():
    """Mark custom domains verified once their TXT record proves ownership."""
    from .models import Domain

    with schema_context(get_public_schema_name()):
        for d in Domain.objects.filter(verified=False):
            if d.verification_token in _txt_values(f"{settings.DOMAIN_VERIFY_PREFIX}.{d.domain}"):
                d.verified = True
                d.verified_at = timezone.now()
                d.save(update_fields=["verified", "verified_at"])