import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.template.loader import render_to_string
from django.utils import timezone

from .models import AccountNotification


logger = logging.getLogger(__name__)


def send_account_created_email(user_id: int) -> bool:
    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if not user or not user.email:
        return False

    notification, _ = AccountNotification.objects.get_or_create(user=user)
    if notification.creation_email_sent_at:
        return False

    try:
        subject = render_to_string(
            "registration/account_created_subject.txt", {"user": user}
        ).strip().replace("\n", " ")
        message = render_to_string(
            "registration/account_created_email.txt",
            {"user": user, "login_url": settings.ACCOUNT_LOGIN_URL},
        )
        sent = send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [user.email])
        if sent:
            notification.creation_email_sent_at = timezone.now()
            notification.last_error = ""
            notification.save(update_fields=["creation_email_sent_at", "last_error", "updated_at"])
            return True
    except Exception as exc:
        notification.last_error = str(exc)[:2000]
        notification.save(update_fields=["last_error", "updated_at"])
        logger.exception("Falha ao enviar confirmacao de conta para user_id=%s", user_id)
    return False


@receiver(post_save, sender=get_user_model())
def schedule_account_created_email(sender, instance, **kwargs):
    if not instance.email or not instance.is_active:
        return
    transaction.on_commit(lambda: send_account_created_email(instance.pk))
