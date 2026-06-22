from datetime import date, datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.forms import MeteoRequestForm
from core.services.fdd.dashboard_runtime import get_mismatch_backend_param_defaults
from core.services.fdd.param_catalog import DEFAULT_CONFIG_NAME
from core.services.fdd.report_pdf import build_mismatch_pdf_report
from core.services.pvmodule.villalva import VillalvaInput, extract_villalva_parameters
from core.views.dashboard import paired_model_metrics
from core.models import (
    AccountNotification,
    InverterOperationalData,
    MeteoRecord,
    MeteoSource,
    PlantMonitoringCredential,
    PlantDetectorConfiguration,
    PlantPerformanceRatio,
    PVPlantMergedRecord15m,
    PVInverter,
    PVModule,
    PVPlant,
    PVPlantDetails,
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


class VillalvaExtractionTests(TestCase):
    def test_villalva_extraction_converges_for_kc200gt_reference(self):
        data = VillalvaInput(
            isc_a=8.21,
            voc_v=32.9,
            vmp_v=26.3,
            imp_a=7.61,
            cells_in_series=54,
            temp_coeff_voc_pct_c=(-0.123 / 32.9 * 100.0),
            temp_coeff_isc_pct_c=(0.0032 / 8.21 * 100.0),
        )

        result = extract_villalva_parameters(
            data,
            alpha_min=1.3,
            alpha_max=1.3,
            alpha_step=0.1,
            rs_step=0.001,
            max_iterations=800,
        )

        self.assertTrue(result.best.converged)
        self.assertLess(result.best.error_pct, 0.01)
        self.assertGreater(result.best.rs_ohm, 0)
        self.assertGreater(result.best.rp_ohm, 100)
        self.assertAlmostEqual(result.best.diode_a, 1.3, places=3)


class ModuleVillalvaViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="villalva-owner",
            password="Strong-test-pass-7291",
        )
        self.client.force_login(self.user)
        self.payload = {
            "nome": "KC200GT TEST",
            "fabricante": "Kyocera",
            "pmp_w": "200.143",
            "vmp_v": "26.3",
            "imp_a": "7.61",
            "voc_v": "32.9",
            "isc_a": "8.21",
            "eficiencia_pct": "16.0",
            "power_tolerance": "",
            "num_celulas": "54",
            "temp_coeff_voc_pct_c": "-0.3739",
            "temp_coeff_isc_pct_c": "0.0390",
            "alpha_min": "1.3",
            "alpha_max": "1.3",
            "alpha_step": "0.1",
            "rs_step": "0.001",
            "max_iterations": "800",
            "atualizar_existente": "on",
        }

    def test_villalva_page_calculates_without_saving(self):
        response = self.client.post(
            reverse("pvmodules:villalva"),
            {**self.payload, "action": "calculate"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Resultado selecionado")
        self.assertContains(response, "Rs")
        self.assertFalse(
            PVModule.objects.filter(nome="KC200GT TEST", fabricante="Kyocera").exists()
        )

    def test_villalva_page_saves_module_with_extracted_parameters(self):
        response = self.client.post(
            reverse("pvmodules:villalva"),
            {**self.payload, "action": "save"},
        )

        module = PVModule.objects.get(nome="KC200GT TEST", fabricante="Kyocera")
        self.assertRedirects(response, reverse("pvmodules:detail", kwargs={"pk": module.pk}))
        self.assertGreater(module.rs_ohm, 0)
        self.assertGreater(module.rp_ohm, 0)
        self.assertAlmostEqual(float(module.diode_a), 1.3, places=3)


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


class ManualAndAuditViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="audit-owner", password="test-pass-123")
        self.other_user = user_model.objects.create_user(username="audit-other", password="test-pass-123")
        self.plant = PVPlant.objects.create(
            owner=self.user,
            nome="Planta auditada",
            latitude=-30.0,
            longitude=-51.0,
            timezone="America/Sao_Paulo",
        )
        self.other_plant = PVPlant.objects.create(
            owner=self.other_user,
            nome="Planta invisivel",
            latitude=-29.0,
            longitude=-50.0,
            timezone="America/Sao_Paulo",
        )

    def test_manual_page_requires_login_and_renders(self):
        url = reverse("user_manual")
        self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")

        self.client.force_login(self.user)
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manual de utilizacao")
        self.assertContains(response, "Funcionalidades principais")

    def test_meteorology_plant_selector_uses_stable_route_and_switches_owned_plant(self):
        selected = PVPlant.objects.create(
            owner=self.user,
            nome="Segunda planta meteorologica",
            latitude=-31.0,
            longitude=-52.0,
            timezone="America/Sao_Paulo",
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("open_meteo_view"),
            {
                "plant": selected.pk,
                "start_date": "2026-06-01",
                "end_date": "2026-06-02",
                "interval_min": "60",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["meteo_summary"]["plant"], selected)
        self.assertContains(response, f'action="{reverse("open_meteo_view")}"')
        self.assertContains(response, "selectionUrl.search = params.toString()")

    def test_audit_list_respects_plant_access_and_exports_csv(self):
        self.client.force_login(self.user)
        MeteoRecord.objects.create(
            plant=self.plant,
            source=MeteoSource.OPENMETEO,
            dataset_model="best_match",
            ts_utc=datetime(2025, 1, 1, 12, 0, tzinfo=dt_timezone.utc),
            interval_min=15,
            ghi=800,
            gti=760,
        )
        MeteoRecord.objects.create(
            plant=self.other_plant,
            source=MeteoSource.OPENMETEO,
            dataset_model="best_match",
            ts_utc=datetime(2025, 1, 1, 12, 0, tzinfo=dt_timezone.utc),
            interval_min=15,
            ghi=900,
        )

        response = self.client.get(reverse("audit_records"), {"dataset": "meteo"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.plant.nome)
        self.assertNotContains(response, self.other_plant.nome)

        export = self.client.get(reverse("audit_records"), {"dataset": "meteo", "action": "export"})
        self.assertEqual(export.status_code, 200)
        self.assertIn("text/csv", export["Content-Type"])
        content = export.content.decode("utf-8-sig")
        self.assertIn("Planta auditada", content)
        self.assertNotIn("Planta invisivel", content)

    def test_audit_can_create_edit_and_delete_meteo_record(self):
        self.client.force_login(self.user)
        create_response = self.client.post(
            reverse("audit_record_create", kwargs={"dataset": "meteo"}),
            {
                "plant": self.plant.pk,
                "source": MeteoSource.OPENMETEO,
                "source_endpoint": "",
                "dataset_model": "manual_test",
                "data_typology": "OTHER",
                "ts_utc": "2025-01-01T12:15:00",
                "interval_min": "15",
                "ghi": "700",
                "dni": "",
                "dhi": "",
                "gti": "680",
                "temp_air": "25",
                "wind_speed": "",
                "rh": "",
                "pressure": "",
                "meteo_qc_score": "",
            },
        )
        self.assertEqual(create_response.status_code, 302)
        record = MeteoRecord.objects.get(plant=self.plant, dataset_model="manual_test")
        self.assertEqual(record.ghi, 700)

        edit_response = self.client.post(
            reverse("audit_record_edit", kwargs={"dataset": "meteo", "pk": record.pk}),
            {
                "plant": self.plant.pk,
                "source": MeteoSource.OPENMETEO,
                "source_endpoint": "",
                "dataset_model": "manual_test",
                "data_typology": "OTHER",
                "ts_utc": "2025-01-01T12:15:00",
                "interval_min": "15",
                "ghi": "725",
                "dni": "",
                "dhi": "",
                "gti": "690",
                "temp_air": "25",
                "wind_speed": "",
                "rh": "",
                "pressure": "",
                "meteo_qc_score": "",
            },
        )
        self.assertEqual(edit_response.status_code, 302)
        record.refresh_from_db()
        self.assertEqual(record.ghi, 725)

        delete_response = self.client.post(
            f"{reverse('audit_records')}?dataset=meteo",
            {"selected": [record.pk], "action": "delete_selected"},
        )
        self.assertEqual(delete_response.status_code, 302)
        self.assertFalse(MeteoRecord.objects.filter(pk=record.pk).exists())

    def test_audit_can_render_merged_records(self):
        self.client.force_login(self.user)
        PVPlantMergedRecord15m.objects.create(
            plant=self.plant,
            source_oper="SHINEMONITOR",
            source_meteo="OPENMETEO",
            ts_utc=datetime(2025, 1, 1, 12, 0, tzinfo=dt_timezone.utc),
            interval_min=15,
            p_ac_w=5000,
            gti=780,
        )

        response = self.client.get(reverse("audit_records"), {"dataset": "merged"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SHINEMONITOR")
        self.assertContains(response, "OPENMETEO")

    def test_meteo_csv_upload_imports_user_csv_records(self):
        self.client.force_login(self.user)
        csv_file = SimpleUploadedFile(
            "meteo.csv",
            (
                b"ts_utc,ghi,poa,temp_air\n"
                b"2025-01-01T12:00:00Z,800,760,25\n"
                b"2025-01-01T12:05:00Z,810,770,25.4\n"
            ),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("open_meteo_view"),
            {
                "action": "upload_csv",
                "plant": self.plant.pk,
                "arquivo": csv_file,
                "interval_min": "5",
                "delimiter": ",",
                "decimal_separator": ".",
                "timestamp_col": "ts_utc",
                "timestamp_timezone": "UTC",
                "dayfirst": "on",
                "dataset_model": "Estacao local",
                "data_typology": "MEASURED",
                "update_existing": "on",
                "ghi_col": "ghi",
                "gti_col": "poa",
                "temp_air_col": "temp_air",
            },
        )

        self.assertEqual(response.status_code, 302)
        records = MeteoRecord.objects.filter(plant=self.plant, source=MeteoSource.USER_CSV).order_by("ts_utc")
        self.assertEqual(records.count(), 2)
        self.assertEqual(records.first().interval_min, 5)
        self.assertEqual(records.first().gti, 760)

    def test_merge_uses_selected_user_csv_meteo_source(self):
        self.client.force_login(self.user)
        with patch("core.views.juntar.build_plant_merged_dataset") as build_mock:
            build_mock.return_value = SimpleNamespace(stats={}, df15=SimpleNamespace(empty=True), df_hour=SimpleNamespace(empty=True))
            response = self.client.post(
                reverse("merge_run_view"),
                {
                    "plant": self.plant.pk,
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-01",
                    "source_oper": "SHINEMONITOR",
                    "source_meteo": MeteoSource.USER_CSV,
                    "time_shift_mode": "none",
                    "time_shift_target": "operational",
                    "time_shift_manual_minutes": "0",
                    "time_shift_max_abs_minutes": "120",
                    "time_shift_step_minutes": "15",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(build_mock.called)
        self.assertEqual(build_mock.call_args.kwargs["fetch_cfg"].meteo_source, MeteoSource.USER_CSV)


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

    def _seed_temperature_corrected_pr_fixture(self):
        module = PVModule.objects.create(
            nome="TEST-PR-TEMP",
            fabricante="SolarScan Tests",
            pmp_w="500.00",
            vmp_v="41.000",
            imp_a="12.200",
            voc_v="49.000",
            isc_a="13.000",
            eficiencia_pct="21.00",
            power_tolerance="",
            num_celulas=144,
            temp_coeff_voc_pct_c="-0.300",
            temp_coeff_isc_pct_c="0.040",
            rs_ohm="0.1000",
            rp_ohm="500.000",
            diode_a="1.300",
        )
        PVPlantDetails.objects.create(
            plant=self.plant,
            module=module,
            strings_count=1,
            modules_per_string=10,
            modules_total=10,
            k_sys="0.900",
            noct_c="45.00",
        )
        base = datetime(2026, 6, 12, 12, 0, tzinfo=dt_timezone.utc)
        for idx in range(8):
            PVPlantMergedRecord15m.objects.create(
                plant=self.plant,
                source_oper="SHINEMONITOR",
                source_meteo="OPENMETEO",
                ts_utc=base + timedelta(minutes=15 * idx),
                interval_min=15,
                p_ac_w=3600.0,
                p_dc_w=4200.0,
                v_dc_v=410.0,
                i_dc_a=10.2,
                v_ac_v=228.0 + idx,
                i_ac_a=15.6,
                freq_hz=60.03,
                alarm_code=7,
                alarm_sev=2,
                e_ac_wh_15=900.0,
                gti=800.0,
                ghi=760.0,
                temp_air=28.0,
                flag_meteo_missing=False,
                flag_inv_missing=False,
            )

    def test_renovigi_payload_extractor_reads_grid_frequency_and_alarm(self):
        from core.services.series_juntar.timeseries_io import _extract_payload

        metrics = _extract_payload(
            "RENOVIGI",
            {
                "Potência ativa total": "3.600",
                "Tensão Fase A": "228,5",
                "Corrente Fase A": "15,6",
                "Frequência da rede": "60,03",
                "Código do alarme": "7",
            },
        )

        self.assertAlmostEqual(metrics["freq_hz"], 60.03)
        self.assertEqual(metrics["alarm_code"], 7.0)
        self.assertEqual(metrics["alarm_sev"], 2.0)

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
        self.assertContains(response, "residualMatrixGrid")
        self.assertContains(response, "fddFlowSvg")
        self.assertContains(response, "diagnosticHourGrid")
        self.assertContains(response, "diagnosticMonthGrid")
        self.assertContains(response, "reorderPostHeatmapSections")
        self.assertContains(response, "renderResidualCorrelationMatrix")
        self.assertContains(response, "renderFddSankey")
        self.assertContains(response, "vendor/chartjs/chart.umd.min.js")
        self.assertContains(response, "prTempCard")
        self.assertContains(response, "chartPrTemp")
        self.assertContains(response, "data-pr-temp-api")
        self.assertContains(response, "basicParamHelpData")
        self.assertContains(response, "help-dot")
        self.assertContains(response, "chartDetailAcVoltageDay")
        self.assertContains(response, "modelFitCard")
        self.assertContains(response, "chartModelFit")
        self.assertContains(response, "modelFitPearson")
        self.assertContains(response, "implementationExplanationCard")
        self.assertContains(response, "[modelFit, validation, explanation]")
        self.assertContains(response, "Frequência da rede [Hz]")
        self.assertContains(response, "Código do alarme")
        self.assertContains(response, "Score do evento residual")
        self.assertContains(response, "translatedToken")
        self.assertContains(
            response,
            '<details class="card glass span-12 advanced-card" id="advancedParamsCard">',
        )
        self.assertNotContains(response, 'id="advancedParamsCard" open')

    def test_mismatch_fdd_api_exposes_ac_frequency_and_alarm_fields(self):
        self._seed_temperature_corrected_pr_fixture()

        response = self.client.get(
            reverse("mismatch_fdd_api"),
            {
                "plant_id": self.plant.pk,
                "start": "2026-06-12",
                "end": "2026-06-12",
                "source_oper": "ALL",
                "source_meteo": "OPENMETEO",
                "min_baseline_points": "4",
                "rca_min_baseline_points": "4",
                "stable_window_points": "2",
                "shading_window_points": "2",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn(60.03, payload["series"]["freq_hz"])
        self.assertIn(7, payload["series"]["alarm_code"])
        self.assertIn(2, payload["series"]["alarm_sev"])
        self.assertIn("model_fit", payload)
        self.assertIn("p_dc", payload["model_fit"])
        self.assertEqual(payload["model_fit"]["p_dc"]["pairs"], 8)
        self.assertIsNotNone(payload["model_fit"]["p_dc"]["rmse"])

        first_dump = next(iter(payload["dump_by_tkey"].values()))
        self.assertEqual(first_dump["chosen_total"]["alarm_code"], 7)
        self.assertEqual(first_dump["chosen_total"]["alarm_sev"], 2)
        self.assertAlmostEqual(first_dump["chosen_total"]["freq_hz"], 60.03)

    def test_mismatch_pr_temp_api_calculates_and_persists_monthly_ratio(self):
        self._seed_temperature_corrected_pr_fixture()

        response = self.client.get(
            reverse("mismatch_fdd_pr_temp_api"),
            {
                "plant_id": self.plant.pk,
                "start": "2026-06-12",
                "end": "2026-06-12",
                "source_oper": "ALL",
                "source_meteo": "OPENMETEO",
                "period": "monthly",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["period"], "monthly")
        self.assertEqual(len(payload["series"]), 1)
        self.assertGreater(payload["series"][0]["performance_ratio"], 0.0)
        self.assertGreater(payload["series"][0]["raw_performance_ratio"], 0.0)

        saved = PlantPerformanceRatio.objects.get(
            plant=self.plant,
            source_oper="ALL",
            source_meteo="OPENMETEO",
            period="monthly",
        )
        self.assertAlmostEqual(
            saved.performance_ratio,
            payload["series"][0]["performance_ratio"],
            places=6,
        )
        self.assertEqual(saved.valid_samples_count, 8)
        self.assertEqual(saved.meta["temperature_model"], "NOCT")

    def test_c18_detector_defaults_are_exposed_consistently(self):
        defaults = get_mismatch_backend_param_defaults()
        response = self.client.get(reverse("mismatch_fdd"))

        self.assertEqual(DEFAULT_CONFIG_NAME, "C18_estabilidade_restritiva")
        self.assertEqual(defaults["warn_abs"], 0.47)
        self.assertEqual(defaults["fault_abs"], 0.95)
        self.assertEqual(defaults["gpoa_gate"], 250.0)
        self.assertEqual(defaults["pmin_w"], 300.0)
        self.assertEqual(defaults["min_baseline_points"], 48)
        self.assertContains(response, "C18_estabilidade_restritiva")
        self.assertContains(response, "Gestão de configurações")

    def test_detector_configuration_crud_is_scoped_to_plant(self):
        url = reverse("mismatch_fdd_configurations_api")
        create = self.client.post(
            url,
            data={
                "action": "save",
                "plant_id": self.plant.pk,
                "name": "Configuração Belarmino",
                "config": {"warn_abs": 0.52, "persist": True},
            },
            content_type="application/json",
        )

        self.assertEqual(create.status_code, 200)
        saved = PlantDetectorConfiguration.objects.get(plant=self.plant)
        self.assertEqual(saved.config["warn_abs"], 0.52)
        self.assertEqual(saved.config["detector_version"], "mismatch_runtime_v1")

        default = self.client.post(
            url,
            data={"action": "set_default", "plant_id": self.plant.pk, "configuration_id": saved.pk},
            content_type="application/json",
        )
        self.assertEqual(default.status_code, 200)
        saved.refresh_from_db()
        self.assertTrue(saved.is_default)

        listing = self.client.get(url, {"plant_id": self.plant.pk})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["configurations"][0]["name"], "Configuração Belarmino")

    def test_meteorology_page_manages_selected_plant_data(self):
        MeteoRecord.objects.create(
            plant=self.plant,
            source=MeteoSource.OPENMETEO,
            ts_utc=timezone.now(),
            interval_min=60,
            dataset_model="best_match",
            ghi=500.0,
        )
        response = self.client.get(
            reverse("open_meteo_view"),
            {"plant": self.plant.pk, "start_date": timezone.now().date(), "end_date": timezone.now().date(), "interval_min": 60},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Gestão dos dados importados")
        self.assertContains(response, "Registros salvos")
        self.assertEqual(response.context["meteo_summary"]["total"], 1)

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


class GrowattWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="growatt-owner", password="Strong-test-pass-7291"
        )
        self.plant = PVPlant.objects.create(
            owner=self.user,
            nome="Planta Growatt",
            latitude=-34.9,
            longitude=-56.2,
            timezone="America/Montevideo",
        )
        self.client.force_login(self.user)

    def _credential(self):
        return PlantMonitoringCredential.objects.create(
            plant=self.plant,
            provedor="GROWATT",
            username="shine-user",
            password="saved-password",
            growatt_plant_id="10225508",
            growatt_device_sn="INV-TEST-001",
            growatt_device_type="1",
            growatt_datalogger_sn="DL-TEST-001",
        )

    def test_saving_growatt_credentials_redirects_to_console(self):
        response = self.client.post(
            reverse("plants:cred_save", kwargs={"pk": self.plant.pk}),
            {
                "provedor": "GROWATT",
                "username": "shine-user",
                "password": "saved-password",
            },
        )

        self.assertRedirects(
            response,
            reverse("plants:growatt_console", kwargs={"pk": self.plant.pk}),
        )

    def test_growatt_console_and_operational_index_are_integrated(self):
        self._credential()

        detail = self.client.get(reverse("plants:detail", kwargs={"pk": self.plant.pk}))
        console = self.client.get(
            reverse("plants:growatt_console", kwargs={"pk": self.plant.pk})
        )
        index = self.client.get(reverse("opdata_index"))

        self.assertContains(detail, "Adquirir dados (Growatt)")
        self.assertContains(console, "Growatt / ShinePhone")
        self.assertContains(index, "Adquirir Growatt")

    def test_growatt_client_normalizes_discovery_and_paginates_history(self):
        from core.services.dados_inversor.growatt_client import GrowattClient

        class FakeLegacyApi:
            def __init__(self, **kwargs):
                self.server_url = ""

            def login(self, username, password):
                return {
                    "success": True,
                    "userId": "user-1",
                    "user": {"token": "temporary-token", "timeZone": 8},
                }

            def plant_list(self, user_id):
                return {
                    "success": True,
                    "data": [{"plantId": "plant-1", "plantName": "Usina A"}],
                }

            def device_list(self, plant_id):
                return [
                    {
                        "deviceSn": "INV-1",
                        "datalogSn": "DL-1",
                        "deviceType": "inverter",
                        "type": "1",
                        "deviceStatus": 1,
                    }
                ]

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class FakeHttpSession:
            def __init__(self):
                self.calls = []

            def request(self, method, url, params=None, data=None, timeout=None):
                request_data = params or data
                self.calls.append((method, url, request_data, timeout))
                page = int(request_data["page"])
                if page == 1:
                    rows = [
                        {"time": f"2026-06-21 10:{index:02d}:00", "power": index}
                        for index in range(100)
                    ]
                else:
                    rows = [{"time": "2026-06-21 08:00:00", "power": 101}]
                return FakeResponse(
                    {
                        "error_code": 0,
                        "error_msg": "",
                        "data": {
                            "count": 101,
                            "datas": rows,
                            "datalogger_sn": "DL-1",
                        },
                    }
                )

        fake_http = FakeHttpSession()

        class FakeOpenApi:
            def __init__(self, token):
                self.token = token
                self.api_url = ""
                self.session = fake_http

            def _get_url(self, path):
                return f"{self.api_url}{path}"

        client = GrowattClient(
            "user",
            "password",
            login_base_url="https://login.example/",
            openapi_base_url="https://api.example/v1/",
            api_factory=FakeLegacyApi,
            openapi_factory=FakeOpenApi,
        )

        self.assertEqual(client.list_plants()[0]["plant_id"], "plant-1")
        self.assertEqual(client.list_devices("plant-1")[0]["device_sn"], "INV-1")
        result = client.fetch_history(
            device_sn="INV-1",
            device_type="1",
            start_day=date(2026, 6, 21),
            end_day=date(2026, 6, 21),
        )

        self.assertEqual(result["meta"]["pages"], 2)
        self.assertEqual(result["meta"]["datalogger_sn"], "DL-1")
        self.assertEqual(len(result["rows"]), 101)
        self.assertEqual(fake_http.calls[0][0], "GET")
        self.assertIn("device/inverter/data", fake_http.calls[0][1])
        self.assertEqual(client.api.server_url, "https://login.example/")

    def test_growatt_sync_is_idempotent_and_payload_feeds_merge_contract(self):
        from core.services.dados_inversor.growatt_ingest import sync_growatt_operational_data
        from core.services.series_juntar.timeseries_io import FetchConfig, fetch_inverter_df

        credential = self._credential()

        class FakeClient:
            def fetch_history(self, **kwargs):
                return {
                    "rows": [
                        {
                            "time": "2026-06-21 12:00:00",
                            "power": 3600,
                            "ppv": 3900,
                            "vpv1": 320,
                            "ipv1": 6,
                            "vpv2": 310,
                            "ipv2": 6,
                            "vac1": 228,
                            "vac2": 229,
                            "vac3": 227,
                            "iac1": 15.6,
                            "iac2": 15.5,
                            "iac3": 15.7,
                            "fac": 60.02,
                            "faultCode1": 0,
                            "warnCode": 0,
                            "status": 1,
                        },
                        {"time": "2026-06-21 12:05:00", "power": 3700, "fac": 60.01},
                    ],
                    "meta": {"datalogger_sn": "DL-TEST-001", "pages": 1, "chunks": 1},
                }

        kwargs = {
            "plant": self.plant,
            "cred": credential,
            "username": credential.username,
            "password": credential.password,
            "start_day": date(2026, 6, 21),
            "end_day": date(2026, 6, 21),
            "client": FakeClient(),
        }
        first = sync_growatt_operational_data(**kwargs)
        second = sync_growatt_operational_data(**kwargs)

        self.assertEqual(first["inserted"], 2)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["updated"], 2)
        self.assertEqual(
            InverterOperationalData.objects.filter(plant=self.plant, provedor="GROWATT").count(),
            2,
        )

        start = datetime(2026, 6, 21, 11, 55, tzinfo=dt_timezone.utc)
        end = datetime(2026, 6, 21, 12, 10, tzinfo=dt_timezone.utc)
        frame = fetch_inverter_df(
            plant=self.plant,
            dt_start_utc=start,
            dt_end_utc=end,
            cfg=FetchConfig(inverter_provider="GROWATT"),
        )
        self.assertEqual(len(frame), 2)
        self.assertAlmostEqual(frame.iloc[0]["p_ac_w"], 3600.0)
        self.assertAlmostEqual(frame.iloc[0]["p_dc_w"], 3900.0)
        self.assertAlmostEqual(frame.iloc[0]["freq_hz"], 60.02)
        self.assertEqual(frame.iloc[0]["alarm_code"], 0.0)

    def test_growatt_payload_extractor_translates_alarm_and_three_phase_values(self):
        from core.services.series_juntar.timeseries_io import _extract_payload

        metrics = _extract_payload(
            "GROWATT",
            {
                "power": 4200,
                "ppv": 4550,
                "vpv1": 320,
                "ipv1": 7,
                "vpv2": 310,
                "ipv2": 7,
                "vac1": 228,
                "vac2": 229,
                "vac3": 227,
                "iac1": 18,
                "iac2": 18.2,
                "iac3": 17.8,
                "fac": 59.99,
                "faultCode1": 23,
                "status": 3,
            },
        )

        self.assertEqual(metrics["p_ac_w"], 4200.0)
        self.assertAlmostEqual(metrics["v_ac_v"], 228.0)
        self.assertAlmostEqual(metrics["i_ac_a"], 18.0)
        self.assertAlmostEqual(metrics["freq_hz"], 59.99)
        self.assertEqual(metrics["alarm_code"], 23.0)
        self.assertEqual(metrics["alarm_sev"], 2.0)

    def test_operational_list_filters_growatt_provider(self):
        now = timezone.now()
        common = {
            "plant": self.plant,
            "pn": "PN",
            "devcode": "TYPE_1",
            "devaddr": 1,
            "sn": "INV",
            "ts_utc": now,
        }
        InverterOperationalData.objects.create(
            **common, provedor="GROWATT", payload={"source": "growatt"}
        )
        InverterOperationalData.objects.create(
            **{**common, "sn": "REN", "ts_utc": now + timedelta(seconds=1)},
            provedor="RENOVIGI",
            payload={"source": "renovigi"},
        )

        response = self.client.get(
            reverse("opdata_list", kwargs={"pk": self.plant.pk}),
            {"provider": "GROWATT"},
        )

        self.assertEqual(response.context["filtered_count"], 1)
        self.assertContains(response, "growatt")
        self.assertNotContains(response, "renovigi")
