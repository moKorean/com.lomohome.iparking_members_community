"""One paired parking lot: the 주차장명 sensor, and the Flow action's write path.

Two jobs, and they are deliberately asymmetric in weight.

**The sensor (requirement 4).** One capability, `iparking_park_name`, holding 주차장명. The
value is set from the device store at `on_init` — so a device shows its lot name immediately,
before any request and even with the account logged out — and then refreshed once an hour
from the server, because a building office can rename a lot. Shape mirrors
`navien_lib/airmonitor/device.py`, the closest analogue in the sibling app: read-only over
REST, restart-on-death with backoff, `set_unavailable` after two consecutive failures, and
`_set()` guarded by `get_capabilities()`.

**The Flow actions (item 7).** `flow_register` is the whole run listener body, shared by both
register cards — `register_visitor` (plate + optional date) and `register_visitor_today`
(plate only), which differ only in where `visit_date` comes from. It writes to a
real building's access-control system, so it delegates every consequential decision to
`client.register()` — zero retries, the hourly ceiling, the fresh-budget recovery re-query,
the status-filtered existence predicate — and adds exactly two things of its own:

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

**Poll cadence** is 3600 s ± 10 % with a 0–10 % start offset and **one request per tick** —
24 requests/day/device. See `const.POLL_INTERVAL_S`; there is no `poll_interval` setting in
v0.1.0.

**자주 오는 차량 — the tile buttons.** Ten device settings (5 이름 + 5 차량번호) and, for every
*complete and valid* pair, one button on the tile that registers that plate for **today in
KST**. Three mechanics carry the whole feature, and each is a constraint rather than a choice:

1. A tile button is a `boolean` capability with `uiComponent: "button"`, and Homey has **no
   dynamic-capability declaration** — so five are declared in `app.json` up front and each
   device adds or removes them itself (`add_capability` / `remove_capability`), which is what
   makes a lot with two favourites show exactly two buttons rather than five.
2. The label comes from `set_capability_options(..., {"title": …})` at runtime, because
   `엄마차` is typed after install and no static manifest can carry it.
3. A `button` reports `true` on press and **stays** true, so `press_favorite` resets it to
   `false` in a `finally` or the button can be pressed exactly once per app start.

**A pair counts only when both halves are present and the plate validates.** Half a pair or a
typo'd plate produces no button *and a log line saying which slot and why* — silence there
would leave a user staring at a button that never appeared with nothing to read.

**The one runtime settings write in this app is here, and it is deliberate.** When a plate
validates, `on_settings` writes the normalized form back (`12가 3456` → `12가3456`) so the
maintainer *sees* what was stored, the same visibility rule the settings page follows on blur.
That is a **settings** write, not a store write — see the paragraph below, which still holds.

**No runtime store writes** (criterion 18). Nothing here calls `set_store_value`: `stor_seq`,
`park_seq` and `lot_id` are fixed at pairing, and `park_name` lives on the capability, which
is the value the user actually looks at. The pattern for a store write exists in
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
    MAX_FAVORITES,
    MAX_POLL_FAILURES,
    POLL_BACKOFF_S,
    POLL_INTERVAL_S,
    POLL_JITTER,
    POLL_START_JITTER,
    STORE_LOT_ID,
    STORE_PARK_NAME,
    STORE_PARK_SEQ,
    STORE_STOR_SEQ,
    favorite_name_setting,
    favorite_plate_setting,
    quick_capability,
)
from iparking_lib.iparking import codes, dates
from iparking_lib.iparking.client import IparkingError, RegisterUncertain
from iparking_lib.iparking.plate import (
    InvalidPlateError,
    mask_plate,
    normalize_plate,
    strip_plate,
)

#: Shown while the lot cannot be read. Deliberately not "logged out" — the account may be
#: fine and the vendor's API simply unreachable, and the tile has no room to explain both.
_UNAVAILABLE = "주차장 정보를 가져올 수 없습니다"

#: Shown when the account no longer lists this lot at all. A different failure with a
#: different remedy (re-pair, or ask the building office), so it gets its own sentence.
_GONE = "이 주차장이 계정에서 보이지 않습니다. 관리사무소에 문의하거나 기기를 다시 추가하세요."

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

    Pure, and takes a plain dict, so the pairing rule is testable without the SDK.
    """
    values = settings if isinstance(settings, dict) else {}
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

    Node's SDK3 hands over one event object (`{oldSettings, newSettings, changedKeys}`), SDK2
    passed `(old, new, changed)` positionally, and every manager this app actually uses is
    snake_case with plain arguments — and **no Python stub ships with the CLI**, so there is
    nothing to read that settles which one the Python runtime does. All of them are accepted.

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
        self._park_name = str(store.get(STORE_PARK_NAME) or "")
        # The store rather than `get_data()`: both hold `lot_id` (the driver writes it to
        # each), and the store is the one object the driver and the device already share.
        self.log(
            f"iparking: visitcar init lot={self._lot_id or '?'} "
            f"park_seq={self._park_seq} stor_seq={self._stor_seq}"
        )

        # Answer the sensor from the store before any request. A lot's name is fixed at
        # pairing and does not need the network to be true, so a hub that boots offline shows
        # 주차장명 immediately instead of an empty tile that looks broken.
        await self._set(CAPABILITY_PARK_NAME, self._park_name)

        # Which quick capabilities already carry a press handler. Per device lifetime, because
        # binding a listener twice for one capability is not something the SDK's contract
        # promises to tolerate and `_reconcile_buttons` runs on every settings save.
        self._listening: set[str] = set()
        # Guards the settings write below against the recursion it could otherwise cause: this
        # is the one place in the app that writes settings *from inside* a settings callback.
        self._settings_busy = False
        # Reconciled at init as well as on every save, so a hub restart does not lose the
        # buttons — a paired device is re-created from scratch and `add_capability` is the only
        # thing that puts them back. Guarded: a runtime that exposes no capability-mutation
        # surface at all must still get its 주차장명 sensor and its poll loop.
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
        """Cancel the poll task, then **await** it, so nothing is still running once Homey
        considers the device gone."""
        self._closing = True
        task = getattr(self, "_poll_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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
        # The boot attempt is deliberately outside the two-cycle budget: it is already
        # excused by the store-seeded capability value, so counting it would make the first
        # real cycle the second strike.
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
        """**One** request: this store's lot list, read for this lot's current name.

        `parking_lots` rather than `enumerate_lots`: a device belongs to exactly one store, so
        asking for one store's lots is one request per tick regardless of how many stores the
        account holds.
        """
        try:
            rows = await self._api.parking_lots(self._stor_seq)
        except Exception:
            self._failures += 1
            await self._update_availability()
            raise
        name = self._name_from(rows)
        if name is None:
            # Not a transport failure, but not a healthy device either: the account can no
            # longer see this lot. Counted the same way, because the user-visible verdict is
            # the same one — this tile is not telling you the truth any more.
            self._failures += 1
            self.log(f"iparking: lot {self._lot_id or self._park_seq} not in the account's list")
            await self._update_availability(_GONE)
            return
        # Reset on an explicit success, never inferred from the absence of an exception.
        self._failures = 0
        if name != self._park_name:
            self.log(f"iparking: lot renamed {self._park_name!r} -> {name!r}")
            self._park_name = name
        await self._set(CAPABILITY_PARK_NAME, name)
        await self._update_availability()

    def _name_from(self, rows) -> str | None:
        """This lot's 주차장명 from a `/parkinglot/list` response, or `None` if it is absent.

        `lot_id` is preferred because it is what `data.id` was keyed on; `park_seq` is a
        fallback for a deployment that stops sending `lot_id`, and it is only consulted when
        the id match failed — matching on the weaker key first could pick a different store's
        lot that happens to share a `park_seq`, which is the very uncertainty that kept
        `park_seq` out of `data.id`.
        """
        for key in ("lot", "seq"):
            for row in rows if isinstance(rows, list) else ():
                if not isinstance(row, dict):
                    continue
                lot_id = str(row.get("lot_id") or row.get("lotId") or "")
                park_seq = _int(row.get("park_seq") or row.get("parkSeq"))
                hit = (lot_id and lot_id == self._lot_id) if key == "lot" else (
                    park_seq and park_seq == self._park_seq
                )
                if hit:
                    return str(row.get("park_name") or row.get("parkName") or "") or None
        return None

    async def _update_availability(self, reason: str = _UNAVAILABLE) -> None:
        """`set_unavailable` after **two** consecutive failures, `set_available` on recovery.

        Two rather than one: a single dropped request against a cloud API addressed over plain
        HTTP is ordinary, two in a row is a pattern. And unavailability has to be *said* here
        rather than inferred from the tile, because `_poll_once` keeps the last name it read —
        so a lot that stopped answering would otherwise look exactly like a healthy one.
        """
        if self._failures >= MAX_POLL_FAILURES:
            await self._safe_unavailable(reason)
        else:
            await self._safe_available()

    # --- the Flow action ----------------------------------------------------

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
        return True

    # --- 자주 오는 차량: the tile buttons -------------------------------------

    async def on_settings(self, *args, **kwargs) -> None:
        """The favourites were edited: normalize the plates, then reconcile the buttons.

        `*args, **kwargs` rather than a declared signature, and that is the point rather than
        laziness: the SDK's Python call shape for this hook is not readable anywhere (see
        `_new_settings`), and a mismatched signature here would present as settings that save
        but never produce a button — with a `TypeError` buried in a hub log nobody is watching.

        Order matters. The write-back happens **first**, and the reconcile then runs against
        the normalized dict, so a plate typed as `12가 3456` produces a button on the same save
        rather than the next one.
        """
        if self._settings_busy:
            # Reached only if a runtime re-enters this hook for our own `set_settings` below.
            # It would converge anyway — a normalized plate normalizes to itself, so the second
            # pass writes nothing — but a settings write from inside a settings callback is
            # exactly the shape that loops, and relying on convergence to stop a loop is not a
            # guarantee.
            self.log("iparking: on_settings re-entered by our own write; ignored")
            return
        self._settings_busy = True
        try:
            values = _new_settings(args, kwargs)
            if values is None:
                self.log("iparking: on_settings gave no readable settings; re-reading them")
                values = await self._device_settings()
            values = await self._normalize_plates(values)
            await self._reconcile_buttons(values)
        finally:
            self._settings_busy = False

    async def _normalize_plates(self, values: dict) -> dict:
        """Write every accepted plate back in its normalized form, and return the fixed dict.

        This is **the one runtime settings write in the app**, and it is deliberate: the
        maintainer's own example is `12가 3456`, with a space, and a user who saves that and
        sees it unchanged has no way to know whether the space was a problem. Writing
        `12가3456` back is the same visibility rule the settings page applies on blur.

        An **invalid** plate is left exactly as typed. Stripping it would half-fix a value that
        is still going to be rejected, and moving the user's text under them while telling them
        nothing is worse than leaving the typo where they can see it.

        Only the keys are logged, never the values — a favourite's plate is still a plate.
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
        if not writes:
            return values
        await self._sdk_call(
            ("set_settings", "setSettings"), writes, what=f"normalized {sorted(writes)}"
        )
        fixed = dict(values)
        fixed.update(writes)
        return fixed

    async def _reconcile_buttons(self, settings: dict | None = None) -> None:
        """Make the tile show exactly one button per complete, valid favourite.

        Runs at `on_init` and on every settings save, and it is written as a full reconcile
        rather than as a diff of the change: `changedKeys` is one of the arguments whose shape
        is not established, and a restart has no changed keys at all. Idempotent by
        construction — every slot is either wanted or not, and both branches are safe to repeat.
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
            # Clears a `true` left latched by a press that the app restarted through.
            await self._set(capability, False)

    async def _label_button(self, capability: str, name: str, language: str) -> None:
        """Put `[엄마차 방문 등록]` on the button.

        `set_capability_options` with a `title` is the only route user input has to a tile
        label — the manifest title is static and `엄마차` is typed after install.

        The title is sent as a **plain string**, which is the form Homey's own documentation
        shows. An `{"ko": …, "en": …}` object is also legal in a manifest, but both values would
        be the same user-typed name here, so it buys nothing and would render as
        `[object Object]` on a runtime that expects a string. **Which one the Python runtime
        wants cannot be settled off-device**: neither raises, and the difference is visible only
        on the tile itself. The *spelling* that answered is logged, which is the half that one
        real look at the device page does settle.

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
                # variable would give all five buttons whichever slot was reconciled last.
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
        """
        capability = quick_capability(index)
        try:
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
        finally:
            # A `button` capability reports `true` on press and **keeps** it. Without this the
            # tile is a control that works exactly once, and the second press — the one that
            # matters, because the first is when the user learns the button exists — sets the
            # value to `true` again with no change for a listener to fire on. In `finally` so a
            # refused or uncertain write leaves a pressable button behind too.
            await self._set(capability, False)

    async def _device_settings(self) -> dict:
        """This device's settings as a plain dict. `{}` only when they are truly unreadable.

        Coerces with `dict(values)` rather than testing `isinstance(values, dict)`. That
        distinction cost a working feature once: the runtime's `get_settings()` returns a
        mapping that is **not** a `dict` subclass, so an `isinstance` guard discarded a perfectly
        good answer, fell through the loop, and logged "this Device exposes no get_settings" —
        while `dir(self)` showed the bound method right there. Every button silently vanished on
        restart and the log actively pointed away from the cause.

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

        Four unverifiable spellings ride on this — `add_capability`, `remove_capability`,
        `set_capability_options`, `set_settings` — because **no Python stub ships with the
        CLI**, so snake_case versus camelCase cannot be checked off-device for any of them.
        Same tactic as `_notify` and `compat.flow_card`: try both, log the winner, and one real
        press settles all four at once.

        Never fatal. A device that cannot grow a button is still a working 주차장명 sensor, and
        that is the requirement the maintainer stated first.

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

        **Settled on hardware 2026-08-04: the call is `create_notification(text)` — one plain
        string, positionally.** Verified against the hub's own timeline, not by the call
        returning without raising.

        The dict shape `create_notification({"excerpt": text})` is tried **last**, and that
        ordering is the whole fix. It used to be tried second, it did **not raise**, it logged
        "ok", and it put a dict inside the notification's `excerpt` field — so every message
        this app posted rendered as a blank line in the timeline while the log claimed success.
        Homey's own managers post `excerpt` as a plain string; comparing our rows against
        theirs is what exposed it.

        The lesson worth keeping: **a call that does not raise is not a call that worked.** A
        shape probe needs a check on the observable result, and where there is none, the shape
        has to be confirmed against the surface the user actually sees.

        `excerpt=` is still attempted first: it is harmless (a wrong keyword raises `TypeError`
        before anything is posted) and it would be the more explicit API if a future runtime
        offers it.

        Never fatal: a registration that succeeded must not be reported as failed because the
        notification could not be posted.
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
        device does not have.
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
