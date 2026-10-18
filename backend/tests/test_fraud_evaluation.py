from app.services.fraud import evaluate_binary_predictions, relationship_overlaps


def test_held_out_evaluation_reports_false_positive_rate_and_review_capacity_metrics():
    result = evaluate_binary_predictions(
        [1, 0, 1, 0, 0, 1],
        [0.9, 0.8, 0.4, 0.3, 0.1, 0.2],
        k=2,
        threshold=0.5,
    )

    assert result["evaluated"] is True
    assert result["precision_at_threshold"] == 0.5
    assert result["recall_at_threshold"] == 0.3333
    assert result["f1_at_threshold"] == 0.4
    assert result["false_positive_rate"] == 0.3333
    assert result["specificity"] == 0.6667
    assert result["confusion_matrix"] == {
        "true_positive": 1,
        "false_positive": 1,
        "true_negative": 2,
        "false_negative": 2,
    }
    assert result["review_capacity_k"] == 2
    assert result["precision_at_review_capacity"] == 0.5
    assert result["recall_at_review_capacity"] == 0.3333


def test_relationship_graph_detects_shared_vendor_tokens_without_emitting_sensitive_values():
    vendors = [
        {"id": "v-a", "name": "Vendor A", "bank_token": "bank-secret-token"},
        {"id": "v-b", "name": "Vendor B", "bank_token": "bank-secret-token"},
        {"id": "v-c", "name": "Vendor C", "address_token": "address-secret-token"},
        {"id": "v-d", "name": "Vendor D", "address_token": "address-secret-token"},
    ]
    employees = [{"id": "e-1", "display_name": "Employee", "address_token": "address-secret-token"}]

    findings = relationship_overlaps(iter(vendors), iter(employees))
    vendor_pairs = [item for item in findings if item["type"] == "vendor_vendor_relationship_overlap"]
    employee_pairs = [item for item in findings if item["type"] == "vendor_employee_relationship_overlap"]

    assert len(vendor_pairs) == 2
    assert len(employee_pairs) == 2
    assert {item["evidence"]["overlap_types"][0] for item in vendor_pairs} == {"bank_token", "address_token"}
    assert "bank-secret-token" not in str(findings)
    assert "address-secret-token" not in str(findings)


def test_held_out_evaluation_refuses_invalid_inputs_instead_of_fabricating_metrics():
    assert evaluate_binary_predictions([1, 1], [0.9, 0.2])["evaluated"] is False
    assert evaluate_binary_predictions([1, 0], [0.9, 0.2], k=0)["evaluated"] is False
    assert evaluate_binary_predictions([1, 2], [0.9, 0.2])["evaluated"] is False
    assert evaluate_binary_predictions([1, 0], [0.9, float("nan")])["evaluated"] is False
    assert evaluate_binary_predictions([1, 0], [0.9, 0.2], threshold=1.5)["evaluated"] is False
