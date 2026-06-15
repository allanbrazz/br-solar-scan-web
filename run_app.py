import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

from dotenv import load_dotenv


APP_NAME = "BrazSolarScan"
HOST = "127.0.0.1"


def _bundle_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def _user_data_dir() -> Path:
    base = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _find_free_port(start: int = 8765, limit: int = 100) -> int:
    for port in range(start, start + limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError("Nenhuma porta local disponivel para iniciar o sistema.")


def _load_desktop_environment(bundle_dir: Path, data_dir: Path) -> None:
    executable_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else bundle_dir
    for candidate in (data_dir / "desktop.env", executable_dir / "desktop.env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def _configure_environment(port: int) -> Path:
    bundle_dir = _bundle_dir()
    data_dir = _user_data_dir()
    _load_desktop_environment(bundle_dir, data_dir)
    local_url = f"http://{HOST}:{port}/"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_ENV", "desktop")
    os.environ.setdefault("DJANGO_DEBUG", "false")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", f"{HOST},localhost")
    os.environ.setdefault(
        "DJANGO_CSRF_TRUSTED_ORIGINS",
        f"http://{HOST}:{port},http://localhost:{port}",
    )
    os.environ.setdefault("DJANGO_ALLOW_PUBLIC_SIGNUP", "true")
    os.environ.setdefault("RENOVIGI_COMPANY_KEY", "bnrl_frRFjEz8Mkn")
    os.environ.setdefault("ACCOUNT_LOGIN_URL", f"{local_url}accounts/login/")
    os.environ.setdefault("DJANGO_DB_NAME", str(data_dir / "braz-solar-scan.sqlite3"))
    os.environ.setdefault("SOLARCONTROL_DATA_DIR", str(data_dir / "data"))
    os.environ.setdefault("DJANGO_MEDIA_ROOT", str(data_dir / "media"))
    os.environ.setdefault("DJANGO_STATIC_ROOT", str(bundle_dir / "staticfiles"))
    os.environ.setdefault(
        "DJANGO_EMAIL_BACKEND",
        "django.core.mail.backends.console.EmailBackend",
    )
    return data_dir


def _open_browser_when_ready(url: str, log_path: Path) -> None:
    health_url = f"{url}healthz/"
    for _ in range(120):
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url, new=1)
                    return
        except Exception:
            time.sleep(0.25)
    log_path.write_text(
        f"O servidor iniciou, mas o navegador nao abriu automaticamente. Acesse {url}\n",
        encoding="utf-8",
    )


def main() -> None:
    preferred_port = int(os.getenv("BRAZ_SOLAR_PORT", "8765"))
    port = _find_free_port(start=preferred_port)
    data_dir = _configure_environment(port)
    log_path = data_dir / "desktop.log"

    try:
        import django

        django.setup()

        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from waitress import create_server

        call_command("migrate", interactive=False, verbosity=0)
        application = get_wsgi_application()
        url = f"http://{HOST}:{port}/"
        server = create_server(
            application,
            host=HOST,
            port=port,
            threads=2,
            channel_timeout=90,
            cleanup_interval=20,
        )

        if os.getenv("BRAZ_SOLAR_OPEN_BROWSER", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }:
            threading.Thread(
                target=_open_browser_when_ready,
                args=(url, log_path),
                daemon=True,
            ).start()

        print(f"Braz Solar Scan iniciado em {url}")
        print("Feche esta janela ou pressione Ctrl+C para encerrar.")
        try:
            server.run()
        except KeyboardInterrupt:
            server.close()
    except Exception:
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
