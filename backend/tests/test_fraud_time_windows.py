from datetime import date, timedelta
from types import SimpleNamespace

from app.services.fraud import vendor_behavior_features
from app.processing import _history_before_issue_date


def test_vendor_history_without_invoice_date_does_not_assume_today():
    today = date.today()
    history = [
        {"id": f"prior-{index}", "vendor_id": "v1", "issue_date": (today - timedelta(days=index + 1)).isoformat(), "total_amount": "100"}
        for index in range(4)
    ]
    result = vendor_behavior_features({"id": "current", "vendor_id": "v1", "issue_date": None, "total_amount": "100"}, history)
    assert result["issue_date_available"] is False
    assert result["indicators"] == []
    assert result["days_since_previous_invoice"] is None
    assert result["history_count"] == 0


def test_isolation_reference_excludes_future_issued_invoices():
    past = SimpleNamespace(issue_date=date(2026, 1, 1))
    same_day = SimpleNamespace(issue_date=date(2026, 1, 2))
    future = SimpleNamespace(issue_date=date(2026, 1, 3))
    assert _history_before_issue_date([past, same_day, future], date(2026, 1, 2)) == [past]
    assert _history_before_issue_date([past, future], None) == []
