"""Snapshot-based reconciliation against missed announcements/withdrawals."""

from __future__ import annotations

from tests.conftest import (
    BACKUP,
    BACKUP_ROUTES,
    PRIMARY,
    PRIMARY_ROUTES,
    announce,
    end_of_rib,
    lookup,
    match_for,
    prime_two_neighbors,
    reconnect,
    withdraw,
)


def issue(client, neighbor: str):
    response = client.post(f"/v1/neighbors/{neighbor}/reconciliation")
    assert response.status_code == 200, response.text
    return response.json()


def submit(client, neighbor: str, ticket: str, routes):
    return client.post(
        f"/v1/neighbors/{neighbor}/reconciliation/submit",
        json={
            "ticket": ticket,
            "routes": [
                {"prefix": prefix, "next_hop": next_hop} for prefix, next_hop in routes
            ],
        },
    )


def submit_ok(client, neighbor: str, ticket: str, routes):
    response = submit(client, neighbor, ticket, routes)
    assert response.status_code == 200, response.text
    return response.json()


def status(client, neighbor: str):
    response = client.get(f"/v1/neighbors/{neighbor}/reconciliation")
    assert response.status_code == 200, response.text
    return response.json()


def primary_routes(client):
    return [
        r
        for r in client.get("/v1/routes").json()["routes"]
        if r["neighbor"] == str(PRIMARY)
    ]


# ------------------------------------------------------------------ issuing
def test_issue_requires_end_of_rib_for_the_current_session(client):
    prime_two_neighbors(client)
    reconnect(client, str(PRIMARY), 2, grace=60)

    response = client.post(f"/v1/neighbors/{PRIMARY}/reconciliation")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "end_of_rib_pending"
    assert error["details"]["session_id"] == 2

    # Once the current session completes its initial update, issuing works.
    end_of_rib(client, str(PRIMARY), 2)
    issued = issue(client, str(PRIMARY))
    assert issued["session_id"] == 2
    assert issued["ticket"].startswith("rec-")


def test_issue_for_unknown_neighbor_is_404(client):
    response = client.post("/v1/neighbors/198.18.0.1/reconciliation")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_neighbor"


def test_status_reports_inactive_then_active_window(client):
    prime_two_neighbors(client)

    before = status(client, str(PRIMARY))
    assert before["active"] is False
    assert before["ticket"] is None

    issued = issue(client, str(PRIMARY))
    during = status(client, str(PRIMARY))
    assert during["active"] is True
    assert during["ticket"] == issued["ticket"]
    assert during["session_id"] == 1
    assert during["tracked_prefixes"] == 0


# ------------------------------------------------------------- batch semantics
def test_snapshot_adds_removes_and_changes_in_one_batch(client):
    prime_two_neighbors(client)
    issued = issue(client, str(PRIMARY))
    revision_before = client.get("/v1/routes").json()["revision"]

    # Snapshot: keep 203.0.113.0/24, change the next hop of 198.51.100.0/24,
    # drop 198.51.100.128/25, add 192.0.2.0/24.
    receipt = submit_ok(
        client,
        str(PRIMARY),
        issued["ticket"],
        [
            ("203.0.113.0/24", "10.0.0.1"),
            ("198.51.100.0/24", "10.0.0.9"),
            ("192.0.2.0/24", "10.0.0.1"),
        ],
    )

    assert receipt["added"] == 1
    assert receipt["removed"] == 1
    assert receipt["changed"] == 1
    assert receipt["kept"] == 1
    assert receipt["route_count"] == 3
    # The whole correction is one batch: exactly one revision step.
    assert receipt["revision"] == revision_before + 1
    assert client.get("/v1/routes").json()["revision"] == revision_before + 1

    by_prefix = {r["prefix"]: r for r in primary_routes(client)}
    assert set(by_prefix) == {"203.0.113.0/24", "198.51.100.0/24", "192.0.2.0/24"}
    assert by_prefix["198.51.100.0/24"]["next_hop"] == "10.0.0.9"
    assert all(not r["pending"] for r in by_prefix.values())

    # The backup neighbour is untouched by the primary's reconciliation.
    backup = [
        r
        for r in client.get("/v1/routes").json()["routes"]
        if r["neighbor"] == str(BACKUP)
    ]
    assert {(r["prefix"], r["next_hop"]) for r in backup} == set(BACKUP_ROUTES)


def test_empty_snapshot_removes_every_untouched_route(client):
    prime_two_neighbors(client)
    issued = issue(client, str(PRIMARY))

    receipt = submit_ok(client, str(PRIMARY), issued["ticket"], [])

    assert receipt["removed"] == len(PRIMARY_ROUTES)
    assert receipt["route_count"] == 0
    assert primary_routes(client) == []


