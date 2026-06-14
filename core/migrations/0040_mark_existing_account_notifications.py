from django.db import migrations
from django.utils import timezone


def mark_existing_accounts(apps, schema_editor):
    User = apps.get_model("auth", "User")
    AccountNotification = apps.get_model("core", "AccountNotification")
    now = timezone.now()

    existing_user_ids = set(
        AccountNotification.objects.values_list("user_id", flat=True)
    )
    AccountNotification.objects.bulk_create(
        [
            AccountNotification(user_id=user_id, creation_email_sent_at=now)
            for user_id in User.objects.values_list("id", flat=True)
            if user_id not in existing_user_ids
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0039_seed_renovigi_public_catalog")]

    operations = [
        migrations.RunPython(mark_existing_accounts, migrations.RunPython.noop),
    ]
