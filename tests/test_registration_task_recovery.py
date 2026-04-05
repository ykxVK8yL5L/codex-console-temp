from contextlib import contextmanager
from pathlib import Path

from src.database.models import Base, RegistrationTask
from src.database.session import DatabaseSessionManager
from src.web.routes import registration as registration_routes


def _build_fake_get_db(manager: DatabaseSessionManager):
    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    return fake_get_db


def test_recover_interrupted_registration_tasks_marks_stale_rows(monkeypatch):
    runtime_dir = Path('tests_runtime')
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / 'registration_task_recovery.db'
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f'sqlite:///{db_path}')
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add_all([
            RegistrationTask(
                task_uuid='recovery-running-001',
                status='running',
                error_message=None,
            ),
            RegistrationTask(
                task_uuid='recovery-running-002',
                status='running',
                error_message='保留原始错误',
            ),
            RegistrationTask(
                task_uuid='recovery-pending-001',
                status='pending',
            ),
            RegistrationTask(
                task_uuid='recovery-completed-001',
                status='completed',
            ),
        ])

    monkeypatch.setattr(registration_routes, 'get_db', _build_fake_get_db(manager))

    result = registration_routes.recover_interrupted_registration_tasks()

    assert result == {
        'running_recovered': 2,
        'pending_recovered': 1,
    }

    with manager.session_scope() as session:
        tasks = {
            task.task_uuid: task
            for task in session.query(RegistrationTask).all()
        }

        assert tasks['recovery-running-001'].status == 'failed'
        assert tasks['recovery-running-001'].completed_at is not None
        assert tasks['recovery-running-001'].error_message == '应用重启后检测到遗留运行任务，已自动标记失败'

        assert tasks['recovery-running-002'].status == 'failed'
        assert tasks['recovery-running-002'].error_message == '保留原始错误'

        assert tasks['recovery-pending-001'].status == 'cancelled'
        assert tasks['recovery-pending-001'].completed_at is not None
        assert tasks['recovery-pending-001'].error_message == '应用重启后检测到遗留排队任务，已自动标记取消'

        assert tasks['recovery-completed-001'].status == 'completed'
        assert tasks['recovery-completed-001'].completed_at is None