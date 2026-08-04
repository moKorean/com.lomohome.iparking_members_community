"""Pairing for the `visitcar` driver, and the app's one Flow card registration.

**One device per parking lot.** Pairing calls `enumerate_lots()`, which iterates every
`invitation_authorization_list` entry × every lot, so a multi-store account generalizes with
no special-casing and there is no "which store?" question for a pair view to ask. That is also
why `build_devices` takes only the session: unlike the sibling app there is no `home_seq`
counterpart to resolve first.

**`data.id` is `lot_id`, not `park_seq`.** `data` is immutable after pairing, so getting the
key wrong is not a bug you fix in the next version — it is a bug every user pays for by
re-pairing every device. `lot_id` (`"1160009001"`) is the vendor's globally-qualified
identifier; bare `park_seq` uniqueness across stores was never established. `stor_seq` goes in
the mutable store, where a later version can correct it.

**An entry that may not register still pairs.** `invitation_register_authorization_yn != "Y"`
gates the *write*, checked live inside `client.register()` on every attempt, because the
building office can grant the permission later. The 주차장명 sensor is useful either way, so
refusing to pair such a store would remove a working sensor to prevent a write that is already
prevented.

The Flow card is registered here rather than in `app.py` for the same reason navien does it:
cards are app-global but every one of them carries a `device` arg filtered to this driver, so
the driver is the object that knows they exist. Registration is guarded — a card that fails to
bind must not take driver init down with it, because that would cost the sensor too.
"""

from homey import driver

from iparking_lib import compat, pairing
from iparking_lib.const import (
    FLOW_REGISTER_VISITOR,
    STORE_LOT_ID,
    STORE_PARK_NAME,
    STORE_PARK_SEQ,
    STORE_STOR_SEQ,
)

#: Shown in the pair view when the account is fine but has no lots behind it. Distinct from
#: the empty-authorization-list refusal `enumerate_lots` raises (`no_stores`), because the
#: remedy differs: there the account is enrolled at no building at all.
_NO_LOTS = (
    "이 계정에서 주차장을 찾지 못했습니다. "
    "아이파킹 MEMBERS 웹사이트에서 단지 정보를 확인하세요."
)


class VisitCarDriver(driver.Driver):

    async def on_init(self) -> None:
        self.log("iParking visitcar driver init")
        self._register_flow_cards()

    def _register_flow_cards(self) -> None:
        """Bind `register_visitor` to the targeted device's own `flow_register`.

        The run listener takes `**kwargs` because Homey passes extras such as `manual`
        alongside `(args, state)`; a handler without it errors the card out at run time, which
        is the kind of failure that only shows up when a user's Flow fires.
        """
        try:
            card = compat.flow_card(self.homey, "action", FLOW_REGISTER_VISITOR)
            compat.register_run_listener(card, self._on_register)
        except Exception as exc:
            self.log(f"iparking: flow card registration failed: {exc}")

    async def _on_register(self, args=None, state=None, **kwargs) -> bool:
        """`register_visitor`'s run listener. **This writes to a building.**

        Deliberately thin: it resolves the targeted device and hands over. Everything that
        matters — the two date modes, the notification echoing the resolved date, the
        duplicate that must not read as a failure — lives in `VisitCarDevice_.flow_register`,
        next to the store fields it needs.
        """
        values = args if isinstance(args, dict) else {}
        target = values.get("device")
        if target is None:
            raise Exception("이 Flow 카드에 등록할 주차장 기기를 선택하세요.")
        return await target.flow_register(
            car_number=str(values.get("car_number") or ""),
            # `""` rather than `None` when the arg was left empty, because that is what
            # `required: false` delivers and it is the whole two-mode behaviour: empty means
            # today in KST, resolved through the same parse path a supplied date takes.
            visit_date=str(values.get("visit_date") or ""),
        )

    async def on_pair(self, session) -> None:
        pairing.install(self, session, self._build_devices)

    async def on_repair(self, session, device=None) -> None:
        pairing.install_repair(self, session)

    async def _build_devices(self, api) -> list:
        """Every lot on the account, as one Homey device each.

        `enumerate_lots` refuses an account with an empty `invitation_authorization_list`
        outright, carrying its own message — that error is allowed to reach the pair view
        rather than being flattened into "no devices found", because "no building is enrolled
        on this account" is something only the building office can fix and the user needs to
        be told which of the two it is.
        """
        lots = await api.enumerate_lots()
        if not lots:
            raise Exception(_NO_LOTS)
        for lot in lots:
            self.log(
                f"iparking: pair lot id={lot.lot_id} park_seq={lot.park_seq} "
                f"stor_seq={lot.stor_seq} can_register={lot.can_register}"
            )
        return [
            {
                "name": lot.park_name,
                # `lot_id`, and it is immutable from here on. See the module docstring.
                "data": {"id": lot.lot_id},
                "store": {
                    STORE_STOR_SEQ: lot.stor_seq,
                    STORE_PARK_SEQ: lot.park_seq,
                    STORE_LOT_ID: lot.lot_id,
                    STORE_PARK_NAME: lot.park_name,
                },
            }
            for lot in lots
        ]