# ------------------------------------------------------- live updates in window
def test_withdrawal_during_window_beats_snapshot(client):
    """The Swagger demo flow: a prefix withdrawn while the snapshot is being
    fetched must not be resurrected by that (stale) snapshot."""
    prime_two_neighbors(client)
    issued = issue(client, str(PRIMARY))

    # Live withdrawal lands immediately, before the snapshot arrives.
    withdraw(client, str(PRIMARY), 1, ["198.51.100.128/25"])
    match = match_for(lookup(client, "198.51.100.200"), "198.51.100.200")
    assert match["prefix"] == "198.51.100.0/24"  # the /25 is already gone

    # The snapshot was taken before the withdrawal and still lists the /25.
    receipt = submit_ok(
        client,
        str(PRIMARY),
        issued["ticket"],
        [(prefix, next_hop) for prefix, next_hop in PRIMARY_ROUTES],
    )

    assert receipt["live_override_prefixes"] == 1
    assert "198.51.100.128/25" not in {r["prefix"] for r in primary_routes(client)}
    match = match_for(lookup(client, "198.51.100.200"), "198.51.100.200")
    assert match["prefix"] == "198.51.100.0/24"


def test_withdrawal_of_already_absent_prefix_still_counts(client):
    prime_two_neighbors(client)
    issued = issue(client, str(PRIMARY))

    # The prefix is not in the online table; the withdrawal is still valid.
    result = withdraw(client, str(PRIMARY), 1, ["192.0.2.0/24"])
    assert result["removed"] == 0
    assert result["not_present"] == ["192.0.2.0/24"]

    # The snapshot lists it, but the last live message wins: it stays absent.
    receipt = submit_ok(
        client,
        str(PRIMARY),
        issued["ticket"],
        PRIMARY_ROUTES + [("192.0.2.0/24", "10.0.0.1")],
    )
    assert "192.0.2.0/24" not in {r["prefix"] for r in primary_routes(client)}
    assert receipt["live_override_prefixes"] == 1


def test_announcement_during_window_beats_snapshot(client):
    prime_two_neighbors(client)
    issued = issue(client, str(PRIMARY))

    # Re-announce one prefix with a new next hop, and announce a brand-new one.
    announce(
        client,
        str(PRIMARY),
        1,
        [("203.0.113.0/24", "10.7.7.7"), ("192.0.2.0/24", "10.0.0.1")],
    )

    # The snapshot disagrees on the next hop and does not know the new prefix.
    receipt = submit_ok(
        client,
        str(PRIMARY),
        issued["ticket"],
        [
            ("203.0.113.0/24", "10.0.0.1"),
            ("198.51.100.0/24", "10.0.0.1"),
            ("198.51.100.128/25", "10.0.0.1"),
        ],
    )

    by_prefix = {r["prefix"]: r for r in primary_routes(client)}
    # Last live message wins over the snapshot for touched prefixes.
    assert by_prefix["203.0.113.0/24"]["next_hop"] == "10.7.7.7"
    # A touched prefix missing from the snapshot is kept, not deleted.
    assert by_prefix["192.0.2.0/24"]["next_hop"] == "10.0.0.1"
    assert receipt["kept"] == 4  # nothing added/removed/changed
    assert receipt["added"] == 0
    assert receipt["removed"] == 0


def test_last_live_message_wins_for_a_prefix(client):
    prime_two_neighbors(client)
    issued = issue(client, str(PRIMARY))

    # announce -> withdraw: the prefix ends up absent, snapshot or not.
    announce(client, str(PRIMARY), 1, [("192.0.2.0/24", "10.0.0.1")])
    withdraw(client, str(PRIMARY), 1, ["192.0.2.0/24"])
    # withdraw -> announce: the prefix ends up present with the last next hop.
    withdraw(client, str(PRIMARY), 1, ["198.51.100.128/25"])
    announce(client, str(PRIMARY), 1, [("198.51.100.128/25", "10.8.8.8")])

    submit_ok(
        client,
        str(PRIMARY),
        issued["ticket"],
        PRIMARY_ROUTES + [("192.0.2.0/24", "10.0.0.1")],
    )

    by_prefix = {r["prefix"]: r for r in primary_routes(client)}
    assert "192.0.2.0/24" not in by_prefix
    assert by_prefix["198.51.100.128/25"]["next_hop"] == "10.8.8.8"


def test_tracked_memory_grows_only_with_distinct_prefixes(client):
    prime_two_neighbors(client)
    issue(client, str(PRIMARY))

    for _ in range(25):
        announce(client, str(PRIMARY), 1, [("203.0.113.0/24", "10.0.0.1")])
        withdraw(client, str(PRIMARY), 1, ["198.51.100.128/25"])

    assert status(client, str(PRIMARY))["tracked_prefixes"] == 2


def test_rejected_stale_messages_do_not_count_as_live_updates(client):
    """Only valid messages touch a prefix; rejected ones leave the snapshot
    authoritative for it."""
    prime_two_neighbors(client)
    reconnect(client, str(PRIMARY), 2, grace=60)
    announce(client, str(PRIMARY), 2, PRIMARY_ROUTES)
    end_of_rib(client, str(PRIMARY), 2)
    issued = issue(client, str(PRIMARY))

    # Session 1 is superseded: rejected, so it must not be tracked.
    stale = client.post(
        f"/v1/neighbors/{PRIMARY}/withdraw",
        json={"session_id": 1, "prefixes": ["198.51.100.128/25"]},
    )
    assert stale.status_code == 409
    assert status(client, str(PRIMARY))["tracked_prefixes"] == 0

    # The snapshot still lists the /25, and because no *valid* live message
    # touched it, the snapshot wins and the prefix stays.
    receipt = submit_ok(
        client, str(PRIMARY), issued["ticket"], PRIMARY_ROUTES
    )
    assert receipt["live_override_prefixes"] == 0
    assert "198.51.100.128/25" in {r["prefix"] for r in primary_routes(client)}


