import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0003_alter_quote_created_by_alter_quoteevent_actor'),
        ('catalog', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='contactbooking',
            name='service',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='manual_bookings', to='catalog.service'),
        ),
        migrations.AddField(
            model_name='primaryorder',
            name='venue',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='primary_orders', to='catalog.venue'),
        ),
        migrations.AddField(
            model_name='primaryorder',
            name='service',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='primary_orders', to='catalog.service'),
        ),
        migrations.AddField(
            model_name='ternaryorder',
            name='venue',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='ternary_orders', to='catalog.venue'),
        ),
        migrations.AddField(
            model_name='ternaryorder',
            name='service',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='ternary_orders', to='catalog.service'),
        ),
        migrations.AddField(
            model_name='invoice',
            name='venue',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='billing_invoices', to='catalog.venue'),
        ),
        migrations.AddField(
            model_name='invoice',
            name='service',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='billing_invoices', to='catalog.service'),
        ),
        migrations.AddField(
            model_name='invoice',
            name='resource',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='billing_invoices', to='catalog.resource'),
        ),
        migrations.AddField(
            model_name='quote',
            name='venue',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='quotes', to='catalog.venue'),
        ),
        migrations.AddField(
            model_name='quote',
            name='service',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='quotes', to='catalog.service'),
        ),
        migrations.AddField(
            model_name='quote',
            name='resource',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='quotes', to='catalog.resource'),
        ),
    ]
