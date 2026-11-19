from django_tenants.test.cases import TenantTestCase

from .models import Contact, FAQItem, FAQTopic


class ContentTests(TenantTestCase):
    @classmethod
    def setup_tenant(cls, tenant):
        tenant.name = "Content Provider"
        tenant.status = "active"

    def test_faq_creation(self):
        topic = FAQTopic.objects.create(topic="General")
        item = FAQItem.objects.create(topic=topic, question="What is OHM?", answer="OHM is an operating system.")
        self.assertEqual(topic.qanda.count(), 1)
        self.assertEqual(str(item), "What is OHM?")

    def test_contact_creation(self):
        contact = Contact.objects.create(
            display_name="Support",
            email="support@example.com",
            platform=Contact.Platform.WEBSITE,
        )
        self.assertEqual(str(contact), "Support")
