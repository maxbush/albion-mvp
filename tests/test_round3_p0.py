"""Round 3 (MASTER_PLAN v3) — E2E-проверки P0-фиксов.

P0.1: _format_dual_time — суффикс [+Nч к London] реально считается
P0.5: naive start_time трактуется как Europe/London (не UTC)
"""

from datetime import datetime, timezone

from src.workflows.lesson_ops import _format_dual_time, _parse_dt, _schedule_at


# ── P0.1: dual-time diff ────────────────────────────────────────────

def test_p01_dual_time_shows_positive_offset():
    """Лето: London=UTC+1 (BST), Almaty=UTC+5 → суффикс [+4ч к London]."""
    result = _format_dual_time("2026-07-28T15:00:00+00:00", "Asia/Almaty")
    assert "16:00 (London)" in result
    assert "20:00 (ваше время, Asia/Almaty)" in result
    # ГЛАВНОЕ: суффикс с разницей поясов реально показывается (bug: всегда был 0).
    assert "[+4ч к London]" in result


def test_p01_dual_time_shows_negative_offset():
    """New York летом UTC-4, London UTC+1 → [-5ч к London]."""
    result = _format_dual_time("2026-07-28T15:00:00+00:00", "America/New_York")
    assert "16:00 (London)" in result
    assert "[-5ч к London]" in result


def test_p01_dual_time_no_suffix_when_same_offset():
    """Europe/Paris летом UTC+2, London UTC+1 → [+1ч к London]."""
    result = _format_dual_time("2026-07-28T15:00:00+00:00", "Europe/Paris")
    assert "[+1ч к London]" in result


def test_p01_dual_time_winter_offsets():
    """Зимой London=UTC+0, Almaty=UTC+5 → [+5ч к London]."""
    result = _format_dual_time("2026-01-28T15:00:00+00:00", "Asia/Almaty")
    assert "15:00 (London)" in result
    assert "[+5ч к London]" in result


# ── P0.5: naive start_time → Europe/London (канон) ──────────────────

def test_p05_parse_dt_naive_is_london_not_utc():
    """Naive вход '...T15:00:00' летом = 15:00 BST, т.е. 14:00 UTC."""
    dt = _parse_dt("2026-07-28T15:00:00")
    assert dt.tzinfo is not None
    as_utc = dt.astimezone(timezone.utc)
    assert (as_utc.hour, as_utc.minute) == (14, 0)
    # London-время — исходные 15:00
    from zoneinfo import ZoneInfo
    assert dt.astimezone(ZoneInfo("Europe/London")).hour == 15


def test_p05_parse_dt_aware_untouched():
    """Aware вход не меняется."""
    dt = _parse_dt("2026-07-28T15:00:00+00:00")
    assert dt.utcoffset() == __import__("datetime").timedelta(0)
    as_utc = dt.astimezone(timezone.utc)
    assert as_utc.hour == 15


def test_p05_schedule_at_converts_naive_london_to_utc():
    """_schedule_at возвращает UTC-строку: наивные 15:00 London летом → 14:00Z."""
    # Дату берём заведомо в будущем, чтобы не сработал fallback.
    target = datetime(2099, 7, 28, 15, 0, 0)
    out = _schedule_at(target)
    out_dt = datetime.fromisoformat(out)
    assert out_dt.tzinfo is not None
    out_utc = out_dt.astimezone(timezone.utc)
    assert out_utc.hour == 14  # BST = UTC+1
