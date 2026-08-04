"""One paired parking lot: the 주차장명 sensor, and the Flow action's write path.

Two jobs, and they are deliberately asymmetric in weight.

**The sensor (requirement 4).** One capability, `iparking_park_name`, holding 주차장명. The
value is set from the device store at `on_init` — so a device shows its lot name immediately,
before any request and even with the account logged out — and then refreshed once an hour
from the server, because a building office can rename a lot. Shape mirrors
`navien_lib/airmonitor/device.py`, the closest analogue in the sibling app: read-only over
REST, restart-on-death with backoff, `set_unavailable` after two consecutive failures, and
`_set()` guarded by `get_capabilities()`.

**The Flow action (item 7).** `flow_register` is the whole run listener body. It writes to a
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

**No runtime store writes** (criterion 18). Nothing here calls `set_store_value`: `stor_seq`,
`park_seq` and `lot_id` are fixed at pairing, and `park_name` lives on the capability, which
is the value the user actually looks at. The pattern for a store write exists in
`com.lomohome.localthings` if a later version needs one.
"""

import asyncio
import random

from homey import device

from iparking_lib import compat, i18n
from iparking_lib.const import (
    CAPABILITY_PARK_NAME,
    MAX_POLL_FAILURES,
    POLL_BACKOFF_S,
    POLL_INTERVAL_S,
    POLL_JITTER,
    POLL_START_JITTER,
    STORE_LOT_ID,
    STORE_PARK_NAME,
    STORE_PARK_SEQ,
    STORE_STOR_SEQ,
)
from iparking_lib.iparking import codes, dates
from iparking_lib.iparking.client import IparkingError, RegisterUncertain
from iparking_lib.iparking.plate import mask_plate, strip_plate

#: Shown while the lot cannot be read. Deliberately not "logged out" — the account may be
#: fine and the vendor's API simply unreachable, and the tile has no room to explain both.
_UNAVAILABLE = "주차장 정보를 가져올 수 없습니다"

#: Shown when the account no longer lists this lot at all. A different failure with a
#: different remedy (re-pair, or ask the building office), so it gets its own sentence.
_GONE = "이 주차장이 계정에서 보이지 않습니다. 관리사무소에 문의하거나 기기를 다시 추가하세요."


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

    async def flow_register(self, car_number: str, visit_date: str = "") -> bool:
        """`register_visitor` — the app's one Flow action. **This writes to a building.**

        Two modes, and they are one argument apart, exactly as the maintainer asked for: a
        plate alone registers for today; a plate plus a date registers for that date. That is
        `visit_date`'s `required: false` in the card definition doing the work — the empty case
        falls through to `dates.today_kst()` via `client.register`, so both modes share one
        parse path and one window check.

        Returns `True` for a registration *and* for a duplicate. Raises for everything else,
        including `RegisterUncertain`, which is raised only after its own notification.
        """
        raw = "" if visit_date is None else str(visit_date).strip()
        # Logged verbatim, and at this level on purpose: `log` is the only channel this
        # runtime gives an app, and §3.6's open question — whether Homey hands a `date` arg
        # over as `dd-mm-yyyy` or `mm-dd-yyyy` — is answerable from one ordinary Flow run if
        # and only if this line exists. A calendar date is not personal data; the plate on the
        # next line is, which is why that one is masked and this one is not.
        self.log(
            f"iparking: flow register {mask_plate(car_number)} "
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
            self.log("iparking: flow register outcome UNCERTAIN; no retry")
            await self._notify(str(exc))
            raise
        except (IparkingError, ValueError) as exc:
            # `ValueError` covers `InvalidPlateError` and `DateError` — user-input verdicts
            # that carry their own i18n keys just like the API errors do.
            raise Exception(_render(exc, language)) from exc

        outcome = str(result)
        plate = strip_plate(car_number)
        human = dates.format_kst_human(result.api_date, language)
        key = ("flow_registered" if outcome == codes.OUTCOME_OK
               else "flow_already_registered" if outcome == codes.OUTCOME_ALREADY_REGISTERED
               else "")
        if not key:
            # An explicit `FAIL` for this plate: a verdict from the server, so it is reported
            # as one. (`register` never returns anything else — an unresolved outcome is
            # `RegisterUncertain`, handled above.)
            raise Exception(i18n.translate(codes.OUTCOME_FAILED, language))

        # The plate is shown in full here, unmasked, and that is not an oversight either: the
        # masking rule protects *log lines*, which get pasted into issues. A notification is
        # this Flow's answer to the person who wrote the Flow, and "12가****" would make it
        # useless. `flow_registered` is the only place in the app that renders a full plate
        # outside the settings page.
        text = i18n.translate(key, language, plate=plate, date=human)
        if result.ambiguous:
            # A 2-2-4 date whose first two fields are both ≤ 12 was resolved day-first by
            # policy, not by evidence. Saying so is the mitigation; hiding it is how a
            # visitor ends up at a gate on the wrong day.
            text = f"{text}\n{i18n.translate('date_ambiguous', language, date=human)}"
        await self._notify(text)
        self.log(f"iparking: flow register {mask_plate(plate)} -> {outcome} on {result.api_date}")
        return True

    async def _notify(self, text: str) -> None:
        """Create a Homey notification, tolerating whatever this build calls it.

        The Python SDK's notification surface is not pinned anywhere we can read — the Node
        API is `homey.notifications.createNotification({excerpt})` and the Python bindings for
        the managers this app does use are snake_case with plain arguments — so both spellings
        and all three plausible call shapes are tried, in the same spirit as
        `compat.flow_card`. The shape that worked is logged, so one real Flow run settles it.

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
            for shape, call in (
                ("excerpt=", lambda f=fn: f(excerpt=text)),
                ("{'excerpt': …}", lambda f=fn: f({"excerpt": text})),
                ("positional", lambda f=fn: f(text)),
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
