from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payroll", "0001_initial"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="salaryreport",
            constraint=models.CheckConstraint(
                condition=models.Q(("end_date__gte", models.F("start_date"))),
                name="salary_report_end_not_before_start",
            ),
        ),
    ]
