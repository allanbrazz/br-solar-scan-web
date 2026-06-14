from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from core.forms import MeteoRequestForm
from core.models import AccountNotification, PVInverter, PVModule, PVPlant


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


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="no-reply@brazsolarscan.test",
    ACCOUNT_LOGIN_URL="https://example.test/accounts/login/",
)
class AccountEmailTests(TestCase):
    @override_settings(ALLOW_PUBLIC_SIGNUP=True)
    def test_signup_saves_email_and_sends_creation_confirmation(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("signup"),
                {
                    "username": "new-owner",
                    "email": "owner@example.com",
                    "password1": "Strong-test-pass-7291",
                    "password2": "Strong-test-pass-7291",
                },
            )

        self.assertRedirects(response, reverse("login"))
        user = get_user_model().objects.get(username="new-owner")
        self.assertEqual(user.email, "owner@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@example.com"])
        self.assertIn("https://example.test/accounts/login/", mail.outbox[0].body)
        self.assertIsNotNone(
            AccountNotification.objects.get(user=user).creation_email_sent_at
        )

    def test_admin_can_create_user_with_email_and_trigger_confirmation(self):
        admin = get_user_model().objects.create_superuser(
            username="site-admin",
            email="",
            password="Admin-test-pass-7291",
        )
        self.client.force_login(admin)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("admin:auth_user_add"),
                {
                    "username": "plant-owner",
                    "email": "plant-owner@example.com",
                    "password1": "Strong-test-pass-7291",
                    "password2": "Strong-test-pass-7291",
                    "_save": "Save",
                },
            )

        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(username="plant-owner")
        self.assertEqual(user.email, "plant-owner@example.com")
        self.assertEqual(len(mail.outbox), 1)

    def test_password_reset_sends_email_with_reset_link(self):
        user = get_user_model().objects.create_user(
            username="reset-owner",
            email="",
            password="Strong-test-pass-7291",
        )
        get_user_model().objects.filter(pk=user.pk).update(email="reset@example.com")

        response = self.client.post(
            reverse("password_reset"), {"email": "reset@example.com"}
        )

        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/accounts/reset/", mail.outbox[0].body)


class RenovigiWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="renovigi-owner", password="Strong-test-pass-7291"
        )
        self.plant = PVPlant.objects.create(
            owner=self.user,
            nome="Berlarmino",
            latitude=-5.685,
            longitude=-35.287,
            timezone="America/Maceio",
        )
        self.client.force_login(self.user)

    def test_public_renovigi_catalog_is_seeded(self):
        self.assertTrue(
            PVModule.objects.filter(
                fabricante="Renovigi", nome="RENO-R 550"
            ).exists()
        )
        self.assertTrue(
            PVInverter.objects.filter(
                fabricante="Renovigi", modelo="RENO-5K-PLUS"
            ).exists()
        )

    def test_saving_renovigi_credentials_redirects_to_console(self):
        response = self.client.post(
            reverse("plants:cred_save", kwargs={"pk": self.plant.pk}),
            {
                "provedor": "RENOVIGI",
                "username": "BELARMINO",
                "password": "temporary-test-secret",
            },
        )

        self.assertRedirects(
            response, reverse("renovigi_console", kwargs={"pk": self.plant.pk})
        )
