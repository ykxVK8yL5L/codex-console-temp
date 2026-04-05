from src.database.models import EmailService, RegistrationTask
from src.web.routes.registration import (
    RECOVERED_PENDING_TASK_ERROR,
    RECOVERED_RUNNING_TASK_ERROR,
    task_to_response,
)


def test_task_to_response_marks_startup_recovered_running_task():
    service = EmailService(id=15, name='测试云邮', service_type='cloudmail', config={})
    task = RegistrationTask(
        id=101,
        task_uuid='task-recovered-running-001',
        status='failed',
        email_service_id=15,
        error_message=RECOVERED_RUNNING_TASK_ERROR,
    )
    task.email_service = service

    response = task_to_response(task)

    assert response.recovered_on_startup is True
    assert response.recovery_reason == 'startup_recovered_running'
    assert response.email_service_name == '测试云邮'
    assert response.email_service_type == 'cloudmail'


def test_task_to_response_marks_startup_recovered_pending_task():
    task = RegistrationTask(
        id=102,
        task_uuid='task-recovered-pending-001',
        status='cancelled',
        error_message=RECOVERED_PENDING_TASK_ERROR,
    )

    response = task_to_response(task)

    assert response.recovered_on_startup is True
    assert response.recovery_reason == 'startup_recovered_pending'


def test_task_to_response_keeps_normal_task_unmarked():
    task = RegistrationTask(
        id=103,
        task_uuid='task-normal-001',
        status='failed',
        error_message='普通失败',
    )

    response = task_to_response(task)

    assert response.recovered_on_startup is False
    assert response.recovery_reason is None