# -------------------------------------------------------------- ticket lifecycle
def test_ticket_is_consumed_by_successful_submission(client):
    prime_two_neighbors(client)
    issued = issue(client, str(PRIMARY))
    submit_ok(client, str(PRIMARY), issued["ticket"], PRIMARY_ROUTES)

    assert status(client, str(PRIMARY))["active"] is False
    before = client.get("/v1/routes").json()
    replay = submit(client, str(PRIMARY), issued["ticket"], PRIMARY_ROUTES)
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "reconciliation_ticket_invalid"
    assert client.get("/v1/routes").json() == before


def test_reissue_supersedes_previous_ticket(client):
    prime_two_neighbors(client)
    first = issue(client, str(PRIMARY))
    second = issue(client, str(PRIMARY))
    assert first["ticket"] != second["ticket"]

    stale = submit(client, str(PRIMARY), first["ticket"], PRIMARY_ROUTES)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "reconciliation_ticket_invalid"

    receipt = submit_ok(client, str(PRIMARY), second["ticket"], PRIMARY_ROUTES)
    assert receipt["kept"] == len(PRIMARY_ROUTES)


def test_reconnect_invalidates_ticket(client):
    prime_two_neighbors(client)
    issued = issue(client, str(PRIMARY))

    reconnect(client, str(PRIMARY), 2, grace=60)

    assert status(client, str(PRIMARY))["active"] is False
    response = submit(client, str(PRIMARY), issued["ticket"], PRIMARY_ROUTES)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reconciliation_ticket_invalid"
    # The retained pending routes are untouched by the failed submission.
    assert all(r["pending"] for r in primary_routes(client))


def test_reset_invalidates_ticket(client):
    prime_two_neighbors(client)
    issued = issue(client, str(PRIMARY))

    client.post("/v1/admin/reset")
    # Unknown right after the reset...
    response = submit(client, str(PRIMARY), issued["ticket"], PRIMARY_ROUTES)
    assert response.status_code == 404

    # ...and still invalid after the neighbour re-establishes the same session.
    reconnect(client, str(PRIMARY), 1, grace=0)
    announce(client, str(PRIMARY), 1, PRIMARY_ROUTES)
    end_of_rib(client, str(PRIMARY), 1)
    response = submit(client, str(PRIMARY), issued["ticket"], PRIMARY_ROUTES)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reconciliation_ticket_invalid"


def test_unknown_or_forged_ticket_is_rejected_and_table_kept(client):
    prime_two_neighbors(client)
    issue(client, str(PRIMARY))
    before = client.get("/v1/routes").json()

    response = submit(client, str(PRIMARY), "rec-forged", [])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reconciliation_ticket_invalid"
    assert client.get("/v1/routes").json() == before


def test_submit_for_unknown_neighbor_is_404(client):
    response = submit(client, "198.18.0.1", "rec-whatever", [])
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_neighbor"


# ----------------------------------------------------------------- validation
def test_duplicate_prefix_in_snapshot_is_rejected_and_table_kept(client):
    prime_two_neighbors(client)
    issued = issue(client, str(PRIMARY))
    before = client.get("/v1/routes").json()

    response = client.post(
        f"/v1/neighbors/{PRIMARY}/reconciliation/submit",
        json={
            "ticket": issued["ticket"],
            "routes": [
                {"prefix": "203.0.113.0/24", "next_hop": "10.0.0.1"},
                {"prefix": "203.0.113.0/24", "next_hop": "10.0.0.2"},
            ],
        },
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "duplicate_prefix"
    assert error["details"]["duplicates"] == ["203.0.113.0/24"]
    assert client.get("/v1/routes").json() == before

    # The ticket survives the bad submission: fix the snapshot and resubmit.
    receipt = submit_ok(client, str(PRIMARY), issued["ticket"], PRIMARY_ROUTES)
    assert receipt["kept"] == len(PRIMARY_ROUTES)


def test_reconciled_routes_are_confirmed_and_visible_in_lookup(client):
    prime_two_neighbors(client)
    issued = issue(client, str(PRIMARY))
    submit_ok(
        client,
        str(PRIMARY),
        issued["ticket"],
        PRIMARY_ROUTES + [("192.0.2.0/24", "10.9.9.9")],
    )

    match = match_for(lookup(client, "192.0.2.77"), "192.0.2.77")
    assert match["status"] == "confirmed"
    assert match["neighbor"] == str(PRIMARY)
    assert match["next_hop"] == "10.9.9.9"
    assert match["route_session_id"] == 1
