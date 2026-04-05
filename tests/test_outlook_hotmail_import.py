import asyncio
from contextlib import contextmanager
from pathlib import Path

from src.database.models import Base, Account, EmailService
from src.database.session import DatabaseSessionManager
from src.web.routes import email as email_routes
from src.web.routes import registration as registration_routes


def _build_manager(db_name: str) -> DatabaseSessionManager:
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / db_name
    if db_path.exists():
        db_path.unlink()
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def test_outlook_batch_import_accepts_hotmail_oauth_payload(monkeypatch):
    manager = _build_manager("outlook_batch_import.db")

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(email_routes, "get_db", fake_get_db)

    request = email_routes.OutlookBatchImportRequest(
        data=(
            "MQKBIU8900@HOTMAIL.COM----kkdqm59772----"
            "9e5f94bc-e8a4-4e73-b8be-63364c29d753----"
            "M.C537_SN1.0.U.-example-refresh-token$"
        ),
        enabled=True,
        priority=3,
    )

    result = asyncio.run(email_routes.batch_import_outlook(request))

    assert result.total == 1
    assert result.success == 1
    assert result.failed == 0
    assert result.accounts[0]["email"] == "mqkbiu8900@hotmail.com"
    assert result.accounts[0]["has_oauth"] is True

    with manager.session_scope() as session:
        service = session.query(EmailService).one()
        assert service.name == "mqkbiu8900@hotmail.com"
        assert service.priority == 3
        assert service.config["email"] == "mqkbiu8900@hotmail.com"
        assert service.config["password"] == "kkdqm59772"
        assert service.config["client_id"] == "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
        assert service.config["refresh_token"] == "M.C537_SN1.0.U.-example-refresh-token$"


def test_outlook_registration_status_matches_account_case_insensitively(monkeypatch):
    manager = _build_manager("outlook_registration_status.db")

    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="outlook",
                name="foo@hotmail.com",
                config={
                    "email": "foo@hotmail.com",
                    "password": "secret",
                    "client_id": "client-id",
                    "refresh_token": "refresh-token",
                },
                enabled=True,
                priority=0,
            )
        )
        session.add(Account(email="Foo@Hotmail.com", password="pw", status="active"))

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    result = asyncio.run(registration_routes.get_outlook_accounts_for_registration())

    assert result.total == 1
    assert result.registered_count == 1
    assert result.unregistered_count == 0
    assert result.accounts[0].email == "foo@hotmail.com"
    assert result.accounts[0].is_registered is True
