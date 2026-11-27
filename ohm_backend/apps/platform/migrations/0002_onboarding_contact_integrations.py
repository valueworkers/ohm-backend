from django.db import migrations, models


def move_legacy_contacts(apps, schema_editor):
    Provisioning = apps.get_model("tenants", "TenantProvisioning")
    Tenant = apps.get_model("tenants", "Tenant")
    for request in Provisioning.objects.all().iterator():
        request.contact_emails = {"primary": request.email} if request.email else None
        request.contact_phones = {"primary": request.contact_phone} if request.contact_phone else None
        request.ops_admin_emails = [request.ops_admin_email] if request.ops_admin_email else None
        request.save(update_fields=["contact_emails", "contact_phones", "ops_admin_emails"])
    for tenant in Tenant.objects.all().iterator():
        tenant.contact_emails = {"primary": tenant.contact_email} if tenant.contact_email else None
        tenant.contact_phones = {"primary": tenant.contact_phone} if tenant.contact_phone else None
        tenant.save(update_fields=["contact_emails", "contact_phones"])


class Migration(migrations.Migration):
    dependencies = [("tenants", "0001_initial")]

    operations = [
        migrations.AddField("tenant", "contact_emails", models.JSONField(blank=True, null=True)),
        migrations.AddField("tenant", "contact_phones", models.JSONField(blank=True, null=True)),
        migrations.AddField("tenant", "existing_website", models.URLField(blank=True, max_length=255, null=True)),
        migrations.AddField("tenant", "erp_name", models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField("tenant", "existing_payment_providers", models.JSONField(blank=True, null=True)),
        migrations.AddField("tenant", "provided_payment_providers", models.JSONField(blank=True, null=True)),
        migrations.AddField("tenant", "razorpay_account_id", models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField("tenant", "razorpay_key_id", models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField("tenant", "cashfree_app_id", models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField("tenant", "cashfree_secret_key", models.TextField(blank=True, null=True)),
        migrations.AddField("tenantprovisioning", "contact_emails", models.JSONField(blank=True, null=True, help_text='{"primary": "a@x.com", "secondary": "b@x.com", "tertiary": "c@x.com"}')),
        migrations.AddField("tenantprovisioning", "contact_phones", models.JSONField(blank=True, null=True, help_text='{"primary": "9889765432", "secondary": "9876543210", "tertiary": "..."}')),
        migrations.AddField("tenantprovisioning", "ops_admin_emails", models.JSONField(blank=True, null=True, help_text='["ops1@x.com", "ops2@x.com"]; NULL if none.')),
        migrations.AddField("tenantprovisioning", "existing_website", models.URLField(blank=True, max_length=255, null=True)),
        migrations.AddField("tenantprovisioning", "erp_name", models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField("tenantprovisioning", "existing_payment_providers", models.JSONField(blank=True, null=True, help_text="Own gateway = Yes: providers we connect.")),
        migrations.AddField("tenantprovisioning", "provided_payment_providers", models.JSONField(blank=True, null=True, help_text="Own gateway = No: providers we set up for them.")),
        migrations.AddField("tenantprovisioning", "razorpay_account_id", models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField("tenantprovisioning", "razorpay_key_id", models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField("tenantprovisioning", "cashfree_app_id", models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField("tenantprovisioning", "cashfree_secret_key", models.TextField(blank=True, null=True)),
        migrations.RunPython(move_legacy_contacts, migrations.RunPython.noop),
        migrations.AddConstraint("tenantprovisioning", models.CheckConstraint(condition=models.Q(("contact_emails__isnull", False), ("contact_emails__has_key", "primary"), ("contact_phones__isnull", False), ("contact_phones__has_key", "primary")), name="contacts_primary_required")),
        migrations.RemoveField("tenant", "contact_email"),
        migrations.RemoveField("tenant", "contact_phone"),
        migrations.RemoveField("tenantprovisioning", "email"),
        migrations.RemoveField("tenantprovisioning", "contact_phone"),
        migrations.RemoveField("tenantprovisioning", "ops_admin_email"),
    ]
