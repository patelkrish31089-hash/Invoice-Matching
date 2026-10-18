from app.services.fraud import detect_duplicates


def test_near_duplicate_can_be_found_from_repeated_line_items_when_number_changes():
    current = {
        "id": "current", "vendor_id": "vendor-1", "invoice_number": "REF-900",
        "issue_date": "2026-04-12", "total_amount": "150.00",
        "line_items": [
            {"sku": "A-1", "description": "Widget Pro", "quantity": "2", "unit_price": "50.00", "line_total": "100.00"},
            {"sku": "B-2", "description": "Cable Bundle", "quantity": "1", "unit_price": "50.00", "line_total": "50.00"},
        ],
    }
    history = [{
        "id": "prior", "vendor_id": "vendor-1", "invoice_number": "INV-100",
        "issue_date": "2026-04-10", "total_amount": "150.00",
        "line_items": [
            {"sku": "B-2", "description": "Cable Bundle", "quantity": "1.0", "unit_price": "50", "line_total": "50.00"},
            {"sku": "A-1", "description": "Widget Pro", "quantity": "2.0", "unit_price": "50", "line_total": "100"},
        ],
    }]

    signals = detect_duplicates(current, history)

    assert len(signals) == 1
    assert signals[0]["type"] == "near_duplicate"
    assert signals[0]["evidence"]["invoice_number_similarity"] < 72
    assert signals[0]["evidence"]["line_item_similarity"] == 1.0
    assert signals[0]["evidence"]["line_item_counts"] == [2, 2]
    assert "near-identical line-item content" in signals[0]["explanation"]
    assert "Widget Pro" not in str(signals[0]["evidence"])


def test_duplicate_detector_does_not_use_line_similarity_across_vendor_or_amount():
    current = {
        "id": "current", "vendor_id": "vendor-1", "invoice_number": "REF-900",
        "issue_date": "2026-04-12", "total_amount": "150.00",
        "line_items": [{"sku": "A-1", "description": "Widget Pro", "quantity": "2", "unit_price": "50", "line_total": "100"}],
    }
    history = [
        {"id": "other-vendor", "vendor_id": "vendor-2", "invoice_number": "INV-100", "issue_date": "2026-04-12", "total_amount": "150.00", "line_items": current["line_items"]},
        {"id": "other-amount", "vendor_id": "vendor-1", "invoice_number": "INV-100", "issue_date": "2026-04-12", "total_amount": "151.00", "line_items": current["line_items"]},
    ]

    assert detect_duplicates(current, history) == []
