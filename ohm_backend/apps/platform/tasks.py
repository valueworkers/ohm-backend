import logging

import dns.resolver
from celery import Task, current_app, shared_task
from django.conf import settings
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from dns.exception import Timeout
from dns.resolver import NXDOMAIN, NoAnswer, NoNameservers, Resolver


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


def _txt_values(name):
    resolver = Resolver()
    resolver.lifetime = 3.0
    try:
        answers = resolver.resolve(name, "TXT")
    except (NXDOMAIN, NoAnswer, NoNameservers, Timeout):
        return set()
    return {b"".join(r.strings).decode() for r in answers}

@shared_task
def provision_tenant_task(request_id):
    from .models import TenantProvisioning
    from .services import provision_tenant
    provision_tenant(TenantProvisioning.objects.get(pk=request_id))

@shared_task
def verify_pending_domains():
    """Mark custom domains verified once their TXT record proves ownership."""
    from .models import Domain

    base = getattr(settings, "TENANT_BASE_DOMAIN", "o-hm.com")
    with schema_context(get_public_schema_name()):
        pending = (
            Domain.objects.filter(verified=False)
            .exclude(domain=base)
            .exclude(domain__endswith=f".{base}")
        )
        for d in pending:
            if d.verification_token in _txt_values(f"{settings.DOMAIN_VERIFY_PREFIX}.{d.domain}"):
                d.verified = True
                d.verified_at = timezone.now()
                d.save(update_fields=["verified", "verified_at"])
                logger.info("Verified domain %s", d.domain)