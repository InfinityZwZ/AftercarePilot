from fastapi.testclient import TestClient


def test_api_startup_checks_real_pg_without_seed_or_model(pg, monkeypatch):
    import src.main as main
    from src.storage import database
    from sqlalchemy import text
    monkeypatch.setattr(main, "init_langfuse", lambda: None)
    monkeypatch.setattr(main, "get_langfuse_client", lambda: None)
    monkeypatch.setattr(database, "session_factory", lambda readonly=False: pg["reader_sessions"] if readonly else pg["sessions"])
    with pg["sessions"]() as session:
        before = session.scalar(text("SELECT count(*) FROM aftersales_tickets"))
    with TestClient(main.app) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200 and response.json()["status"] == "ok"
    with pg["sessions"]() as session:
        assert session.scalar(text("SELECT count(*) FROM aftersales_tickets")) == before


def test_startup_rejects_writer_as_reader(pg, monkeypatch):
    import pytest
    from src.storage import database
    monkeypatch.setattr(database, "session_factory", lambda readonly=False: pg["sessions"])
    with pytest.raises(RuntimeError, match="excessive"):
        database.check_database()


def test_optional_tracing_errors_do_not_block_real_pg_api(pg, monkeypatch):
    import src.main as main
    from src.storage import database
    from types import SimpleNamespace
    def broken():
        raise RuntimeError("synthetic tracing outage")
    monkeypatch.setattr(main, "init_langfuse", broken)
    monkeypatch.setattr(main, "get_langfuse_client", lambda: SimpleNamespace(flush=broken))
    monkeypatch.setattr(main, "ProposalService", lambda: SimpleNamespace(recover_interrupted=lambda: None))
    monkeypatch.setattr(database, "session_factory", lambda readonly=False: pg["reader_sessions"] if readonly else pg["sessions"])
    with TestClient(main.app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/ready").status_code == 200
