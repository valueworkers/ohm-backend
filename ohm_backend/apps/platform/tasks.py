import dns.resolver
from celery import Task, current_app, shared_task
from django.conf import settings
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context


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
