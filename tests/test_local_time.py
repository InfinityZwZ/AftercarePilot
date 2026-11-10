from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.domain.clock import SystemClock, utc
from tests.test_stage3_api import api


def test_system_clock_uses_os_local_timezone_and_current_instant():
    before = datetime.now().astimezone()
    actual = SystemClock().now()
    after = datetime.now().astimezone()
    assert before <= actual <= after
    assert actual.utcoffset() == before.utcoffset()


def test_default_timezone_follows_system(monkeypatch):
    from src.config import Settings
    monkeypatch.delenv('APP_TIMEZONE', raising=False)
    assert Settings(_env_file=None).app_timezone == 'local'


def test_local_timestamp_matches_system_without_changing_instant():
    from src.ui.presentation import local_time
    value = datetime(2026, 11, 6, 7, 1, 24, tzinfo=timezone.utc)
    expected = value.astimezone().strftime('%Y-%m-%d %H:%M:%S') + '（本地时间）'
    assert local_time(value.isoformat(), 'local') == expected


def test_system_local_conversion_keeps_offsets_date_specific(monkeypatch):
    from src.ui import presentation
    monkeypatch.setattr(presentation, 'to_local', lambda value: value.astimezone(ZoneInfo('America/New_York')), raising=False)
    assert presentation.local_time('2026-01-10T12:00:00Z', 'local') == '2026-01-10 07:00:00（本地时间）'
    assert presentation.local_time('2026-07-10T12:00:00Z', 'local') == '2026-07-10 08:00:00（本地时间）'


def test_to_local_preserves_instant_and_rejects_naive_values():
    import pytest
    from src.domain.clock import to_local
    instant = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    assert utc(to_local(instant)) == instant
    with pytest.raises(ValueError):
        to_local(instant.replace(tzinfo=None))


def test_recent_seed_uses_local_calendar_day_but_keeps_absolute_times(stage3, monkeypatch):
    from src.domain import clock
    from scripts.seed_demo import seed_recent_orders
    from src.storage.models import Order
    monkeypatch.setattr(clock, 'to_local', lambda value: value.astimezone(ZoneInfo('Asia/Shanghai')), raising=False)
    now = datetime(2026, 11, 6, 17, 30, tzinfo=timezone.utc)
    with stage3['sessions'].begin() as session:
        ids = seed_recent_orders(session, now=now)
        assert ids == ['ORD-20261107-01', 'ORD-20261107-02', 'ORD-20261107-03']
        row = session.get(Order, ids[0])
        assert utc(row.delivered_at) == now - timedelta(hours=1)
        assert row.shipment['estimated_delivery'] == '2026-11-07'


def test_request_page_formats_existing_dates_as_local(api, stage3, monkeypatch):
    from src.config import settings
    from tests.test_stage3_api import create
    from tests.test_streamlit_workflow import start_ui
    from src.ui.presentation import local_time
    monkeypatch.setattr(settings, 'app_timezone', 'local')
    create(api)
    app, _ = start_ui(api, monkeypatch)
    assert not app.exception
    expected = local_time(stage3['clock'].now().isoformat(), 'local')
    assert any(item.value == expected for item in app.caption)
    assert not any('悉尼' in item.value for item in app.caption)




def test_compose_passes_same_system_timezone_to_api_and_ui():
    from pathlib import Path
    import yaml
    compose = yaml.safe_load((Path(__file__).resolve().parents[1] / 'compose.yaml').read_text(encoding='utf-8'))
    for service in ('api', 'ui'):
        assert compose['services'][service]['environment']['TZ'].startswith('${TZ:?')


def test_compose_timezone_refresh_preserves_other_configuration(tmp_path):
    from dotenv import dotenv_values
    from scripts.configure_compose import write_configuration
    path = tmp_path / 'compose.env'
    write_configuration(path, verification=True, timezone_name='Asia/Shanghai')
    first = dict(dotenv_values(path))
    assert first['TZ'] == 'Asia/Shanghai' and first['APP_TIMEZONE'] == 'local'
    write_configuration(path, verification=True, timezone_name='Australia/Sydney')
    second = dict(dotenv_values(path))
    assert second.pop('TZ') == 'Australia/Sydney'
    first.pop('TZ')
    assert first == second


def test_mcp_inherits_valid_timezone_without_application_secrets(monkeypatch):
    from src.mcp.gateway import server_environment
    monkeypatch.setenv('TZ', 'Asia/Shanghai')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-secret-never-in-child')
    environment = server_environment('test-reader-url')
    assert environment['TZ'] == 'Asia/Shanghai'
    assert 'OPENAI_API_KEY' not in environment
    monkeypatch.setenv('TZ', '../private-file')
    assert 'TZ' not in server_environment('test-reader-url')
