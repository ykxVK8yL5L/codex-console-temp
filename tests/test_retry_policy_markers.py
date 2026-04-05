from src.web.routes import accounts as accounts_routes
from src.web.routes import payment as payment_routes


def test_accounts_validate_retry_markers_cover_weak_network():
    assert accounts_routes._is_retryable_validate_error("network timeout while requesting")
    assert accounts_routes._is_retryable_validate_error("HTTP 503 upstream unavailable")
    assert not accounts_routes._is_retryable_validate_error("password is invalid")


def test_payment_subscription_retry_markers_cover_weak_network():
    assert payment_routes._is_retryable_subscription_check_error("connection reset by peer")
    assert payment_routes._is_retryable_subscription_check_error("http 429 too many requests")
    assert not payment_routes._is_retryable_subscription_check_error("账号未订阅 plus/team")
