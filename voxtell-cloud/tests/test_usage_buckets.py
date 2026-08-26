"""GET /v1/usage — daily buckets, zero-filled, in UTC. Plus its access gate.

Time is expressed in SQL, never patched in Python — conftest's rule, and here it is
load-bearing rather than stylistic: the endpoint buckets with
``date_trunc('day', created_at AT TIME ZONE 'UTC')`` *inside Postgres*, so a
monkeypatched Python clock would prove nothing about the thing that can actually be
wrong, which is the timezone the grouping happens in.

The HTTP layer is driven through ASGI with ``get_session`` and ``get_console_user``
overridden. Overriding the identity dependency means these tests cannot also prove
*which* dependency the route uses — so the gate is asserted separately, and
behaviourally, in ``test_is_jwt_gated_not_key_gated``. Between the two, both the SQL
and the access rule are pinned.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.auth import get_console_user
from api.db import get_session
from api.main import app

pytestmark = pytest.mark.pg


@pytest_asyncio.fixture
async def client(db_session, make_user):
    """An ASGI client authenticated as a fresh user, sharing the test's session.

    Sharing the session matters: the test writes usage events and the endpoint must
    read them inside the same transaction, which no separate engine would see.
    """
    uid = make_user()

    from api.models import User

    user = await db_session.get(User, uid)
    assert user is not None

    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_console_user] = lambda: user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        c.user = user  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()


def _add_event(session, uid, *, when, prompts=1, gpu=1.0, voxels=100):
    """Insert a usage event at an explicit UTC instant."""
    return session.execute(
        text(
            "INSERT INTO usage_events (id, user_id, prompts, gpu_seconds, voxels, created_at) "
            "VALUES (:id, :uid, :p, :g, :v, :ts)"
        ),
        {"id": uuid.uuid4(), "uid": uid, "p": prompts, "g": gpu, "v": voxels, "ts": when},
    )


@pytest.mark.asyncio
async def test_window_is_zero_filled_and_ordered(client, db_session):
    """Every day in the window appears, including the empty ones, oldest first."""
    uid = client.user.id
    now = datetime.now(timezone.utc)
    today = now.date()

    await _add_event(db_session, uid, when=now, prompts=3, gpu=10.5)
    await _add_event(db_session, uid, when=now, prompts=2, gpu=4.5)
    await _add_event(db_session, uid, when=now - timedelta(days=3), prompts=1, gpu=2.0)
    await db_session.flush()

    r = await client.get("/v1/usage", params={"days": 5})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["window_days"] == 5
    assert len(body["days"]) == 5, "the window must be dense, not sparse"
    days = [d["day"] for d in body["days"]]
    assert days == sorted(days), "oldest first"
    assert days[-1] == today.isoformat(), "the window includes today"
    assert body["since"] == (today - timedelta(days=4)).isoformat()

    by_day = {d["day"]: d for d in body["days"]}
    assert by_day[today.isoformat()]["jobs"] == 2
    assert by_day[today.isoformat()]["prompts"] == 5
    assert by_day[today.isoformat()]["gpu_seconds"] == pytest.approx(15.0)
    assert by_day[(today - timedelta(days=3)).isoformat()]["jobs"] == 1

    # The empty days are present and zeroed, not absent. The window is
    # {today, -1, -2, -3, -4} and only today and -3 have events, so three are empty.
    empty = [d["day"] for d in body["days"] if d["jobs"] == 0]
    assert empty == [
        (today - timedelta(days=n)).isoformat() for n in (4, 2, 1)
    ], "the gaps must be exactly the days with no activity, in order"
    assert all(
        d["prompts"] == 0 and d["gpu_seconds"] == 0.0 and d["voxels"] == 0
        for d in body["days"] if d["day"] in empty
    )


@pytest.mark.asyncio
async def test_totals_match_the_buckets(client, db_session):
    """The summary numbers and the chart must not be able to disagree."""
    uid = client.user.id
    now = datetime.now(timezone.utc)
    await _add_event(db_session, uid, when=now, prompts=4, gpu=8.0)
    await _add_event(db_session, uid, when=now - timedelta(days=1), prompts=6, gpu=12.0)
    await db_session.flush()

    body = (await client.get("/v1/usage", params={"days": 7})).json()
    assert body["total_jobs"] == sum(d["jobs"] for d in body["days"]) == 2
    assert body["total_prompts"] == sum(d["prompts"] for d in body["days"]) == 10
    assert body["total_gpu_seconds"] == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_events_outside_the_window_are_excluded(client, db_session):
    await _add_event(
        db_session, client.user.id,
        when=datetime.now(timezone.utc) - timedelta(days=40), prompts=9, gpu=99.0,
    )
    await db_session.flush()

    body = (await client.get("/v1/usage", params={"days": 7})).json()
    assert body["total_jobs"] == 0
    assert body["total_prompts"] == 0


@pytest.mark.asyncio
async def test_another_users_events_are_never_counted(client, db_session, make_user):
    """Scoping is the one bug here that would be a disclosure, not a glitch."""
    other = make_user()
    now = datetime.now(timezone.utc)
    await _add_event(db_session, client.user.id, when=now, prompts=1, gpu=1.0)
    await _add_event(db_session, other, when=now, prompts=50, gpu=500.0)
    await db_session.flush()

    body = (await client.get("/v1/usage", params={"days": 7})).json()
    assert body["total_jobs"] == 1
    assert body["total_prompts"] == 1


@pytest.mark.asyncio
async def test_day_boundary_is_utc(client, db_session):
    """An event at 23:30 UTC belongs to that UTC day, not a local one.

    This is the assertion that matters most. The daily columns sit directly under a
    monthly quota counted from ``quota.month_start()``, which is UTC. If the
    bucketing drifted to the server's local zone the two would disagree by a day at
    the edges — visible only as a chart that does not add up to the number above it,
    which is exactly the kind of discrepancy nobody investigates and everybody
    quietly distrusts.
    """
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    stamp = datetime(
        yesterday.year, yesterday.month, yesterday.day, 23, 30, tzinfo=timezone.utc
    )
    await _add_event(db_session, client.user.id, when=stamp)
    await db_session.flush()

    body = (await client.get("/v1/usage", params={"days": 3})).json()
    by_day = {d["day"]: d["jobs"] for d in body["days"]}
    assert by_day[yesterday.isoformat()] == 1
    assert by_day[datetime.now(timezone.utc).date().isoformat()] == 0


@pytest.mark.asyncio
async def test_window_is_capped(client):
    """Unbounded, this is a growing full-table scan per request."""
    assert (await client.get("/v1/usage", params={"days": 0})).status_code == 422
    assert (await client.get("/v1/usage", params={"days": 1000})).status_code == 422
    assert (await client.get("/v1/usage", params={"days": 366})).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/usage", "/v1/system"])
async def test_is_jwt_gated_not_key_gated(db_session, path):
    """A leaked workstation key segments images; it does not enumerate history.

    Behavioural rather than by introspection: this FastAPI wraps included routers in
    an opaque object with no reachable ``.routes``, so walking the route tree is both
    fragile and version-dependent. ``get_console_user`` rejects the ``vxt_`` prefix
    before it ever reaches Keycloak, so a syntactically-valid key is enough to prove
    the gate — no real key and no IdP required.

    This is the test that fails if someone later "simplifies" these routes onto
    ``get_caller`` to match the job endpoints.
    """
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(path, headers={"Authorization": "Bearer vxt_not_a_real_key"})
        assert r.status_code == 401, r.text
        assert "api key" in r.text.lower()

        # And no credential at all is also refused, rather than falling through to
        # an anonymous read.
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.get(path)).status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()
