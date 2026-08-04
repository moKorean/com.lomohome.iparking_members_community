"""One paired parking lot: the 오늘 등록 sensor, its 자주 오는 차량 tile buttons, and the
register write path.

**The sensor is 오늘 등록된 차량 수 — how many vehicles are registered for today at this lot**
(`iparking_today_count`). It replaced 주차장명 in v0.1.4, and the swap is the whole argument for
the poll that refreshes it. 주차장명 was the lot's name: *also* the name Homey shows for the
device, so the tile printed the same string twice, and constant for the lifetime of a pairing —
24 requests a day to re-confirm it was waste, so it and its loop were deleted. This count changes
whenever anybody registers a car, including on the vendor's own website where this app cannot see
it happen. Polling a constant was waste; polling a count is what makes it true.

**`CANCEL` rows are not counted.** 취소 flips `inot_status` and leaves the row in the list, so a
day's rows are frequently mostly cancellations — counting them showed 6 on the maintainer's own
account where the honest answer was 1. The rule lives once, in `client.count_registered_on`, over
the same `ACTIVE_STATUSES` set the register path's recovery re-query uses.

**Three things keep the number honest**, and each is a defect avoided rather than a nicety:

1. **The date window is recomputed on every tick** from `dates.today_api()`, never cached at
   `on_init`. A cached window would survive KST midnight and leave yesterday's count on the tile
   until the app restarted — the tile would be wrong for a whole day and would look fine.
2. **This app's own actions update it immediately**, at **zero extra requests**: a register from
   a Flow card or a tile press refreshes it, and every settings-page history fetch feeds its rows
   straight into the count (`note_history`), which is what covers the settings page's register
   and cancel — the page re-reads the table after both. The poll is therefore only there to catch
   registrations made elsewhere and the midnight rollover.
3. **A one-day window** (`startDate == endDate == today`) keeps the response small, and the date
   is asserted client-side anyway — see `count_registered_on`.

**Poll cadence** is 3600 s ± 10 % with a 0–10 % start offset and **one request per tick** —
24 requests/day/device. See `const.POLL_INTERVAL_S`; there is still no `poll_interval` setting.
Two consecutive failures mark the device unavailable, because the capability keeps the last count
it read: without the transition, a lot that stopped answering looks exactly like a lot with no
visitors today.

**The register path (item 7).** `flow_register` is the whole run listener body, shared by both
register cards — `register_visitor` (plate + optional date) and `register_visitor_today` (plate
only), which differ only in where `visit_date` comes from — and by a tile press. It writes to a
real building's access-control system, so it delegates every consequential decision to
`client.register()` — zero retries, the hourly ceiling, the fresh-budget recovery re-query, the
status-filtered existence predicate — and adds exactly two things of its own:

1. **The success notification always echoes the date that was actually used**, rendered by
   `dates.format_kst_human`. This is not decoration. A Homey Flow `date` argument in
   `mm-dd-yyyy` field order is *shape-identical* to `dd-mm-yyyy`, both readings are real
   dates, and a wrong day on access control fails **silently** — the guest discovers it at a
   closed gate. The echo is how a misparse is caught on first use instead.
2. **The raw `visit_date` string is logged**, so the on-device format question (§3.6) can be
   settled from an ordinary Flow run rather than a special probe. It is a calendar date, not
   personal data, so logging it verbatim breaks no rule here.

**`already_registered` is notified, never raised.** Re-entering a plate that is already
registered is the most likely real outcome of a first use; a Flow that read that benign
duplicate as a failed action would fire its own error branch over nothing.
`RegisterUncertain` *is* raised — but only after its own notification, and the text it carries
never invites a retry, because a retry is what turns one uncertain write into two real
registrations at a building.

**자주 오는 차량 — the tile buttons.** Twenty device settings (10 이름 + 10 차량번호) and, for
every *complete and valid* pair, one button on the tile that registers that plate for **today in
KST**. Three mechanics carry the whole feature, and each is a constraint rather than a choice:

1. A tile button is a `boolean` capability with `uiComponent: "button"`, and Homey has **no
   dynamic-capability declaration** — so ten are declared in `app.json` up front and each
   device adds or removes them itself (`add_capability` / `remove_capability`), which is what
   makes a lot with two favourites show exactly two buttons rather than ten.
2. The label comes from `set_capability_options(..., {"title": …})` at runtime, because
   `엄마차` is typed after install and no static manifest can carry it.
3. The schemas are **`getable: false`**, exactly like Homey's own `button` capability, and that
   is what makes the tile draw a momentary push button. v0.1.3 used `getable: true` and then
   wrote `false` back after every press to "un-latch" it; that was inverted reasoning and the
   maintainer's hub showed it — the readable value *was* the latch, the tile sat lit like a
   switch that had been flipped on, and the reset existed only to undo a problem it had itself
   created. **Nothing in this module writes a capability value at all any more.**

**A pair counts only when both halves are present and the plate validates.** Half a pair or a
typo'd plate produces no button *and a log line saying which slot and why* — silence there
would leave a user staring at a button that never appeared with nothing to read.

**The one runtime settings write in this app is here, and it is deferred on purpose.** When a
plate validates, the normalized form is written back (`12가 3456` → `12가3456`) so the
maintainer *sees* what was stored, the same visibility rule the settings page follows on blur.
It cannot be written from inside the settings callback: the SDK refuses that outright
(`Device.set_settings` raises `Cannot set settings while on_settings is still pending`, and
`_on_settings` then overwrites its own cached settings with the frozen incoming dict anyway), so
the write is **scheduled** and lands one turn of the event loop later. See
`_schedule_normalize`. That is a **settings** write, not a store write — see the paragraph
below, which still holds.

**No runtime store writes** (criterion 18). Nothing here calls `set_store_value`: `stor_seq`,
`park_seq` and `lot_id` are fixed at pairing. The pattern for a store write exists in
`com.lomohome.localthings` if a later version needs one. Settings are a different object with
a different lifetime — they are user input, editable on the device page — so normalizing one
in place breaks none of that invariant's reasoning.
"""

