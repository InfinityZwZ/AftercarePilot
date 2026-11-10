"""Verify approval/ticket behavior over actual HTTP, never call a live model."""
import httpx


def get(client, path):
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def post(client, path, body, expected=200):
    response = client.post(path, json=body)
    if response.status_code != expected:
        raise AssertionError(f"Unexpected HTTP status {response.status_code}; response body omitted")
    return response.json()


def exercise_http(base_url, token, *, combined=False):
    with httpx.Client(base_url=base_url, headers={"X-Operator-Token": token}, timeout=60, trust_env=False) as client:
        assert get(client, "/ready")["status"] == "ready"
        original_count = len(get(client, "/aftersales-tickets")["items"])
        proposal = post(client, "/proposals/analyze", {"message": "ORD-1009 的音箱坏了，我想退货。", "language": "zh"})
        assert proposal["status"] == "pending_approval" and len(proposal["payload"]["evidence"]) == 3
        proposal_id = proposal["proposal_id"]
        path = "/proposals/" + proposal_id
        guard = {"expected_revision": proposal["revision"], "expected_payload_hash": proposal["payload_hash"]}
        post(client, path + "/execute", guard, 403)
        assert len(get(client, "/aftersales-tickets")["items"]) == original_count
        if combined:
            result = post(client, path + "/confirm-and-create", guard)
        else:
            assert post(client, path + "/approve", guard)["status"] == "approved"
            assert len(get(client, "/aftersales-tickets")["items"]) == original_count
            assert get(client, path + "/execution")["status"] == "not_executed"
            result = post(client, path + "/execute", guard)
        assert result["status"] == "executed"
        checked = get(client, path + "/execution")
        assert checked["status"] == "executed" and all(checked["verification"].values())
        ticket = get(client, "/aftersales-tickets/" + checked["ticket"]["id"])
        assert ticket["status"] == "requested" and ticket["order_id"] == "ORD-1009"
        assert post(client, path + "/execute", guard)["ticket"]["id"] == ticket["id"]
        assert len(get(client, "/aftersales-tickets")["items"]) == original_count + 1
        audits = get(client, "/audit-logs?request_id=" + proposal["request_id"])["items"]
        assert sum(item["event_type"] == "ticket_created" for item in audits) == 1
        assert get(client, "/requests/" + proposal["request_id"])["status"] == "accepted"
        return dict(proposal_id=proposal_id, request_id=proposal["request_id"], ticket_id=ticket["id"], ticket_no=ticket["ticket_no"],
                    guard=guard, total_tickets=original_count + 1, verification=checked["verification"],
                    unapproved_no_ticket=True, approval_no_ticket=not combined, combined_confirmation=combined,
                    replay_same_ticket=True, provider_calls=0)


def verify_persistence(base_url, token, previous):
    with httpx.Client(base_url=base_url, headers={"X-Operator-Token": token}, timeout=30, trust_env=False) as client:
        path = "/proposals/" + previous["proposal_id"]
        assert get(client, path)["status"] == "executed"
        result = get(client, path + "/execution")
        assert result["status"] == "executed" and all(result["verification"].values())
        assert result["ticket"]["id"] == previous["ticket_id"]
        assert get(client, "/aftersales-tickets/" + previous["ticket_id"])["ticket_no"] == previous["ticket_no"]
        assert post(client, path + "/execute", previous["guard"])["ticket"]["id"] == previous["ticket_id"]
        assert post(client, path + "/confirm-and-create", previous["guard"])["ticket"]["id"] == previous["ticket_id"]
        assert get(client, "/requests/" + previous["request_id"])["status"] == "accepted"
        assert len(get(client, "/aftersales-tickets")["items"]) == previous["total_tickets"]
        audits = get(client, "/audit-logs?request_id=" + previous["request_id"])["items"]
        assert sum(item["event_type"] == "ticket_created" for item in audits) == 1
        return {**previous, "restart_preserved_ticket": True, "restart_replay_same_ticket": True}
