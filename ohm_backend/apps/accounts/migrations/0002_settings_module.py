from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="email_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="UserSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("theme", models.CharField(default="system", max_length=10)),
                ("font_size", models.CharField(default="medium", max_length=10)),
                ("font_family", models.CharField(default="system", max_length=30)),
                ("notification_channels", models.JSONField(blank=True, default=dict)),
                ("notification_categories", models.JSONField(blank=True, default=dict)),
                ("marketing_consent_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="settings", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="EmailVerification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254)),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="email_verifications", to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