import asyncio
import collections
import random

from homey import device

from iparking_lib import compat, i18n
from iparking_lib.const import (
    CAPABILITY_PARK_NAME,
    CAPABILITY_TODAY_COUNT,
    MAX_FAVORITES,
    MAX_POLL_FAILURES,
    POLL_BACKOFF_S,
    POLL_INTERVAL_S,
    POLL_JITTER,
    POLL_START_JITTER,
    SETTINGS_WRITE_ATTEMPTS,
    SETTINGS_WRITE_RETRY_S,
    STORE_LOT_ID,
    STORE_PARK_SEQ,
    STORE_STOR_SEQ,
    favorite_name_setting,
    favorite_plate_setting,
    quick_capability,
)
from iparking_lib.iparking import codes, dates
from iparking_lib.iparking.client import (
    IparkingError,
    RegisterUncertain,
    count_registered_on,
)
from iparking_lib.iparking.plate import (
    InvalidPlateError,
    mask_plate,
    normalize_plate,
    strip_plate,
)

#: Shown while today's count cannot be read. Deliberately not "logged out" — the account may be
#: fine and the vendor's API simply unreachable, and the tile has no room to explain both.
_UNAVAILABLE = "오늘 등록 현황을 가져올 수 없습니다"

#: One usable favourite: its 1-based slot, the label its button carries, and the **normalized**
#: plate. Never constructed for half a pair or an invalid plate — see `read_favorites`.
Favorite = collections.namedtuple("Favorite", "index name plate")


def read_favorites(settings) -> tuple[list[Favorite], list[str]]:
    """Every complete, valid (이름, 차량번호) pair in `settings`, plus a reason per rejected slot.

    **A pair counts only when both halves are present and the plate validates.** The plate goes
    through `normalize_plate`, so the maintainer's own example — `12가 3456`, *with a space* —
    is accepted and stored without it; that is the whitespace-stripping case `plate.py` exists
    for, and rejecting it would make the feature fail on its first use.

    Rejections are **returned rather than dropped**: a name with no plate, a plate with no name,
    or a plate the vendor's regex refuses each yield a sentence for the log. A user who typed a
    plate wrong is otherwise looking at a button that never appeared, with nothing anywhere to
    explain it. The plate is masked in those sentences (criterion 14) and the *name* is left out
    of them entirely — it is a nickname the user chose, diagnostic output gets pasted into
    issues, and the tile already shows it to the only person who needs it.

    Takes any mapping and coerces it with `dict(...)` rather than testing `isinstance(…, dict)`:
    this runtime's `get_settings()` returns a `MappingProxyType`, which is a perfectly usable
    mapping and not a `dict` subclass. An `isinstance` guard here silently discarded every
    favourite on a real hub once — see `_device_settings` for the full story.
    """
    try:
        values = dict(settings)
    except (TypeError, ValueError):
        values = {}
    favorites: list[Favorite] = []
    rejected: list[str] = []
    for index in range(1, MAX_FAVORITES + 1):
        name = str(values.get(favorite_name_setting(index)) or "").strip()
        raw = str(values.get(favorite_plate_setting(index)) or "")
        stripped = strip_plate(raw)
        if not name and not stripped:
            continue
        if not stripped:
            rejected.append(f"슬롯 {index}: 이름만 있고 차량번호가 비어 있습니다")
            continue
        if not name:
            rejected.append(
                f"슬롯 {index}: 차량번호 {mask_plate(stripped)} 만 있고 이름이 없습니다"
            )
            continue
        try:
            plate = normalize_plate(raw)
        except InvalidPlateError:
            rejected.append(
                f"슬롯 {index}: 차량번호 {mask_plate(stripped)} 형식이 올바르지 않습니다"
            )
            continue
        favorites.append(Favorite(index, name, plate))
    return favorites, rejected


def _new_settings(args, kwargs) -> dict | None:
    """The post-change settings out of whatever shape this runtime calls `on_settings` with.

    The Python SDK calls it with three keywords — `old_settings`, `new_settings`,
    `changed_keys` — but Node's SDK3 hands over one event object
    (`{oldSettings, newSettings, changedKeys}`) and SDK2 passed `(old, new, changed)`
    positionally. All of them are accepted, because this hook is the one place where getting the
    shape wrong presents as settings that save and never produce a button, with a `TypeError`
    buried in a hub log nobody is watching.

    `None` means "could not tell", which the caller answers by re-reading `get_settings()`.
    That fallback is why the tolerance here can afford to be conservative rather than clever.

    The positional case is the one with a trap: with `(old, new, changed)` the **second** dict
    is the new one, and picking the first would reconcile the buttons against the settings the
    user just replaced — i.e. the feature would appear to lag one edit behind.
    """
    for key in ("newSettings", "new_settings"):
        value = kwargs.get(key)
        if isinstance(value, dict):
            return value
    dicts = [value for value in args if isinstance(value, dict)]
    if not dicts:
        dicts = [value for value in kwargs.values() if isinstance(value, dict)]
    for value in dicts:
        for key in ("newSettings", "new_settings"):
            inner = value.get(key)
            if isinstance(inner, dict):
                return inner
    if len(dicts) >= 2:
        return dicts[1]
    return dicts[0] if dicts else None


def _prefixed(text: str, label: str) -> str:
    """`text` with the favourite's name in front, so a notification says *which* button fired.

    No locale key of its own: the sentence is unchanged, only attributed, and the name is user
    input that no locale file could ever carry.
    """
    return f"{label} · {text}" if label else text


