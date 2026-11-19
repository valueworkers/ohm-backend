from rest_framework.routers import DefaultRouter

from .views import ContactViewSet, FAQViewSet, VideoViewSet

app_name = "content"

router = DefaultRouter()
router.register(r"faq", FAQViewSet, basename="faq")
router.register(r"video", VideoViewSet, basename="content-video")
router.register(r"contacts", ContactViewSet, basename="contact")

urlpatterns = router.urls
