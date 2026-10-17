"""
Convert Tenant.owner (FK) → Tenant.owners (M2M).

Three-step migration so existing owner data is preserved:
  1. Add the new M2M `owners` field.
  2. Data migration: copy each tenant's FK `owner` into the M2M.
  3. Remove the old FK `owner` column.
"""
from django.conf import settings
from django.db import migrations, models


def copy_owner_to_owners(apps, schema_editor):
    """For every tenant that has an owner FK, add that user to the M2M owners."""
    Tenant = apps.get_model("tenants", "Tenant")
    for tenant in Tenant.objects.filter(owner__isnull=False).select_related("owner"):
        tenant.owners.add(tenant.owner)


def copy_owners_to_owner(apps, schema_editor):
    """Reverse: pick the first owner from M2M and put it back into the FK."""
    Tenant = apps.get_model("tenants", "Tenant")
    for tenant in Tenant.objects.all():
        first_owner = tenant.owners.first()
        if first_owner:
            tenant.owner = first_owner
            tenant.save(update_fields=["owner"])


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0003_remove_tenantprovisioning_address_tenant_gst_number_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Step 1: Add the M2M field alongside the existing FK.
        migrations.AddField(
            model_name="tenant",
            name="owners",
            field=models.ManyToManyField(
                blank=True,
                related_name="owned_tenants",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # Step 2: Copy data from FK → M2M.
        migrations.RunPython(copy_owner_to_owners, copy_owners_to_owner),
        # Step 3: Drop the old FK column.
        migrations.RemoveField(
            model_name="tenant",
            name="owner",
        ),
    ]