class VisitCarDevice_(device.Device):

    async def on_init(self) -> None:
        store = self.get_store()
        self._stor_seq = _int(store.get(STORE_STOR_SEQ))
        self._park_seq = _int(store.get(STORE_PARK_SEQ))
        self._lot_id = str(store.get(STORE_LOT_ID) or "")
        # The store rather than `get_data()`: both hold `lot_id` (the driver writes it to
        # each), and the store is the one object the driver and the device already share.
        self.log(
            f"iparking: visitcar init lot={self._lot_id or '?'} "
            f"park_seq={self._park_seq} stor_seq={self._stor_seq}"
        )

        # Which quick capabilities already carry a press handler. Per device lifetime, because
        # binding a listener twice for one capability is not something the SDK's contract
        # promises to tolerate and `_reconcile_buttons` runs on every settings save.
        self._listening: set[str] = set()
        # Guards `on_settings` against re-entry by the deferred write below.
        self._settings_busy = False
        # The scheduled normalized-plate write, so `on_uninit` can cancel it and a second save
        # can supersede it. See `_schedule_normalize`.
        self._normalize_task: asyncio.Task | None = None
        # `None` rather than 0 until something has actually been read: 0 is a real answer — a day
        # with no visitors — and starting there would log a "changed to 0" that never happened.
        self._today_count: int | None = None

        # Both guarded, and independently: a runtime that exposes no capability-mutation surface
        # at all must still reach the end of `on_init` with a working register path, which is
        # the requirement that survives everything else here.
        try:
            await self._shed_park_name()
        except Exception as exc:
            self.log(f"iparking: the 주차장명 sensor could not be removed: {exc}")
        try:
            await self._adopt_today_count()
        except Exception as exc:
            self.log(f"iparking: the 오늘 등록 sensor could not be added: {exc}")
        # Reconciled at init as well as on every save, so a hub restart does not lose the
        # buttons — a paired device is re-created from scratch and `add_capability` is the only
        # thing that puts them back.
        try:
            await self._reconcile_buttons()
        except Exception as exc:
            self.log(f"iparking: 자주 오는 차량 buttons could not be reconciled: {exc}")

        self._api = None
        # Set on teardown so the poll task's done-callback can tell "died" from "dismantled".
        self._closing = False
        self._restart_step = 0
        self._restart_delay = POLL_BACKOFF_S[0]
        # REST is this device's only link, so consecutive failed reads are its only
        # availability signal. A count rather than a timestamp, because the rule below is
        # written in cycles and a second, time-based expression of it could only disagree.
        self._failures = 0
        self._poll_task = asyncio.create_task(self._run())
        self._poll_task.add_done_callback(self._on_poll_task_done)

    async def on_uninit(self) -> None:
        await self._teardown()

    async def _teardown(self) -> None:
        """Cancel everything this device has in flight, then **await** it.

        Two things: the poll task, and the deferred normalized-plate write. Both are awaited
        rather than merely cancelled, so nothing is still running once Homey considers the device
        gone — a `set_capability_value` or a `set_settings` that lands afterwards is a write
        against a torn-down object.
        """
        self._closing = True
        pending = []
        task = getattr(self, "_poll_task", None)
        if task is not None:
            task.cancel()
            pending.append(task)
        write = self._normalize_task
        self._normalize_task = None
        if write is not None:
            write.cancel()
            pending.append(write)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _shed_park_name(self) -> None:
        """Remove the 주차장명 sensor from a device paired by an earlier version.

        This is what spares every existing user a re-pair. It is a no-op on a freshly paired
        device — the capability is not in `driver.compose.json` any more — and on any device
        that has already been through one `on_init` since the upgrade.
        """
        if CAPABILITY_PARK_NAME not in self.get_capabilities():
            return
        self.log("iparking: removing the 주차장명 sensor left over from an earlier version")
        await self._sdk_call(
            ("remove_capability", "removeCapability"), CAPABILITY_PARK_NAME
        )
        # It never had a listener, but the discard keeps one rule for every removal rather than
        # two: a capability that goes away takes its listener registration with it.
        self._listening.discard(CAPABILITY_PARK_NAME)

    async def _adopt_today_count(self) -> None:
        """Add the 오늘 등록 sensor to a device paired by an earlier version.

        The mirror of `_shed_park_name`, and it is needed for the same reason that one is:
        **`driver.compose.json`'s capability list only applies to a device at the moment it is
        paired.** Declaring the sensor there gives it to new devices and to nobody else, so
        without this an existing user upgrades, loses 주차장명 as intended, and gains nothing —
        the tile ends up with only its buttons and the new sensor is invisible.

        Verified on hardware: after the upgrade the paired device reported
        `capabilities: ['iparking_quick_1', 'iparking_quick_2']` and no count at all.

        A no-op on a freshly paired device, and idempotent, so a second `on_init` costs nothing.
        """
        if CAPABILITY_TODAY_COUNT in self.get_capabilities():
            return
        self.log("iparking: adding the 오늘 등록 sensor to a device paired before it existed")
        await self._sdk_call(
            ("add_capability", "addCapability"), CAPABILITY_TODAY_COUNT
        )

    # --- 오늘 등록: the sensor and its poll -----------------------------------

    def _on_poll_task_done(self, task) -> None:
        """Restart the poll loop if it died; stay out of the way if it was torn down.

        Both halves are load-bearing, and both are copied from navien's airmonitor because
        both were bugs there first. `task.cancelled()` is the only thing separating a
        dismantled device from a crashed loop. And `_poll_task` **must** be reassigned:
        otherwise a later `on_uninit` cancels the dead original, the restarted loop outlives
        the device, and it keeps calling `set_capability_value` on a torn-down Device.
        """
        if task.cancelled() or self._closing:
            return
        exc = task.exception()
        self.log(f"iparking: poll task died ({exc!r}); restarting in {self._restart_delay}s")
        self._poll_task = asyncio.create_task(self._restart_poll())
        self._poll_task.add_done_callback(self._on_poll_task_done)

    async def _restart_poll(self) -> None:
        """Wait one `POLL_BACKOFF_S` step, then re-enter `_run`.

        Every restart is logged with its exception by the caller, so a line that keeps
        repeating is the signal that a real crash is hiding inside the loop.
        """
        await asyncio.sleep(self._restart_delay)
        self._restart_step = min(self._restart_step + 1, len(POLL_BACKOFF_S) - 1)
        self._restart_delay = POLL_BACKOFF_S[self._restart_step]
        await self._run()

    async def _acquire_api(self) -> None:
        """Wait for the app-wide shared session, however long the account takes to appear.

        A device can be paired and then the account cleared, so "no credentials yet" is a
        normal state to sit in rather than an error to die on. The tile says so meanwhile.
        """
        delay = 5
        while True:
            try:
                self._api = await compat.shared_api(self.homey)
                return
            except Exception as exc:
                self.log(f"iparking: login pending ({exc}); retrying in {delay}s")
                await self._safe_unavailable("아이파킹 로그인 재시도 중…")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)

    async def _run(self) -> None:
        # Guarded as one block: an unguarded boot failure here would escape `_run` before the
        # loop below was ever reached, and this device would then poll never again until the
        # app restarted. (That exact asymmetry was a real bug in the sibling app.)
        try:
            await self._acquire_api()
            await self._poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(f"iparking: initial poll failed: {exc}")
        # The boot attempt is deliberately outside the two-cycle budget: a device that has never
        # answered has no count to be wrong about, so counting it would make the first real cycle
        # the second strike.
        self._failures = 0
        # One-shot 0–10 % offset on the first loop sleep only, so lots paired together stop
        # ticking in lockstep from boot.
        offset = POLL_INTERVAL_S * random.uniform(0.0, POLL_START_JITTER)
        while True:
            delay = POLL_INTERVAL_S * random.uniform(1 - POLL_JITTER, 1 + POLL_JITTER) + offset
            offset = 0.0
            self.log(f"iparking: next poll in {delay:.0f}s")
            await asyncio.sleep(delay)
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"iparking: poll failed: {exc}")

    async def _poll_once(self) -> None:
        """**One** request: today's 등록 내역 for this lot, counted.

        **The window is recomputed here, on every tick, and never cached anywhere.** That is the
        midnight rollover: at 00:00 KST the answer becomes "today's registrations", for the new
        today, and a window captured at `on_init` would keep reporting yesterday's number until
        the app happened to restart — wrong for a whole day, and indistinguishable on the tile
        from a correct answer.

        A one-day window also keeps the response small; the date is asserted again inside
        `count_registered_on`, because the vendor's own filtering rules were never characterised
        and a bare number on a tile cannot reveal that it silently covered three months.
        """
        today = dates.today_api()
        try:
            rows = await self._api.history(
                park_seq=self._park_seq,
                stor_seq=self._stor_seq,
                start_date=today,
                end_date=today,
            )
        except Exception:
            self._failures += 1
            await self._update_availability()
            raise
        # Reset on an explicit success, never inferred from the absence of an exception.
        self._failures = 0
        await self._apply_count(count_registered_on(rows, today), today)
        await self._update_availability()

    async def note_history(self, park_seq: int, stor_seq: int, rows) -> None:
        """Update the count from rows somebody else already fetched. **Costs no request.**

        Called by the settings page's `GET /history` handler, which is what keeps the tile
        correct the instant a user registers or cancels there: `form.js` re-reads the table after
        both actions, so this one hook covers both without a second round trip.

        Silently ignores a fetch for a different lot, and a window that does not include today —
        the settings table's default window is three months, and this must not turn a wide fetch
        into a count of it. `count_registered_on` does that filtering; the guard here is only
        about *which device* the rows belong to.

        Never raises: it is a courtesy update hanging off somebody else's request, and a failure
        here must not turn a successful history fetch into an error on the page.
        """
        if int(park_seq) != self._park_seq or int(stor_seq) != self._stor_seq:
            return
        try:
            today = dates.today_api()
            await self._apply_count(count_registered_on(rows, today), today)
        except Exception as exc:
            self.log(f"iparking: 오늘 등록 count could not be updated from a history read: {exc}")

    async def refresh_today_count(self) -> None:
        """Re-read today's count, now. One request, and never fatal.

        Called after this app's own register so the tile is right the moment the user acts, rather
        than up to an hour later. The alternative — incrementing the number we already had —
        was rejected: it would put a value on the tile that no server ever confirmed, and this
        capability's whole job is to report what the vendor says is registered.
        """
        try:
            if self._api is None:
                # The register path holds its own session handle, so a press can land before the
                # poll loop has stored one — most likely on the very first press after a restart.
                # Taking the shared session here rather than giving up keeps that press's tile
                # update working instead of leaving it silently to the next tick.
                self._api = await compat.shared_api(self.homey)
            await self._poll_once()
        except Exception as exc:
            self.log(f"iparking: 오늘 등록 count refresh failed: {exc}")

    async def _apply_count(self, count: int, today: str) -> None:
        value = int(count)
        if value != self._today_count:
            self.log(f"iparking: 오늘({today}) 등록된 차량 {value}대")
            self._today_count = value
        await self._set(CAPABILITY_TODAY_COUNT, value)

    async def _update_availability(self, reason: str = _UNAVAILABLE) -> None:
        """`set_unavailable` after **two** consecutive failures, `set_available` on recovery.

        Two rather than one: a single dropped request against a cloud API addressed over plain
        HTTP is ordinary, two in a row is a pattern. And unavailability has to be *said* here
        rather than inferred from the tile, because `_poll_once` keeps the last count it read —
        so a lot that stopped answering would otherwise look exactly like a lot with no visitors
        today, which is the most ordinary reading of all.
        """
        if self._failures >= MAX_POLL_FAILURES:
            await self._safe_unavailable(reason)
        else:
            await self._safe_available()

    # --- the register path --------------------------------------------------

    async def flow_register(
        self, car_number: str, visit_date: str = "", *, label: str = ""
    ) -> bool:
        """The register path behind **both** Flow actions. **This writes to a building.**

        Two modes, and they are one argument apart, exactly as the maintainer asked for: a
        plate alone registers for today; a plate plus a date registers for that date. That is
        `visit_date`'s `required: false` in the card definition doing the work — the empty case
        falls through to `dates.today_kst()` via `client.register`, so both modes share one
        parse path and one window check.

        `register_visitor_today` is the same first mode reached from a card with no date field
        at all: its listener passes `visit_date=""` and nothing else about this method changes.
        There is deliberately no second register path — the invariants below (zero retries on
        `POST /invitations`, the echoed resolved date, `RegisterUncertain` as its own outcome)
        are only guaranteed once because they are only written once.

        `label` is the third caller: a **tile button** (`press_favorite`), which passes the
        favourite's name and no date. It is a parameter rather than a fork precisely because
        "do not fork the register logic" is the rule that keeps the invariants above true once
        instead of twice — all it changes is which locale keys the notification uses, and one
        thing that cannot be shared: a Flow card has an error branch to route a refusal into,
        while a tile press has nowhere at all, so on the button path a refused write is
        **notified as well as raised**. That is where `can_register: false` becomes visible.

        Returns `True` for a registration *and* for a duplicate. Raises for everything else,
        including `RegisterUncertain`, which is raised only after its own notification.
        """
        # "quick" vs "flow" selects the notification keys and tags the log lines. Both branches
        # reach the same `api.register` below with the same arguments.
        source = "quick" if label else "flow"
        raw = "" if visit_date is None else str(visit_date).strip()
        # Logged verbatim, and at this level on purpose: `log` is the only channel this
        # runtime gives an app, and §3.6's open question — whether Homey hands a `date` arg
        # over as `dd-mm-yyyy` or `mm-dd-yyyy` — is answerable from one ordinary Flow run if
        # and only if this line exists. A calendar date is not personal data; the plate on the
        # next line is, which is why that one is masked and this one is not.
        self.log(
            f"iparking: {source} register {mask_plate(car_number)} "
            f"visit_date raw={raw!r} (empty = today in KST)"
        )
        language = await compat.ui_language(self.homey)

        try:
            api = await compat.shared_api(self.homey)
            result = await api.register(
                car_number=car_number,
                park_seq=self._park_seq,
                stor_seq=self._stor_seq,
                visit_date=raw or None,
            )
        except RegisterUncertain as exc:
            # Distinct, and notified before it is raised: the Flow's own error branch shows a
            # one-line failure, and the outcome here is *not* a failure — it is unknown. The
            # notification is what carries the "do not register again, check the website"
            # text. Nothing retries: not here, not in `register`.
            self.log(f"iparking: {source} register outcome UNCERTAIN; no retry")
            # `_prefixed` attributes it to the button that fired without touching the sentence:
            # the text still says the outcome is unknown, still points at the vendor's website,
            # and still never invites a retry.
            await self._notify(_prefixed(str(exc), label))
            raise
        except (IparkingError, ValueError) as exc:
            # `ValueError` covers `InvalidPlateError` and `DateError` — user-input verdicts
            # that carry their own i18n keys just like the API errors do.
            message = _render(exc, language)
            # A tile press has no error branch. Without this, `can_register: false` — the one
            # refusal a user cannot fix themselves ("관리사무소에 문의하세요") — would land
            # nowhere they could read it.
            if label:
                await self._notify(_prefixed(message, label))
            raise Exception(message) from exc

        outcome = str(result)
        plate = strip_plate(car_number)
        human = dates.format_kst_human(result.api_date, language)
        key = (f"{source}_registered" if outcome == codes.OUTCOME_OK
               else f"{source}_already_registered"
               if outcome == codes.OUTCOME_ALREADY_REGISTERED else "")
        if not key:
            # An explicit `FAIL` for this plate: a verdict from the server, so it is reported
            # as one. (`register` never returns anything else — an unresolved outcome is
            # `RegisterUncertain`, handled above.)
            message = i18n.translate(codes.OUTCOME_FAILED, language)
            if label:
                await self._notify(_prefixed(message, label))
            raise Exception(message)

        # The plate is shown in full here, unmasked, and that is not an oversight either: the
        # masking rule protects *log lines*, which get pasted into issues. A notification is
        # this Flow's answer to the person who wrote the Flow, and "12가****" would make it
        # useless. `flow_registered` is the only place in the app that renders a full plate
        # outside the settings page.
        # `name=` is passed on both paths and ignored by the `flow_*` templates, which do not
        # contain `{name}` — `str.format` tolerates extra keywords, so one call serves both key
        # sets rather than two branches that could drift.
        text = i18n.translate(key, language, name=label, plate=plate, date=human)
        if result.ambiguous:
            # A 2-2-4 date whose first two fields are both ≤ 12 was resolved day-first by
            # policy, not by evidence. Saying so is the mitigation; hiding it is how a
            # visitor ends up at a gate on the wrong day.
            text = f"{text}\n{i18n.translate('date_ambiguous', language, date=human)}"
        await self._notify(text)
        self.log(
            f"iparking: {source} register {mask_plate(plate)} -> {outcome} on {result.api_date}"
        )
        # The tile should be right the moment the user acts, not up to an hour later. **Only when
        # the registration was for today**, because a Flow card registering next Tuesday changes
        # no count and must not spend a request discovering that. Never fatal, and deliberately
        # after the notification: a refresh that fails must not turn a registration that
        # succeeded into an error.
        if str(result.api_date) == dates.today_api():
            await self.refresh_today_count()
        return True

    # --- 자주 오는 차량: the tile buttons -------------------------------------

    async def on_settings(self, *args, **kwargs) -> None:
        """The favourites were edited: normalize the plates, then reconcile the buttons.

        `*args, **kwargs` rather than a declared signature, and that is the point rather than
        laziness: three call shapes are plausible (see `_new_settings`) and a mismatched
        signature here would present as settings that save but never produce a button.

        Normalization happens **in memory first**, so the reconcile below runs against the
        normalized dict and a plate typed as `12가 3456` produces a button on the same save
        rather than the next one. The visible write-back is *scheduled*, because the SDK refuses
        a `set_settings` made while this hook is still pending — see `_schedule_normalize`.
        """
        if self._settings_busy:
            # Reached only if a runtime re-enters this hook for our own `set_settings`. The
            # Python SDK documents that it does not, but a settings write triggered by a
            # settings callback is exactly the shape that loops, and relying on the write
            # converging to stop a loop is not a guarantee.
            self.log("iparking: on_settings re-entered by our own write; ignored")
            return
        self._settings_busy = True
        try:
            values = _new_settings(args, kwargs)
            if values is None:
                self.log("iparking: on_settings gave no readable settings; re-reading them")
                values = await self._device_settings()
            values, writes = self._normalized(values)
            self._schedule_normalize(writes)
            await self._reconcile_buttons(values)
        finally:
            self._settings_busy = False

    def _normalized(self, values: dict) -> tuple[dict, dict]:
        """`(settings with every accepted plate normalized, the subset that changed)`.

        Pure, and the reason the reconcile can run against the new values before the write that
        makes them visible has happened.

        An **invalid** plate is left exactly as typed. Stripping it would half-fix a value that
        is still going to be rejected, and moving the user's text under them while telling them
        nothing is worse than leaving the typo where they can see it.
        """
        writes = {}
        for index in range(1, MAX_FAVORITES + 1):
            key = favorite_plate_setting(index)
            raw = str(values.get(key) or "")
            if not raw:
                continue
            try:
                plate = normalize_plate(raw)
            except InvalidPlateError:
                continue
            if plate != raw:
                writes[key] = plate
        fixed = dict(values)
        fixed.update(writes)
        return fixed, writes

    def _schedule_normalize(self, writes: dict) -> None:
        """Write the normalized plates back **after** `on_settings` has returned.

        This is the app's one runtime settings write, and it has to be deferred rather than
        performed inline. The SDK's own `Device.set_settings` opens with

            if self._on_settings_pending:
                raise HomeyError("Cannot set settings while on_settings is still pending")

        and `_on_settings` clears that flag only in its `finally`, *after* also overwriting its
        cached settings with the frozen incoming dict. So an inline write could not succeed, and
        would not have survived if it had. On the maintainer's hub the inline version failed
        exactly this way: `fav_plate_1` stayed ``12가 3456``, space and all, while
        `_sdk_call` logged the refusal into a hub log nobody was watching.

        A task rather than a callback: `asyncio.create_task` cannot start the coroutine until
        the loop is next free, which is necessarily after `_on_settings` has run to completion —
        there is no `await` in it between `on_settings` returning and the flag being cleared.

        Convergent, and guarded in three ways rather than by hope: nothing is scheduled when
        there is nothing to write; a newer save **cancels** the pending write instead of racing
        it, because the newer values are the true ones; and the write itself stands down if a
        save is in flight when it wakes. If it fails anyway the values on disk are simply still
        the ones the user typed, which the next save normalizes again — the operation is
        idempotent, so a lost write costs visibility for one save and never correctness.
        """
        if not writes:
            return
        task = self._normalize_task
        if task is not None and not task.done():
            task.cancel()
        self._normalize_task = asyncio.create_task(self._write_normalized(writes))

    async def _write_normalized(self, writes: dict) -> None:
        """The deferred half of `_schedule_normalize`.

        Only the keys are logged, never the values — a favourite's plate is still a plate.
        """
        # Yielding one turn was not enough, and could not be: `Device._on_settings` holds
        # `_on_settings_pending = True` across `await self.on_settings(...)`, and our handler
        # awaits the button reconcile *after* scheduling this — so this task gets a turn while
        # the flag is still set, and `set_settings` raises
        # `HomeyError("Cannot set settings while on_settings is still pending")`.
        #
        # The SDK offers no "settings committed" signal to wait on, so rather than guess at an
        # ordering, retry until the window closes. The flag is cleared in a `finally` immediately
        # after the handler returns, so in practice this succeeds on the first or second try; the
        # remaining attempts are there so a slow reconcile cannot silently lose the write.
        #
        # Worth knowing for anyone tempted to simplify: the same method then does
        # `self._settings = frozen_new_settings`, overwriting with the values the user submitted.
        # So a write that lands *too early* is not merely refused — it is discarded. Late is the
        # only safe direction, which is why this retries forward and never pre-empts.
        last = ""
        for attempt in range(1, SETTINGS_WRITE_ATTEMPTS + 1):
            await asyncio.sleep(0 if attempt == 1 else SETTINGS_WRITE_RETRY_S)
            if self._settings_busy:
                self.log("iparking: a newer settings save is in flight; normalized write skipped")
                return
            try:
                # Through `compat.resolve`, not a bare `await`: the SDK's `set_settings` is a
                # coroutine, but a bare await would raise `TypeError` against any surface that
                # returns a plain value — which is exactly what a test caught here.
                await compat.resolve(self.set_settings(writes))
            except Exception as exc:  # noqa: BLE001 — the SDK raises a bare HomeyError here
                last = str(exc)
                if "on_settings is still pending" not in last:
                    self.log(f"iparking: normalized {sorted(writes)} failed: {exc}")
                    return
                continue
            self.log(f"iparking: normalized {sorted(writes)} written (attempt {attempt})")
            return
        self.log(
            f"iparking: normalized {sorted(writes)} gave up after "
            f"{SETTINGS_WRITE_ATTEMPTS} attempts: {last}"
        )

    async def _reconcile_buttons(self, settings: dict | None = None) -> None:
        """Make the tile show exactly one button per complete, valid favourite.

        Runs at `on_init` and on every settings save, and it is written as a full reconcile
        rather than as a diff of the change: `changed_keys` describes one edit while a restart
        has no changed keys at all, and the same code has to serve both. Idempotent by
        construction — every slot is either wanted or not, and both branches are safe to repeat.

        Note what is **not** here any more: there is no write of `False` to a freshly added
        button. The capabilities are `getable: false`, so there is no value to clear.
        """
        values = settings if isinstance(settings, dict) else await self._device_settings()
        favorites, rejected = read_favorites(values)
        for reason in rejected:
            self.log(f"iparking: 자주 오는 차량 {reason} — 버튼을 만들지 않았습니다")
        wanted = {favorite.index: favorite for favorite in favorites}
        self.log(f"iparking: 자주 오는 차량 {len(wanted)} slot(s) ready: {sorted(wanted)}")
        language = await compat.ui_language(self.homey)
        for index in range(1, MAX_FAVORITES + 1):
            capability = quick_capability(index)
            favorite = wanted.get(index)
            present = capability in self.get_capabilities()
            if favorite is None:
                if present:
                    # A favourite that was cleared or broken takes its button with it. Leaving
                    # a stale button behind would leave a live control wired to a plate the
                    # user has already removed.
                    await self._sdk_call(
                        ("remove_capability", "removeCapability"), capability
                    )
                    # Forgotten deliberately: a removed capability takes its listener with it,
                    # so a slot that is filled in again needs a *new* one. Remembering it here
                    # would make `_listen` skip the rebind and leave an inert button — which
                    # looks exactly like a working one until somebody presses it.
                    self._listening.discard(capability)
                continue
            if not present and not await self._sdk_call(
                ("add_capability", "addCapability"), capability
            ):
                continue
            await self._label_button(capability, favorite.name, language)
            self._listen(capability, index)

    async def _label_button(self, capability: str, name: str, language: str) -> None:
        """Put `[엄마차 방문 등록]` on the button.

        `set_capability_options` with a `title` is the only route user input has to a tile
        label — the manifest title is static and `엄마차` is typed after install.

        The title is sent as a **plain string**, and that is settled rather than assumed:
        verified on the maintainer's hub 2026-08-04, where `엄마차 방문 등록` rendered correctly
        on the tile. An `{"ko": …, "en": …}` object is also legal in a *manifest*, but both
        values would be the same user-typed name here, so it buys nothing.

        The name is not in the log line. It is a nickname the user chose (`장모님차`), diagnostic
        output gets pasted into issues, and the tile already shows it to the person who typed it.
        """
        title = i18n.translate("quick_button", language, name=name)
        await self._sdk_call(
            ("set_capability_options", "setCapabilityOptions"),
            capability,
            {"title": title},
            what=f"{capability} title",
        )

    def _listen(self, capability: str, index: int) -> None:
        """Bind the press handler for one button, once per device lifetime."""
        if capability in self._listening:
            return
        for name in ("register_capability_listener", "registerCapabilityListener"):
            fn = getattr(self, name, None)
            if fn is None:
                continue

            async def _pressed(*_args, slot=index, **_kwargs) -> bool:
                # `slot=index` binds the value now: a late-bound closure over the loop
                # variable would give every button whichever slot was reconciled last.
                return await self.press_favorite(slot)

            try:
                fn(capability, _pressed)
            except Exception as exc:
                self.log(f"iparking: {name}({capability}) failed: {exc}")
                return
            self._listening.add(capability)
            self.log(f"iparking: press handler bound via {name}({capability})")
            return
        self.log(
            "iparking: this Device exposes no capability-listener registrar; "
            "자주 오는 차량 buttons would be inert"
        )

    async def press_favorite(self, index: int) -> bool:
        """A tile button was pressed. **This writes to a building.**

        Registers that slot's plate for **today in KST** — `visit_date=""` through
        `flow_register`, i.e. the same `client.register` the Flow cards reach, with the same
        zero retries on `POST /invitations`, the same recovery re-query, the same
        `already_registered` outcome and the same `RegisterUncertain`.

        The settings are re-read here rather than cached from the last reconcile. A button and
        the plate behind it are two different objects on a hub, and the interval between them
        includes the user editing the settings — registering a plate the user has since replaced
        is exactly the kind of write that cannot be taken back.

        Nothing is written back to the capability afterwards. It is `getable: false`, so the
        press left no value behind to reset; the `finally` that used to do it was the latch it
        claimed to be curing.
        """
        values = await self._device_settings()
        favorites, _rejected = read_favorites(values)
        favorite = next((item for item in favorites if item.index == index), None)
        if favorite is None:
            # The slot lost its pair between the button appearing and this press.
            self.log(f"iparking: 자주 오는 차량 슬롯 {index} has no complete pair; no write")
            language = await compat.ui_language(self.homey)
            await self._notify(i18n.translate("quick_unset", language))
            await self._reconcile_buttons(values)
            return False
        return await self.flow_register(
            car_number=favorite.plate, visit_date="", label=favorite.name
        )

    async def _device_settings(self) -> dict:
        """This device's settings as a plain dict. `{}` only when they are truly unreadable.

        Coerces with `dict(values)` rather than testing `isinstance(values, dict)`. That
        distinction cost a working feature once: the runtime's `get_settings()` returns a
        `MappingProxyType`, which is not a `dict` subclass, so an `isinstance` guard discarded a
        perfectly good answer, fell through the loop, and logged "this Device exposes no
        get_settings" — while `dir(self)` showed the bound method right there. Every button
        silently vanished on restart and the log actively pointed away from the cause.

        Two lessons are worth keeping in the code. A tolerance check should accept anything it
        can *use*, not only the one type it expected; and a fallback message must describe what
        was observed, not assume why — hence the distinct log lines below.
        """
        for name in ("get_settings", "getSettings"):
            fn = getattr(self, name, None)
            if fn is None:
                continue
            try:
                values = await compat.resolve(fn())
            except Exception as exc:
                self.log(f"iparking: {name}() failed: {exc}")
                return {}
            try:
                return dict(values)
            except (TypeError, ValueError):
                self.log(
                    f"iparking: {name}() returned {type(values).__name__}, "
                    "which is not usable as a mapping"
                )
                return {}
        self.log(
            "iparking: this Device has neither get_settings nor getSettings; "
            "자주 오는 차량 buttons unavailable"
        )
        return {}

    async def _sdk_call(self, names: tuple[str, ...], *args, what: str = "") -> bool:
        """Call the first of `names` this Device actually has, and **log which one answered**.

        Every spelling this rides on is now confirmed snake_case on hardware
        (`add_capability`, `remove_capability`, `set_capability_options`, `set_settings`,
        `get_settings`, `register_capability_listener`, 2026-08-04). The camelCase fallbacks
        stay because they cost one `getattr` each and the log line names the winner, which is
        what turned six unverifiable guesses into six measured facts in the first place.

        Never fatal. A device that cannot grow a button still has a working register path, which
        is the requirement the maintainer stated first.

        `what` exists so a caller can keep values out of the log line: `set_settings` is handed
        plates, and `str(args)` would print them.
        """
        detail = what or (args[0] if args and isinstance(args[0], str) else "…")
        for name in names:
            fn = getattr(self, name, None)
            if fn is None:
                continue
            try:
                await compat.resolve(fn(*args))
            except Exception as exc:
                self.log(f"iparking: {name}({detail}) failed: {exc}")
                return False
            self.log(f"iparking: {name}({detail}) ok")
            return True
        self.log(
            f"iparking: this Device exposes none of {'/'.join(names)}; {detail} skipped"
        )
        return False

    async def _notify(self, text: str) -> None:
        """Create a Homey notification.

        **Settled on hardware 2026-08-04, and since confirmed against the SDK's own source: the
        call is `create_notification(text)` — one plain string, positionally.** The manager
        wraps it as `{"excerpt": message}` itself.

        The dict shape `create_notification({"excerpt": text})` is tried **last**, and that
        ordering is the whole fix. It used to be tried second, it did **not raise**, it logged
        "ok", and it put a dict inside the notification's `excerpt` field — so every message
        this app posted rendered as a blank line in the timeline while the log claimed success.

        The lesson worth keeping: **a call that does not raise is not a call that worked.** A
        shape probe needs a check on the observable result, and where there is none, the shape
        has to be confirmed against the surface the user actually sees.

        `excerpt=` is still attempted first: it is harmless (a wrong keyword raises `TypeError`
        before anything is posted) and it would be the more explicit API if a future runtime
        offers it.

        Never fatal: a registration that succeeded must not be reported as failed because the
        notification could not be posted.

        **This is the only user-visible channel a tile press has.** The Python SDK exposes no
        toast, alert or transient-message call of any kind — see the note in
        `press_favorite`'s caller chain and the report for task #14 — so a press that succeeds
        says so here, in the timeline, and nowhere else.
        """
        manager = getattr(self.homey, "notifications", None)
        if manager is None:
            self.log("iparking: no notifications manager on this runtime; outcome logged only")
            return
        for name in ("create_notification", "createNotification"):
            fn = getattr(manager, name, None)
            if fn is None:
                continue
            # ORDER IS THE FIX. The plain string must be tried before the dict, because this
            # runtime accepts a positional argument of *any* type and stores it in `excerpt`
            # verbatim — so the dict shape "succeeds" and writes a dict into the field. The
            # dict stays last rather than being deleted: on a runtime that genuinely wants
            # `{"excerpt": …}` the positional call raises TypeError first, and dropping it
            # would silently lose every message there instead of only rendering it blank.
            for shape, call in (
                ("excerpt=", lambda f=fn: f(excerpt=text)),
                ("positional", lambda f=fn: f(text)),
                ("{'excerpt': …}", lambda f=fn: f({"excerpt": text})),
            ):
                try:
                    await compat.resolve(call())
                except TypeError:
                    continue  # wrong call shape; nothing was posted
                except Exception as exc:
                    self.log(f"iparking: notification failed ({exc})")
                    return
                self.log(f"iparking: notification posted via {name}({shape})")
                return
        self.log("iparking: notifications manager exposes no create_notification")


    # --- helpers ------------------------------------------------------------

    async def _set(self, capability: str, value) -> None:
        """Write a capability value, guarded by `get_capabilities()`.

        The guard is what lets this device tolerate a capability list edited in
        `driver.compose.json` without a re-pair: the real SDK raises on a capability the
        device does not have. It is also what makes the 주차장명 removal safe — a device that
        has already shed it simply never gets written to.

        `iparking_today_count` is the **only** capability anything writes. The tile buttons are
        `getable: false` and nothing touches them; see `_reconcile_buttons`.
        """
        if value is None or capability not in self.get_capabilities():
            return
        try:
            if self.get_capability_value(capability) != value:
                await self.set_capability_value(capability, value)
        except Exception as exc:
            self.log(f"iparking: set {capability} failed: {exc}")

    async def _safe_available(self) -> None:
        try:
            await self.set_available()
        except Exception:
            pass

    async def _safe_unavailable(self, reason: str) -> None:
        try:
            await self.set_unavailable(reason)
        except Exception:
            pass


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _render(exc: Exception, language: str) -> str:
    """An exception as a sentence in the viewer's language, falling back to its own message.

    Mirrors `api._fail`'s rendering: every error this app raises carries an i18n key rather
    than prose, so the Flow card can say the same thing the settings page says.
    """
    key = getattr(exc, "key", "")
    if key:
        rendered = i18n.translate(
            key,
            language,
            code=getattr(exc, "code", "") or "",
            days=getattr(exc, "max_days", dates.MAX_DAYS_AHEAD),
        )
        if rendered != key:
            return rendered
    return str(exc) or key
