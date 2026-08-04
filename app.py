"""iParking Visitor Parking — 방문 차량 관리 for a Korean apartment complex, on Homey.

Copyright 2026, Geunwon Mo (mokorean@gmail.com). MIT.

A **clean-room** client for iParking MEMBERS: it is not a port of anyone else's code, and
its protocol knowledge comes from reverse-engineering the vendor's own web bundles, recorded
verbatim in `docs/RECON.md`. Architecturally it mirrors the sibling app
`com.lomohome.navien` — one app-level shared session, a `homey`-free pure client, and
credentials in app settings.

**Registering a visitor vehicle acts on a real building's access-control system.** That fact
shapes the whole app, and most of all `iparking_lib/iparking/client.py`'s `register()`. Read
that docstring before changing anything on the write path.
"""

import asyncio
import sys
from pathlib import Path

# The Homey runner may not put the app directory on sys.path.
sys.path.insert(0, str(Path(__file__).parent))

from homey import app as homey_app

from iparking_lib import compat, selfcheck
from iparking_lib.const import (
    SETTING_LANGUAGE,
    SETTING_PASSWORD,
    SETTING_USERNAME,
)
from iparking_lib.iparking.client import IparkingApi, NeedCredentialsError

_NEED_LOGIN = "먼저 앱 설정에서 아이파킹 계정으로 로그인하세요."


class IparkingApp(homey_app.App):
    async def on_init(self) -> None:
        self._api = None
        self._api_lock = asyncio.Lock()
        # Read-only probes, every one of them reported rather than raised. The handshake one
        # is the app's only early warning that the login host's certificate is about to
        # expire while certifi sits pinned on a hub nobody updates.
        selfcheck.run(self.log)
        await self._seed_ui_language()
        self.log("iParking Visitor Parking app is running...")

    def _client(self, username: str, password: str) -> IparkingApi:
        """The one shared `IparkingApi` object, kept *stable* — its credentials are updated
        in place rather than replaced, so a device or a settings-page handler that already
        holds a reference picks up a repair (password change) without a re-init.

        Replacing the object instead would be the classic silent version of this bug: the
        new client would work perfectly for whoever asked next, while everything already
        holding the old one kept using credentials the server has started rejecting, and
        nothing anywhere would report a failure.
        """
        if self._api is None:
            self._api = IparkingApi(username=username, password=password, log=self.log)
        elif self._api.username != username or self._api.password != password:
            compat.repoint_credentials(self._api, username, password)
        return self._api

    async def shared_api(self) -> IparkingApi:
        """The single iParking session shared by every device, Flow card and settings-page
        handler on this account.

        One session, because the vendor's `access_token` is minted per account with no
        refresh endpoint: a second login mints a second token, and there is no evidence the
        first survives it. So each caller holding its own login would be a login storm that
        could bounce the others. The lock serialises the first login specifically so devices
        starting together do not race into one.

        The token this mints lives on the returned object and **nowhere else** — see the
        note in `iparking_lib/const.py` under "Settings keys" for why that is deliberate
        rather than an oversight.
        """
        async with self._api_lock:
            username = await compat.setting_get(self.homey, SETTING_USERNAME)
            password = await compat.setting_get(self.homey, SETTING_PASSWORD)
            if not username or not password:
                raise NeedCredentialsError(_NEED_LOGIN)
            api = self._client(username, password)
            if not api.logged_in:
                await api.login()
            return api

    async def logout(self) -> IparkingApi | None:
        """Stop every running caller before the account is removed.

        Dropping `self._api` does nothing on its own: each device caches the object it was
        handed and never asks for another one, so it would keep polling a live session for
        an account the user just deleted. There is no device registry to reach through
        either — `homey` exposes no get_devices/get_driver in this Python surface — so the
        object the devices already hold *is* the seam, and `IparkingApi.logout()` flipping
        its `disabled` flag is what actually stops their traffic. It also drops the
        in-memory token, which is the only copy of it that exists.

        `self._api` is deliberately *kept*. Clearing it as well would make the logout
        permanent: `_client` would build a brand-new client on the next login while every
        device went on holding the disabled one, and nothing would ever clear `disabled`.
        Devices only re-fetch the session if their poll task dies, and that loop catches
        everything short of CancelledError — so re-entering correct credentials would
        appear to succeed while every device stayed dead until the app restarted. Keeping
        the one object and letting `reauth` re-enable it is what makes the recovery real,
        and it is also the only shape that preserves one session per account.

        Returns the disabled client so a caller can assert on it.
        """
        async with self._api_lock:
            api = self._api
            if api is not None:
                api.logout()
            return api

    async def reauth(self, username: str, password: str) -> IparkingApi:
        """Point the shared session at new credentials and log in to validate them.

        Used by the settings page and by the driver's repair view: it updates the one shared
        client in place (so running devices recover on their next request) and raises if the
        credentials are wrong — the caller only saves them once this succeeds.

        Because the update is in place and happens *before* `login()` is attempted, a
        rejected password leaves the shared session holding it; every caller must restore
        the saved account on failure (`pairing._restore_shared`, `api._restore_shared`).

        This is also where a logged-out session comes back to life, and it has to be here
        rather than in `_client`: after clearing the account the user typically re-enters
        *the same* one, so `_client`'s `!=` comparison never fires and a re-enable hung off
        it would never run. `disabled` is cleared for the attempt and put back if the
        attempt fails, so a wrong password leaves a logged-out account logged out instead of
        letting every device resume polling with credentials the server just rejected.

        Returns the shared client so the caller can read `auth_entries` / `can_register` off
        the session it just validated instead of opening a second one.
        """
        async with self._api_lock:
            api = self._client(username, password)
            was_disabled = api.disabled
            api.disabled = False
            try:
                await api.login()
            except Exception:
                api.disabled = was_disabled
                raise
            return api

    async def _seed_ui_language(self) -> None:
        """Give the Python side *a* language to speak before any webview has reported one.

        Every user-visible sentence this app produces is raised from Python carrying an i18n
        key, and `iparking_lib/i18n.py` resolves those keys itself — precisely because
        `homey.i18n` reports the *app's* language rather than the viewer's. The settings page
        corrects this on load (`POST /language`), but a Flow action can fire before any page
        has ever been opened, and until then the fallback is English.

        Korean is the better first guess for this app: the service, the building and every
        vendor-supplied string in it are Korean. So the SDK's accessor is asked, and its
        answer is only stored if it agrees — a resolved 'en' is left unsaved rather than
        recorded as the user's choice, so the first webview to load still gets to set it.
        """
        if await compat.setting_get(self.homey, SETTING_LANGUAGE):
            return
        reported = await compat.language(self.homey, default="")
        if reported and reported != "en":
            await compat.remember_ui_language(self.homey, reported)
            self.log(f"UI language seeded from the SDK: {reported}")


homey_export = IparkingApp
