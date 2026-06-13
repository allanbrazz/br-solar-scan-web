from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.forms import MeteoRequestForm
from core.models import PVPlant


class HealthCheckTests(TestCase):
    def test_health_check_reports_database_status(self):
        response = self.client.get(reverse("healthz"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class AccessControlTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="owner", password="test-pass-123")
        self.other_user = user_model.objects.create_user(username="other", password="test-pass-123")
        self.plant = PVPlant.objects.create(
            owner=self.user,
            nome="Planta do proprietario",
            latitude=-30.0,
            longitude=-51.0,
            timezone="America/Sao_Paulo",
        )
        self.other_plant = PVPlant.objects.create(
            owner=self.other_user,
            nome="Planta de outro usuario",
            latitude=-29.0,
            longitude=-50.0,
            timezone="America/Sao_Paulo",
        )

    def test_module_catalog_requires_authentication(self):
        response = self.client.get(reverse("pvmodules:list"))

        self.assertRedirects(response, f"{reverse('login')}?next={reverse('pvmodules:list')}")

    def test_meteo_page_requires_authentication(self):
        response = self.client.get(reverse("open_meteo_view"))

        self.assertRedirects(response, f"{reverse('login')}?next={reverse('open_meteo_view')}")

    def test_meteo_form_only_lists_owned_plants(self):
        form = MeteoRequestForm(user=self.user)

        plant_ids = set(form.fields["plant"].queryset.values_list("id", flat=True))
        self.assertEqual(plant_ids, {self.plant.id})

    @override_settings(ALLOW_PUBLIC_SIGNUP=False)
    def test_signup_can_be_disabled_in_production(self):
        response = self.client.get(reverse("signup"))

        self.assertEqual(response.status_code, 404)
