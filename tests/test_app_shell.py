"""Tests for `app.py` (the shared session) and `iparking_lib/selfcheck.py`.

Two subjects, both of them things that fail silently on a hub and cannot be caught by looking
at the code.

**The shared session.** Its whole design is "one stable object, credentials updated in place",
and every way of getting that wrong looks like success at the moment it happens: a replaced
object works perfectly for whoever asks next while everything already holding the old one keeps
using rejected credentials; a `logout` that also drops the object makes itself permanent and
every device stays dead until the app restarts. So the assertions here are about *identity* and
*ordering*, not about return values.

**The certificate warning.** Both hosts' leaf certificates expire 2026-10-27 and `certifi` is
pinned on a hub nobody updates routinely, so the ordinary outcome of a CA rotation is that login
stops working one day with no hint why. The warning threshold is the part of that probe with a
bug worth catching, and it is the part a real handshake cannot exercise — a live server's
certificate is months from expiring, so the boundary is only reachable against a fixed clock.

No event-loop plugin is installed, so each test drives `asyncio.run` itself.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

import app as app_module
from iparking_lib import selfcheck
from iparking_lib.const import SETTING_LANGUAGE, SETTING_PASSWORD, SETTING_USERNAME
from iparking_lib.iparking.client import IparkingAuthError, NeedCredentialsError


@pytest.fixture
def make_app(make_homey, monkeypatch):
    """An `IparkingApp` on a fake hub, with the startup probes stubbed out.

    `selfcheck.run` is replaced because one of its probes opens a real TLS connection to the
    vendor's login host: useful once per app start on a hub, unacceptable in a unit test.
    Everything else in `on_init` runs for real, so the seeding behaviour below is the shipping
    code path rather than a re-implementation of it.
    """
    monkeypatch.setattr(app_module.selfcheck, "run", lambda log: None)

    def _make(**kwargs):
        homey = make_homey(**kwargs)
        instance = app_module.IparkingApp(homey=homey)
        asyncio.run(instance.on_init())
        return instance, homey

    return _make


class _NoNetwork:
    """A login that records instead of dialling out."""

    def __init__(self, api):
        self.api = api
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        self.api.access_token = "11111111-2222-3333-4444-555555555555"


def _stub_login(api):
    login = _NoNetwork(api)
    api.login = login
    return login


# --- the shared session -------------------------------------------------------


def test_shared_api_refuses_before_an_account_exists(make_app):
    instance, _homey = make_app()
    with pytest.raises(NeedCredentialsError):
        asyncio.run(instance.shared_api())


def test_shared_api_returns_the_same_object_every_time(make_app):
    """One session per account is the whole point: the vendor's token is minted per account with
    no refresh endpoint, so a second login is a second token and the first one's fate is
    unverified."""
    instance, _homey = make_app(
        settings={SETTING_USERNAME: "iparking-dev", SETTING_PASSWORD: "pw"}
    )
    first = instance._client("iparking-dev", "pw")
    _stub_login(first)

    assert asyncio.run(instance.shared_api()) is first
    assert asyncio.run(instance.shared_api()) is first


def test_shared_api_logs_in_once(make_app):
    """The lock exists so devices starting together do not race into a login storm; the
    `logged_in` guard is what makes the second call free."""
    instance, _homey = make_app(
        settings={SETTING_USERNAME: "iparking-dev", SETTING_PASSWORD: "pw"}
    )
    login = _stub_login(instance._client("iparking-dev", "pw"))
    asyncio.run(instance.shared_api())
    asyncio.run(instance.shared_api())

    assert login.calls == 1


def test_a_credential_change_repoints_the_same_object(make_app):
    """Replaced instead of repointed is the classic silent version of this bug: the new client
    works for whoever asks next, while everything already holding the old one keeps using
    credentials the server has started rejecting."""
    instance, _homey = make_app()
    first = instance._client("iparking-dev", "old-pw")
    first.access_token = "11111111-2222-3333-4444-555555555555"
    first.memb_name = "999동9999호"

    second = instance._client("iparking-dev", "new-pw")

    assert second is first
    assert second.password == "new-pw"
    # Cleared, so the next request logs in with the new credentials rather than reusing a token
    # minted for the old ones.
    assert second.access_token == ""
    # And the account description goes with the token: a repointed session must not answer for
    # the previous account, least of all with its home address.
    assert second.memb_name == ""
    assert second.auth_entries == []


def test_an_unchanged_credential_leaves_the_session_alone(make_app):
    """`check_connection` reads the *saved* credentials, so this comparison is what stops that
    button from logging every running device out."""
    instance, _homey = make_app()
    first = instance._client("iparking-dev", "pw")
    first.access_token = "11111111-2222-3333-4444-555555555555"

    assert instance._client("iparking-dev", "pw") is first
    assert first.access_token != ""


def test_logout_disables_the_object_the_devices_already_hold(make_app):
    """There is no device registry to reach through — `homey` exposes no get_devices/get_driver
    in this Python surface — so the object they already hold *is* the seam, and flipping its
    kill flag is what actually stops their traffic."""
    instance, _homey = make_app()
    session = instance._client("iparking-dev", "pw")
    session.access_token = "11111111-2222-3333-4444-555555555555"

    returned = asyncio.run(instance.logout())

    assert returned is session
    assert session.disabled is True
    # The in-memory token is the only copy of it that exists, and it went with the logout.
    assert session.access_token == ""


def test_logout_keeps_the_object_so_the_recovery_can_be_real(make_app):
    """Dropping `self._api` as well would make the logout permanent: `_client` would build a new
    client on the next login while every device went on holding the disabled one, and nothing
    would ever clear `disabled`."""
    instance, _homey = make_app()
    session = instance._client("iparking-dev", "pw")
    asyncio.run(instance.logout())

    assert instance._api is session


def test_reauth_re_enables_a_logged_out_session(make_app):
    """It has to happen here rather than in `_client`: after clearing the account the user
    typically re-enters *the same* one, so `_client`'s `!=` comparison never fires and a
    re-enable hung off it would never run."""
    instance, _homey = make_app()
    session = instance._client("iparking-dev", "pw")
    _stub_login(session)
    asyncio.run(instance.logout())

    assert asyncio.run(instance.reauth("iparking-dev", "pw")) is session
    assert session.disabled is False


def test_a_rejected_reauth_leaves_a_logged_out_account_logged_out(make_app):
    """Otherwise a wrong password would let every device resume polling with credentials the
    server just rejected."""
    instance, _homey = make_app()
    session = instance._client("iparking-dev", "pw")

    async def _reject():
        raise IparkingAuthError("로그인에 실패했습니다.")

    session.login = _reject
    asyncio.run(instance.logout())

    with pytest.raises(IparkingAuthError):
        asyncio.run(instance.reauth("iparking-dev", "wrong"))
    assert session.disabled is True


def test_reauth_leaves_the_rejected_credentials_on_the_session(make_app):
    """Not a defect but a documented consequence: `reauth` repoints *before* it validates, which
    is exactly why every caller has to restore the saved account on failure
    (`api._restore_shared`, `pairing._restore_shared`)."""
    instance, _homey = make_app()
    session = instance._client("iparking-dev", "good-pw")

    async def _reject():
        raise IparkingAuthError("nope")

    session.login = _reject
    with pytest.raises(IparkingAuthError):
        asyncio.run(instance.reauth("iparking-dev", "wrong-pw"))

    assert session.password == "wrong-pw"


# --- the UI language seed -----------------------------------------------------


def test_seeding_leaves_an_existing_language_alone(make_app):
    """A webview's report always wins: it is the only thing that knows what the user is
    actually looking at."""
    _instance, homey = make_app(settings={SETTING_LANGUAGE: "en"}, language="ko")
    assert homey.settings.values[SETTING_LANGUAGE] == "en"


def test_seeding_records_a_non_english_sdk_answer(make_app):
    _instance, homey = make_app(language="ko-KR")
    assert homey.settings.values[SETTING_LANGUAGE] == "ko"


def test_seeding_does_not_record_english(make_app):
    """`homey.i18n` reports the *app's* language, which resolves to 'en' on this firmware
    regardless of the user. Storing that would look like a choice the user made and stop the
    first webview from correcting it."""
    _instance, homey = make_app(language="en")
    assert homey.settings.values.get(SETTING_LANGUAGE) is None


# --- selfcheck ----------------------------------------------------------------


def _not_after(days_from_now: int) -> str:
    """A `notAfter` in OpenSSL's own format, the way a peer certificate reports it."""
    when = datetime.now(UTC) + timedelta(days=days_from_now)
    return when.strftime("%b %d %H:%M:%S %Y GMT")


