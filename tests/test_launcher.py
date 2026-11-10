import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.error import URLError

import pytest
from pydantic import SecretStr

import run as launcher
from scripts import local_pg


@pytest.fixture
def existing_cluster(tmp_path, monkeypatch):
    monkeypatch.setattr(local_pg, "LOCAL", tmp_path)
    (tmp_path / "pgdata").mkdir()
    (tmp_path / "pgdata" / "PG_VERSION").write_text("17")
    pg_bin = tmp_path / "bin"
    pg_bin.mkdir()
    (pg_bin / "pg_ctl.exe").touch()
    return pg_bin


@pytest.mark.parametrize("running", [False, True])
def test_resume_only_starts_existing_cluster(existing_cluster, monkeypatch, running):
    status = MagicMock(return_value=SimpleNamespace(returncode=0 if running else 3))
    binary = MagicMock()
    monkeypatch.setattr(local_pg.subprocess, "run", status)
    monkeypatch.setattr(local_pg, "run_binary", binary)
    local_pg.resume(existing_cluster)
    status.assert_called_once()
    assert status.call_args.args[0][-1] == "status"
    if running:
        binary.assert_not_called()
    else:
        binary.assert_called_once()
        assert binary.call_args.args[0] == existing_cluster / "pg_ctl.exe"
        assert binary.call_args.args[-1] == "start"
        assert str(local_pg.LOCAL / "pgdata") in map(str, binary.call_args.args)
    assert list(local_pg.LOCAL.glob("*.env")) == []
    assert not (local_pg.LOCAL / "pg-credentials.json").exists()


def test_resume_missing_cluster_refuses_initialization(tmp_path, monkeypatch):
    monkeypatch.setattr(local_pg, "LOCAL", tmp_path)
    binary = MagicMock()
    monkeypatch.setattr(local_pg, "run_binary", binary)
    with pytest.raises(RuntimeError, match="缺失"):
        local_pg.resume(tmp_path / "bin")
    binary.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("host,port,database,should_resume", [
    ("127.0.0.1", 55432, "aftercare_demo", True),
    ("localhost", 55432, "aftercare_demo", True),
    ("pg.example", 55432, "aftercare_demo", False),
    ("127.0.0.1", 5432, "aftercare_demo", False),
    ("127.0.0.1", 55432, "other_database", False),
])
def test_ensure_resumes_only_configured_project_cluster(monkeypatch, host, port, database, should_resume):
    from src.config import settings
    from src.storage import database as storage
    monkeypatch.setattr(settings, "database_url", SecretStr(f"postgresql+psycopg://app:private@{host}:{port}/{database}"))
    events = []
    monkeypatch.setattr(local_pg, "resume", lambda _: events.append("resume"))
    monkeypatch.setattr(storage, "check_database", lambda: events.append("check"))
    local_pg.ensure_database(local_pg.LOCAL / "postgresql" / "pgsql" / "bin")
    assert events == (["resume", "check"] if should_resume else ["check"])


@pytest.fixture
def mocked_launcher(monkeypatch):
    api, ui = MagicMock(pid=101), MagicMock(pid=102)
    api.poll.return_value = ui.poll.return_value = None
    popen = MagicMock(side_effect=[api, ui])
    preflight, ready, browser, stop, ports = [MagicMock() for _ in range(5)]
    monkeypatch.setattr(launcher.subprocess, "run", preflight)
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "wait_ready", ready)
    monkeypatch.setattr(launcher.webbrowser, "open", browser)
    monkeypatch.setattr(launcher, "stop_process", stop)
    monkeypatch.setattr(launcher, "check_port", ports)
    monkeypatch.setattr(launcher.time, "sleep", MagicMock(side_effect=KeyboardInterrupt))
    return SimpleNamespace(api=api, ui=ui, popen=popen, preflight=preflight, ready=ready,
                           browser=browser, stop=stop, ports=ports)


def test_launcher_preflights_before_services_and_opens_once(mocked_launcher):
    m = mocked_launcher
    assert launcher.main() == 0
    m.preflight.assert_called_once_with([launcher.VENV_PYTHON, "-m", "scripts.local_pg", "ensure"],
                                        cwd=str(launcher.ROOT), check=True)
    assert [call.args[0] for call in m.ports.call_args_list] == [8000, 8501]
    assert m.popen.call_count == 2
    api_command, ui_command = [call.args[0] for call in m.popen.call_args_list]
    assert api_command[0] == ui_command[0] == launcher.VENV_PYTHON
    assert api_command[api_command.index('--host') + 1] == '127.0.0.1'
    assert ui_command[ui_command.index('--server.address') + 1] == '127.0.0.1'
    assert ui_command[ui_command.index("--server.headless") + 1] == "true"
    assert [call.args[1] for call in m.ready.call_args_list] == [
        "http://127.0.0.1:8000/api/v1/ready", "http://127.0.0.1:8501/_stcore/health"]
    m.browser.assert_called_once_with("http://localhost:8501")
    assert [call.args[0] for call in m.stop.call_args_list] == [m.ui, m.api]


def test_database_failure_launches_nothing(mocked_launcher, capsys):
    m = mocked_launcher
    m.preflight.side_effect = subprocess.CalledProcessError(1, ["sensitive-command"])
    assert launcher.main() == 1
    m.popen.assert_not_called()
    m.browser.assert_not_called()
    error = capsys.readouterr().err
    assert "数据库检查未通过" in error
    assert "sensitive-command" not in error


def test_occupied_port_launches_nothing(mocked_launcher):
    m = mocked_launcher
    m.ports.side_effect = RuntimeError("端口已被占用")
    assert launcher.main() == 1
    m.popen.assert_not_called()
    m.browser.assert_not_called()


@pytest.mark.parametrize("failed_service", ["api", "ui"])
def test_service_failure_closes_owned_children_without_opening_browser(mocked_launcher, failed_service):
    m = mocked_launcher
    failure = RuntimeError("启动失败")
    m.ready.side_effect = [failure] if failed_service == "api" else [None, failure]
    assert launcher.main() == 1
    m.browser.assert_not_called()
    assert m.popen.call_count == (1 if failed_service == "api" else 2)
    assert m.stop.call_args_list[-1].args == (m.api,)
    assert m.stop.call_args_list[0].args == (None if failed_service == "api" else m.ui,)


def test_readiness_retries_then_accepts_health(monkeypatch):
    process = MagicMock()
    process.poll.return_value = None
    response = MagicMock()
    response.__enter__.return_value.status = 200
    request = MagicMock(side_effect=[URLError("not ready"), response])
    monkeypatch.setattr(launcher, "urlopen", request)
    monkeypatch.setattr(launcher.time, "sleep", MagicMock())
    launcher.wait_ready(process, "http://127.0.0.1/ready", "后端")
    assert request.call_count == 2


def test_readiness_exited_process_does_not_accept_other_service(monkeypatch):
    process = MagicMock()
    process.poll.return_value = 1
    request = MagicMock()
    monkeypatch.setattr(launcher, "urlopen", request)
    with pytest.raises(RuntimeError, match="启动失败"):
        launcher.wait_ready(process, "http://127.0.0.1/ready", "后端")
    request.assert_not_called()


def test_readiness_timeout_is_bounded(monkeypatch):
    monkeypatch.setattr(launcher.time, "monotonic", MagicMock(side_effect=[0, 31]))
    with pytest.raises(RuntimeError, match="30 秒"):
        launcher.wait_ready(MagicMock(), "http://127.0.0.1/ready", "后端")
