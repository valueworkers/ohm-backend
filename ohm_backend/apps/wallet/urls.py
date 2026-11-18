from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "wallet"

router = DefaultRouter()
router.register(r"", views.WalletViewSet, basename="wallet")

urlpatterns = router.urls
