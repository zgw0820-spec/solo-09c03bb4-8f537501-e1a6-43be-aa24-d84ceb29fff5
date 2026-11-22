# BGP Reconnect Route Visibility

After a BGP neighbour reconnects, the on-call engineer needs to know whether the
affected networks have already moved to the backup path. This FastAPI service
receives the reconnects, IPv4 announcements, withdrawals and End-of-RIB messages
relayed by the collector, keeps one shared routing table, and answers
longest-prefix lookups with next hop and a confirmed/pending flag.

## Delivery semantics

* A **reconnect** carries a strictly increasing `session_id`. It supersedes the
  old session. The neighbour's existing routes are **retained** and flagged
  **pending** for a grace window.
* **Announcements and withdrawals from the current session** take effect
  immediately, and the announced routes are confirmed.
* **Messages from a superseded (older) session are rejected** with HTTP 409 and no
  table change. A message that claims a newer session than the neighbour has
  established is rejected too, until the matching reconnect arrives.
* **Grace timers are bound to the session that armed them.** A reconnect that is
  itself superseded leaves a timer that cannot touch the table.
* Pending routes for one neighbour are removed **as one batch** when either the
  current session's **End-of-RIB** arrives or that reconnect's **grace period
  expires**. Both paths cancel the timer first, so the batch runs once.
* Cleanup is per neighbour: another neighbour's updates and its End-of-RIB never
  wait for this one's convergence.

### Lookup precedence

For each destination in a batch, all neighbours are considered against one
shared table revision, then the winner is chosen by:

1. **longest prefix match**;
2. among equal prefixes, **confirmed before pending**;
3. among equal prefixes and status, **lower neighbour priority number**
   (configured in `NEIGHBOR_PRIORITIES`, default 100);
4. final deterministic tie-break on neighbour address.

Destinations with no route are returned explicitly with `status: "not_found"`.

## Snapshot reconciliation

If the collector missed announcements or withdrawals, it can repair the online
table from the device's own table, per neighbour:

1. **Open a window** — `POST /v1/neighbors/{ip}/reconciliation` issues a
   reconciliation **ticket**. A ticket can only be issued for the neighbour's
   **current session after its End-of-RIB**; earlier, the table is still
   converging and a snapshot could not be told apart from in-flight updates
   (attempts get 409 `end_of_rib_pending`).
2. **Fetch the device table** — while the window is open, announcements and
   withdrawals apply **as usual**. The service remembers the last valid
   message per prefix; this tracking memory grows only with the number of
   **distinct prefixes** seen, not with the number of messages.
3. **Submit the snapshot** — `POST /v1/neighbors/{ip}/reconciliation/submit`
   with the ticket and the device's full IPv4 prefix/next-hop list:
   * prefixes touched by live updates during the window follow the **last
     message** — a withdrawal keeps the prefix out even if the (stale)
     snapshot still lists it, and even if the prefix was already gone from
     the table when the withdrawal arrived;
   * every other prefix follows the **snapshot** — missing routes are added,
     diverging next hops corrected, online routes absent from the snapshot
     deleted;
   * the whole correction takes effect as **one batch** (a single table
     revision step), and the receipt counts `added` / `removed` / `changed`
     (plus `kept` and the number of live-overridden prefixes).

A ticket is **invalidated** by a neighbour reconnect, a service reset, a newer
ticket for the same neighbour, or a successful submission. Submitting with a
dead ticket (409 `reconciliation_ticket_invalid`) or a snapshot containing
duplicate prefixes (400 `duplicate_prefix`) is rejected and the online table
is left unchanged; after a duplicate-prefix rejection the ticket stays valid,
so the collector can fix the snapshot and resubmit.

`GET /v1/neighbors/{ip}/reconciliation` reports the open window (ticket,
session, how many distinct prefixes have live overrides so far).

## Run with Docker Compose

```bash
docker compose up --build
```

* API + Swagger UI: <http://localhost:8000/docs>
* ReDoc: <http://localhost:8000/redoc>
* OpenAPI JSON: <http://localhost:8000/openapi.json>
* Health: <http://localhost:8000/healthz>

`DEMO_SEED=1` is set in `docker-compose.yml`, so the service starts with both
neighbours at session 1 and their routes confirmed — Swagger is demo-ready.

Environment variables (see `app/config.py`):

| Variable | Default (compose) | Meaning |
| --- | --- | --- |
| `NEIGHBOR_PRIORITIES` | `192.0.2.1=10,192.0.2.2=20` | `ip=priority`, lower wins ties |
| `DEFAULT_NEIGHBOR_PRIORITY` | `100` | Priority for unlisted neighbours |
| `RECONNECT_GRACE_SECONDS` | `20` | Default grace window |
| `DEMO_SEED` | `1` | Seed the two-neighbour baseline on startup |
| `HOST_PORT` | `8000` | Host port mapped to container port 8000 |

## Demo walkthrough in Swagger (`/docs`)

The two local samples under `samples/` are the collector payloads for the two
neighbours. `samples/neighbor-primary-192.0.2.1.json` contains the reconnect and
failover story; `samples/neighbor-backup-192.0.2.2.json` is the standing backup.

1. **`GET /v1/routes`** — both neighbours' routes are confirmed. The seed is
   equivalent to replaying the backup sample plus the primary's session-1 prefix
   of the primary sample.
2. **`POST /v1/lookup`** with body from `samples/lookup-destinations.json`:
   `203.0.113.10` and `198.51.100.10` resolve via the primary (`10.0.0.1`),
   `198.51.100.200` hits the more specific `/25`, and `192.0.2.77` is
   `not_found`.
