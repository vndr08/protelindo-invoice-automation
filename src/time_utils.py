"""Kebijakan waktu tunggal: simpan UTC ISO-8601, tampilkan Asia/Jakarta (WIB)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc
WIB = ZoneInfo("Asia/Jakarta")
WIB_DISPLAY_FORMAT = "%Y-%m-%d %H:%M WIB"


def utc_now() -> datetime:
    """Membuat timestamp sadar-timezone untuk seluruh write baru."""
    return datetime.now(UTC)


def utc_now_iso() -> str:
    """Menyimpan UTC dengan offset eksplisit agar tidak bergantung timezone komputer."""
    return utc_now().isoformat(timespec="seconds")


def parse_utc_timestamp(value: str | datetime | None) -> datetime | None:
    """Membaca timestamp baru maupun legacy lalu menormalkannya sebagai UTC aware.

    SQLite CURRENT_TIMESTAMP lama berbentuk naive ``YYYY-MM-DD HH:MM:SS``. SQLite
    mendefinisikan nilai itu sebagai UTC, sehingga asumsi ini aman untuk record legacy
    yang dibuat aplikasi. Timestamp naive dari sumber eksternal tetap perlu review.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_wib(value: str | datetime | None) -> datetime | None:
    """Mengonversi canonical/legacy UTC menjadi waktu Asia/Jakarta."""
    parsed = parse_utc_timestamp(value)
    return parsed.astimezone(WIB) if parsed is not None else None


def format_wib(value: str | datetime | None) -> str | None:
    """Format manusia yang konsisten untuk terminal dan dokumentasi."""
    local = to_wib(value)
    return local.strftime(WIB_DISPLAY_FORMAT) if local is not None else None


def excel_wib_datetime(value: str | datetime | None) -> datetime | None:
    """Membuat datetime naive lokal untuk Excel; zone ditunjukkan oleh format cell WIB."""
    local = to_wib(value)
    return local.replace(tzinfo=None) if local is not None else None
