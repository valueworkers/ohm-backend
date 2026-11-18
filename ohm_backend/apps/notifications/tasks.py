from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.utils import timezone

from apps.platform.tasks import TenantTask

from .models import Notification

User = get_user_model()


@shared_task(base=TenantTask, bind=True, max_retries=3, default_retry_delay=60)
def send_email_task(self, to_email, subject, message):
    """Send notification email asynchronously."""
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[to_email],
            fail_silently=False,
        )
    except Exception as exc:
        raise self.retry(exc=exc)


@shared_task(base=TenantTask, bind=True, max_retries=3, default_retry_delay=60)
def send_push_task(self, user_id, title, message):
    """Send push notification asynchronously."""
    try:
        user = User.objects.get(id=user_id)
        # Check if push_notifications app or devices exist
        try:
            from push_notifications.models import APNSDevice, GCMDevice
            GCMDevice.objects.filter(user=user, active=True).send_message(message, title=title)
            APNSDevice.objects.filter(user=user, active=True).send_message(message, title=title)
        except ImportError:
            pass
    except Exception as exc:
        raise self.retry(exc=exc)


@shared_task(base=TenantTask)
def send_daily_digest():
    """Celery Beat task: email digest of unread notifications every morning."""
    yesterday = timezone.now() - timedelta(days=1)
    for user in User.objects.filter(is_active=True, email__isnull=False).exclude(email=""):
        unread = Notification.objects.filter(
            recipient=user, status__in=[Notification.Status.PENDING, Notification.Status.DELIVERED, Notification.Status.SENT], created_at__gte=yesterday
        ).count()
        if unread:
            send_email_task.delay(
                user.email,
                f"You have {unread} unread notification{'s' if unread > 1 else ''}",
                f"Hi {user.first_name or user.username},\n\nYou have {unread} unread notifications.\nLog in to check them.\n\nCheers,\nThe Team",
            )
