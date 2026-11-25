"""Legacy care-module endpoints kept for existing clients."""

from rest_framework.routers import DefaultRouter

from .views import LocationViewSet, PackageViewSet, PatientDocumentViewSet

router = DefaultRouter()
router.register(r"locations", LocationViewSet, basename="care-location")
router.register(r"packages", PackageViewSet, basename="care-package")
router.register(r"patient-documents", PatientDocumentViewSet, basename="care-patient-document")

urlpatterns = router.urls