def test_expiry_note_reports_the_date_and_the_days_left():
    note = selfcheck.expiry_note("Oct 27 12:00:00 2026 GMT",
                                 now=datetime(2026, 8, 4, tzinfo=UTC))
    assert "2026-10-27" in note
    assert "84d" in note
    assert "WARNING" not in note


def test_expiry_note_warns_inside_the_window():
    """The whole point of the probe: `certifi` is pinned on a hub nobody updates routinely, so
    without this a CA rotation is a silent login outage."""
    note = selfcheck.expiry_note(_not_after(selfcheck.WARN_WITHIN_DAYS - 1))
    assert note.startswith("WARNING")
    assert "certifi" in note


def test_expiry_note_is_quiet_outside_the_window():
    """Asserted at the boundary rather than a comfortable distance from it, because an
    off-by-one here means the warning arrives a month late or fires forever."""
    assert not selfcheck.expiry_note(_not_after(selfcheck.WARN_WITHIN_DAYS + 2)).startswith(
        "WARNING"
    )


def test_expiry_note_warns_at_exactly_the_threshold():
    # +1 hour of slack so `.days` truncation lands on the threshold rather than one below it.
    at_threshold = datetime.now(UTC) + timedelta(days=selfcheck.WARN_WITHIN_DAYS, hours=1)
    note = selfcheck.expiry_note(at_threshold.strftime("%b %d %H:%M:%S %Y GMT"))
    assert note.startswith("WARNING")


def test_expiry_note_reports_an_already_expired_certificate():
    note = selfcheck.expiry_note(_not_after(-1))
    assert note.startswith("EXPIRED")
    assert "certifi" in note


def test_the_transport_policy_probe_names_both_hosts_and_their_schemes():
    """Not a measurement — `client._require_scheme` is what asserts the reached scheme. This
    line exists so the asymmetry is on the record in its own words every start, because "surely
    both should be https" is the most likely well-meant regression in this app."""
    line = selfcheck._schemes()

    assert "oauth.parkingcloud.co.kr -> https (required https)" in line
    assert "members.iparking.co.kr -> http (no floor, by policy)" in line


def test_a_failing_probe_is_reported_rather_than_raised():
    """A failing probe belongs in the log, not in the way of the app starting."""
    def _boom():
        raise RuntimeError("no route to host")

    line = selfcheck._probe("oauth-handshake", _boom)
    assert line.startswith("oauth-handshake: FAILED (RuntimeError: no route to host)")


def test_run_emits_every_probe(monkeypatch):
    monkeypatch.setattr(selfcheck, "PROBES", (("stub", lambda: "ok"),))
    lines = []
    selfcheck.run(lines.append)

    assert lines == ["--- runtime self-check ---", "stub: ok", "--- end self-check ---"]
