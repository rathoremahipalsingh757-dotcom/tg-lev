"""Environment-based configuration for the Telegram shop bot."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    paymento_api_key: str
    paymento_ipn_secret: str
    paymento_ipn_url: str
    paymento_ipn_host: str
    paymento_ipn_port: int
    google_credentials: dict[str, str]
    google_sheet_id: str
    admin_telegram_id: int
    support_contact: str
    support_contact_secondary: str
    vouches_url: str
    reservation_minutes: int
    poll_interval_seconds: int
    max_quantity: int


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Required environment variable {name} is not set.")
    return value


def _load_google_credentials(value: str) -> dict[str, str]:
    candidate = value.strip()
    if candidate.startswith("{"):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ValueError("GOOGLE_SHEETS_CREDENTIALS_JSON is not valid JSON.") from exc
    else:
        credentials_path = Path(candidate).expanduser()
        try:
            parsed = json.loads(credentials_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(
                "GOOGLE_SHEETS_CREDENTIALS_JSON must be a JSON object or a readable file path."
            ) from exc
        except json.JSONDecodeError as exc:
            raise ValueError("The Google service-account file is not valid JSON.") from exc

    if not isinstance(parsed, dict):
        raise ValueError("Google service-account credentials must be a JSON object.")
    for key in ("type", "project_id", "private_key", "client_email", "token_uri"):
        if not parsed.get(key):
            raise ValueError(f"Google service-account credentials are missing {key!r}.")
    if parsed["type"] != "service_account":
        raise ValueError("Google credentials must be for a service account.")
    return parsed


def load_settings() -> Settings:
    load_dotenv()
    admin_id_raw = _required("ADMIN_TELEGRAM_ID")
    try:
        admin_id = int(admin_id_raw)
    except ValueError as exc:
        raise ValueError("ADMIN_TELEGRAM_ID must be an integer Telegram user ID.") from exc
    if admin_id <= 0:
        raise ValueError("ADMIN_TELEGRAM_ID must be a positive Telegram user ID.")

    try:
        reservation_minutes = int(os.getenv("RESERVATION_MINUTES", "20"))
        poll_interval_seconds = int(os.getenv("PAYMENT_POLL_INTERVAL_SECONDS", "15"))
        max_quantity = int(os.getenv("MAX_QUANTITY_PER_ORDER", "500"))
        paymento_ipn_port = int(
            os.getenv("PORT", os.getenv("PAYMENTO_IPN_PORT", "8080"))
        )
    except ValueError as exc:
        raise ValueError(
            "Reservation, polling, quantity, and Paymento IPN port settings must be integers."
        ) from exc
    if not 15 <= reservation_minutes <= 30:
        raise ValueError("RESERVATION_MINUTES must be between 15 and 30.")
    if poll_interval_seconds < 5:
        raise ValueError("PAYMENT_POLL_INTERVAL_SECONDS must be at least 5.")
    if not 1 <= max_quantity <= 500:
        raise ValueError("MAX_QUANTITY_PER_ORDER must be between 1 and 500.")
    if not 1 <= paymento_ipn_port <= 65535:
        raise ValueError("PAYMENTO_IPN_PORT must be between 1 and 65535.")

    paymento_ipn_url = _required("PAYMENTO_IPN_URL")
    ipn_url = urlsplit(paymento_ipn_url)
    if (
        ipn_url.scheme != "https"
        or not ipn_url.hostname
        or not ipn_url.path
        or ipn_url.query
        or ipn_url.fragment
        or ipn_url.username
        or ipn_url.password
    ):
        raise ValueError(
            "PAYMENTO_IPN_URL must be a public HTTPS URL with a path and no query or fragment."
        )

    return Settings(
        bot_token=_required("BOT_TOKEN"),
        paymento_api_key=_required("PAYMENTO_API_KEY"),
        paymento_ipn_secret=_required("PAYMENTO_IPN_SECRET"),
        paymento_ipn_url=paymento_ipn_url,
        paymento_ipn_host=os.getenv("PAYMENTO_IPN_HOST", "127.0.0.1").strip(),
        paymento_ipn_port=paymento_ipn_port,
        google_credentials=_load_google_credentials(
            _required("GOOGLE_SHEETS_CREDENTIALS_JSON")
        ),
        google_sheet_id=_required("GOOGLE_SHEET_ID"),
        admin_telegram_id=admin_id,
        support_contact=os.getenv("SUPPORT_CONTACT", "").strip(),
        support_contact_secondary=os.getenv("SUPPORT_CONTACT_2", "").strip(),
        vouches_url=os.getenv("VOUCHES_URL", "").strip(),
        reservation_minutes=reservation_minutes,
        poll_interval_seconds=poll_interval_seconds,
        max_quantity=max_quantity,
    )
