from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from .models import Notification, NotificationTemplate

User = get_user_model()


class NotificationTests(TenantTestCase):
    @classmethod
    def setup_tenant(cls, tenant):
        tenant.name = "Notification Provider"
        tenant.status = "active"

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email="u1@test.com", password="pwd")

    def test_notification_creation_and_mark_read(self):
        notif = Notification.objects.create(
            recipient=self.user,
            channel=Notification.Channel.IN_APP,
            category=Notification.Category.SYSTEM,
            title="Welcome",
            message="Welcome to OHM!",
        )
        self.assertFalse(notif.is_read)
        notif.mark_read()
        self.assertTrue(notif.is_read)
