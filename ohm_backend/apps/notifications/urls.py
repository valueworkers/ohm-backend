from django.urls import include, path
from rest_framework import routers

from .views import (
    NotificationSubscriberView,
    NotificationTemplateViewSet,
    NotificationViewSet,
)

app_name = "notifications"

router = routers.DefaultRouter()
router.register(r"templates", NotificationTemplateViewSet, basename="notification-template")
router.register(r"", NotificationViewSet, basename="notification")

urlpatterns = [
    path("subscriber-notifications/", NotificationSubscriberView.as_view(), name="subscriber-notifications"),
    path("", include(router.urls)),
]
