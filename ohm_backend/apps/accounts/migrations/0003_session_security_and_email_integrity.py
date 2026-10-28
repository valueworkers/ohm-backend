import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.db.models.functions.text
import django.utils.timezone


def reject_case_variant_emails(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    collisions = (
        User.objects.annotate(normalized_email=django.db.models.functions.text.Lower("email"))
        .values("normalized_email")
        .annotate(row_count=models.Count("id"))
        .filter(row_count__gt=1)
    )
    examples = list(collisions.values_list("normalized_email", flat=True)[:10])
    if examples:
        raise RuntimeError(
            "Resolve duplicate case-insensitive account emails before applying this migration. "
            f"Examples: {', '.join(examples)}"
        )
    User.objects.update(email=django.db.models.functions.text.Lower("email"))


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_settings_module")]

    operations = [
        migrations.RunPython(reject_case_variant_emails, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("email"),
                name="accounts_user_email_ci_uniq",
            ),
        ),
        migrations.CreateModel(
            name="UserSession",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("refresh_jti", models.CharField(blank=True, max_length=255)),
                ("user_agent", models.CharField(blank=True, max_length=512)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sessions", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="PasswordResetToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="password_reset_tokens", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name="usersession",
            constraint=models.UniqueConstraint(fields=("refresh_jti",), condition=~models.Q(refresh_jti=""), name="accounts_user_session_jti_uniq"),
        ),
    ]