3. **`POST /v1/neighbors/192.0.2.1/reconnect`** with
   `{"session_id": 2, "grace_seconds": 20}` — response reports
   `retained_pending_routes: 3`.
4. **`POST /v1/lookup`** again — the primary's retained routes are now
   **pending**, and at equal prefix length the confirmed backup route wins, so
   `203.0.113.10` and `198.51.100.10` already show `10.0.0.2` / `192.0.2.2`.
   The `/25` only the primary had is still `pending` until the guest session
   proves itself.
5. **`POST /v1/neighbors/192.0.2.1/announce`** with `session_id: 1` — returns
   **409 `stale_session`**; `GET /v1/routes` shows the table unchanged.
6. **`POST /v1/neighbors/192.0.2.1/announce`** with `session_id: 2` and
   `{"prefix": "203.0.113.0/24", "next_hop": "10.0.0.1"}` — confirmed
   immediately; lookups return to the primary for that prefix.
7. **`POST /v1/neighbors/192.0.2.1/end-of-rib`** with `{"session_id": 2}` —
   purges the remaining pending routes in one batch. The `/25` disappears and
   `198.51.100.200` falls back to the backup's `/24`.
8. **`POST /v1/events`** accepts the whole sample file at once and reports
   per-event outcomes (`applied` / `rejected`); the stale event is reported in
   place without aborting later events.

For a clean slate between runs, `POST /v1/admin/reset` clears all state.

Replaying both sample files after a reset reconstructs the same end state; this
is asserted by `tests/test_demo_flow.py`.

### Reconciliation demo: a withdrawal lands while the snapshot is fetched

The seed left both neighbours at session 1 with End-of-RIB done, so a
reconciliation window can be opened immediately.

1. **`POST /v1/neighbors/192.0.2.1/reconciliation`** — no body. The response
   carries the `ticket` (e.g. `rec-4f2a…`) bound to session 1.
   `GET /v1/neighbors/192.0.2.1/reconciliation` shows the open window.
2. **While the collector is "fetching" the device table**, a live withdrawal
   arrives: **`POST /v1/neighbors/192.0.2.1/withdraw`** with
   `{"session_id": 1, "prefixes": ["198.51.100.128/25"]}`. It applies at once —
   `POST /v1/lookup` for `198.51.100.200` now matches the less-specific
   `198.51.100.0/24`.
3. **The fetch finishes.** The snapshot was taken *before* the withdrawal, so
   it still lists the /25 — and it also carries a next-hop change for
   `198.51.100.0/24` (now `10.0.0.9`) plus a prefix the collector never saw
   (`192.0.2.0/24`). **`POST /v1/neighbors/192.0.2.1/reconciliation/submit`**
   with the body from `samples/reconciliation-192.0.2.1.json`, pasting the
   ticket from step 1 into the `ticket` field.
4. **The receipt** reports `added: 1` (`192.0.2.0/24`), `changed: 1`
   (`198.51.100.0/24` next hop), `removed: 0`, `kept: 1`, and
   `live_override_prefixes: 1`. `GET /v1/routes` confirms the /25 was **not**
   resurrected by the stale snapshot: the in-window withdrawal won.
5. **Submit the same ticket again** — 409 `reconciliation_ticket_invalid`;
   the ticket was consumed by the successful submission.

Also worth trying in Swagger: issue a ticket, then
`POST /v1/neighbors/192.0.2.1/reconnect` with `{"session_id": 2}` — the ticket
is dead (submitting it now returns 409), and a fresh ticket cannot be issued
until the new session's End-of-RIB arrives (409 `end_of_rib_pending`).

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/neighbors/{ip}/reconnect` | New session takes over, old routes pending |
| POST | `/v1/neighbors/{ip}/announce` | IPv4 announcements from current session |
| POST | `/v1/neighbors/{ip}/withdraw` | IPv4 withdrawals from current session |
| POST | `/v1/neighbors/{ip}/end-of-rib` | Batch-remove this neighbour's pending routes |
| POST | `/v1/neighbors/{ip}/reconciliation` | Issue a reconciliation ticket (needs End-of-RIB) |
| GET | `/v1/neighbors/{ip}/reconciliation` | Open-window status and live-override count |
| POST | `/v1/neighbors/{ip}/reconciliation/submit` | Apply device snapshot as one batch |
| POST | `/v1/events` | Ordered batch with per-event accept/reject |
| POST | `/v1/lookup` | Batch longest-prefix lookup, one shared revision |
| GET | `/v1/routes` | Table snapshot with pending flags |
| GET | `/v1/neighbors` | Session state and pending counts per neighbour |
| POST | `/v1/admin/reset` | Clear state (demo helper) |
| GET | `/healthz` | Liveness |

Error bodies are always `{"error": {"code", "message", "details"}}`. Relevant
codes: `stale_session`, `session_not_increasing`, `session_not_established`,
`unknown_neighbor`, `end_of_rib_pending`, `reconciliation_ticket_invalid`,
`duplicate_prefix`.

## Tests

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```

`requirements.txt` pins the runtime; the tests additionally need `pytest` and
`httpx` (installed above). The suite covers longest-prefix selection, priority
tie-breaks, explicit not-found, pending retention, immediate announce/withdraw,
batch End-of-RIB cleanup, grace expiry, superseded-timer protection, stale/future
session rejection, per-neighbour independence, sample replay, and snapshot
reconciliation (ticket lifecycle and invalidation, last-live-message-wins,
one-batch add/remove/change receipts, duplicate-prefix and dead-ticket
rejection, distinct-prefix tracking memory).

`TestClient` drives the app's real lifespan and event loop, so the grace-timer
tests exercise the same `asyncio` timer path used in production.
