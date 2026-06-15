from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.forms import MeteoRequestForm
from core.services.fdd.report_pdf import build_mismatch_pdf_report
from core.views.dashboard import paired_model_metrics
from core.models import (
    AccountNotification,
    InverterOperationalData,
    PlantMonitoringCredential,
    PVInverter,
    PVModule,
    PVPlant,
)


class HealthCheckTests(TestCase):
    def test_health_check_reports_database_status(self):
        response = self.client.get(reverse("healthz"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class DashboardMetricTests(TestCase):
    def test_paired_model_metrics_calculates_rmse_and_correlations(self):
        result = paired_model_metrics(
            measured=[100.0, 200.0, 300.0, None],
            modeled=[110.0, 190.0, 310.0, 400.0],
        )

        self.assertEqual(result["pairs"], 3)
        self.assertAlmostEqual(result["rmse"], 10.0)
        self.assertGreater(result["pearson_r"], 0.99)
        self.assertEqual(result["spearman_rho"], 1.0)

    def test_paired_model_metrics_handles_missing_or_constant_data(self):
        empty = paired_model_metrics([None], [None])
        constant = paired_model_metrics([5.0, 5.0], [10.0, 10.0])

        self.assertEqual(empty["pairs"], 0)
        self.assertIsNone(empty["rmse"])
        self.assertIsNone(constant["pearson_r"])
        self.assertIsNone(constant["spearman_rho"])


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

    def test_regular_user_cannot_access_another_users_plant_flows(self):
        self.client.force_login(self.user)

        protected_urls = [
            reverse("plants:detail", kwargs={"pk": self.other_plant.pk}),
            reverse("plants:cred_save", kwargs={"pk": self.other_plant.pk}),
            reverse("renovigi_console", kwargs={"pk": self.other_plant.pk}),
            reverse("opdata_list", kwargs={"pk": self.other_plant.pk}),
        ]

        for url in protected_urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_superuser_can_access_every_plant_workflow(self):
        admin = get_user_model().objects.create_superuser(
            username="site-admin",
            email="admin@example.com",
            password="Admin-test-pass-7291",
        )
        PlantMonitoringCredential.objects.create(
            plant=self.other_plant,
            provedor="RENOVIGI",
            username="monitor-user",
            password="monitor-password",
        )
        self.client.force_login(admin)

        plant_list = self.client.get(reverse("plants:list"))
        self.assertContains(plant_list, self.plant.nome)
        self.assertContains(plant_list, self.other_plant.nome)
        self.assertEqual(
            self.client.get(
                reverse("plants:detail", kwargs={"pk": self.other_plant.pk})
            ).status_code,
            200,
        )
        credential_response = self.client.get(
            reverse("plants:cred_save", kwargs={"pk": self.other_plant.pk})
        )
        self.assertRedirects(
            credential_response,
            f"{reverse('plants:detail', kwargs={'pk': self.other_plant.pk})}#credentials",
        )
        self.assertEqual(
            self.client.get(
                reverse("renovigi_console", kwargs={"pk": self.other_plant.pk})
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse("opdata_list", kwargs={"pk": self.other_plant.pk})
            ).status_code,
            200,
        )

    def test_operational_index_only_lists_accessible_plants(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("opdata_index"))

        self.assertContains(response, self.plant.nome)
        self.assertNotContains(response, self.other_plant.nome)

    def test_plant_list_filters_and_sorts_without_pagination_warnings(self):
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("plants:list"),
            {"q": "proprietario", "tz": "America/Sao_Paulo", "sort": "lat", "order": "desc"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.plant.nome)
        self.assertNotContains(response, self.other_plant.nome)

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
    def test_signup_page_is_available(self):
        response = self.client.get(reverse("signup"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Criar conta")

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
            nome="Belarmino",
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

    @override_settings(RENOVIGI_COMPANY_KEY="bnrl_frRFjEz8Mkn")
    def test_renovigi_console_displays_company_key(self):
        PlantMonitoringCredential.objects.create(
            plant=self.plant,
            provedor="RENOVIGI",
            username="monitor-user",
            password="monitor-password",
        )

        response = self.client.get(
            reverse("renovigi_console", kwargs={"pk": self.plant.pk})
        )

        self.assertContains(response, "bnrl_frRFjEz8Mkn")

    def test_credentials_link_opens_the_plant_credentials_section(self):
        response = self.client.get(
            reverse("plants:cred_save", kwargs={"pk": self.plant.pk})
        )

        self.assertRedirects(
            response,
            f"{reverse('plants:detail', kwargs={'pk': self.plant.pk})}#credentials",
        )

    def test_updating_credentials_without_password_preserves_saved_password(self):
        credential = PlantMonitoringCredential.objects.create(
            plant=self.plant,
            provedor="RENOVIGI",
            username="old-user",
            password="saved-secret",
        )

        response = self.client.post(
            reverse("plants:cred_save", kwargs={"pk": self.plant.pk}),
            {
                "provedor": "RENOVIGI",
                "username": "updated-user",
                "password": "",
            },
        )

        self.assertRedirects(
            response, reverse("renovigi_console", kwargs={"pk": self.plant.pk})
        )
        credential.refresh_from_db()
        self.assertEqual(credential.username, "updated-user")
        self.assertEqual(credential.password, "saved-secret")

    def test_operational_pages_show_saved_data_and_navigation(self):
        PlantMonitoringCredential.objects.create(
            plant=self.plant,
            provedor="RENOVIGI",
            username="monitor-user",
            password="monitor-password",
        )
        InverterOperationalData.objects.create(
            plant=self.plant,
            pn="Q0D20429837886",
            devcode="518",
            devaddr=1,
            sn="140205021B160186",
            ts_utc=timezone.now(),
            payload={"power": 1234},
        )

        detail = self.client.get(
            reverse("plants:detail", kwargs={"pk": self.plant.pk})
        )
        index = self.client.get(reverse("opdata_index"))
        data = self.client.get(
            reverse("opdata_list", kwargs={"pk": self.plant.pk})
        )

        self.assertContains(detail, reverse("renovigi_console", kwargs={"pk": self.plant.pk}))
        self.assertContains(detail, reverse("opdata_list", kwargs={"pk": self.plant.pk}))
        self.assertContains(index, self.plant.nome)
        self.assertContains(index, "1")
        self.assertContains(data, "Q0D20429837886")

    def test_operational_data_ignores_invalid_page_size(self):
        response = self.client.get(
            reverse("opdata_list", kwargs={"pk": self.plant.pk}),
            {"page_size": "invalid"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_size"], 200)

    def test_pv_dashboard_contains_model_fit_card_and_removes_old_panels(self):
        response = self.client.get(reverse("pv_dashboard"))

        self.assertContains(response, "chartPdcFit")
        self.assertContains(response, "pdcRmse")
        self.assertNotContains(response, "Diagrama de perdas (Sankey)")
        self.assertNotContains(response, "Timeline de persistência de falha")
        self.assertNotContains(response, "Resíduo vs Instabilidade")

    def test_pv_dashboard_empty_api_keeps_model_fit_contract(self):
        response = self.client.get(
            reverse("pv_dashboard_api_timeseries"),
            {
                "plant_id": self.plant.id,
                "start": "2026-06-12",
                "end": "2026-06-14",
                "source_oper": "ALL",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["series"]["p_dc_w"], [])
        self.assertEqual(payload["series"]["p_dc_model_w"], [])
        self.assertEqual(payload["kpis"]["p_dc_fit_pairs"], 0)
        self.assertIsNone(payload["kpis"]["p_dc_rmse_w"])
        self.assertIsNone(payload["kpis"]["p_dc_pearson_r"])
        self.assertIsNone(payload["kpis"]["p_dc_spearman_rho"])

    def test_mismatch_dashboard_contains_chapter_08_charts(self):
        response = self.client.get(reverse("mismatch_fdd"))

        self.assertContains(response, "chartResidualProfile")
        self.assertContains(response, "residualMatrixCanvas")
        self.assertContains(response, "fddFlowSvg")
        self.assertContains(response, "diagnosticHourCanvas")
        self.assertContains(response, "diagnosticMonthCanvas")
        self.assertContains(response, "reorderPostHeatmapSections")
        self.assertContains(response, "renderResidualCorrelationMatrix")
        self.assertContains(response, "renderFddSankey")
        self.assertContains(response, "vendor/chartjs/chart.umd.min.js")
        self.assertContains(
            response,
            '<details class="card glass span-12 advanced-card" id="advancedParamsCard">',
        )
        self.assertNotContains(response, 'id="advancedParamsCard" open')

    def test_home_operational_data_button_uses_system_style(self):
        response = self.client.get(reverse("home"))

        self.assertContains(response, "home-card-action")
        self.assertContains(response, "Abrir dados operativos")

    def test_mismatch_pdf_export_returns_branded_pdf(self):
        payload = {
            "ok": True,
            "range": {"start": "2026-06-12", "end": "2026-06-14"},
            "series": {"t_local": []},
            "thresholds": {"dt_minutes": 15},
            "sources": {},
            "versions": {},
        }
        params = SimpleNamespace(
            start=date(2026, 6, 12),
            end=date(2026, 6, 14),
            source_oper_raw=None,
        )

        with patch(
            "core.views.fdd._build_payload_from_request",
            return_value=(self.plant, params, payload),
        ):
            response = self.client.get(reverse("mismatch_fdd_export_pdf"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("mismatch_fdd_report_plant", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertGreater(len(response.content), 4_000)

    def test_pdf_builder_handles_empty_period(self):
        pdf = build_mismatch_pdf_report(
            plant_name="Belarmino",
            payload={
                "ok": True,
                "range": {"start": "2026-06-12", "end": "2026-06-14"},
                "series": {"t_local": []},
                "thresholds": {"dt_minutes": 15},
                "sources": {},
                "versions": {},
            },
            filters={"dt_minutes": 15},
            generated_at_local="2026-06-14 12:00:00 -03",
            user_label="renovigi-owner",
        )

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 4_000)
