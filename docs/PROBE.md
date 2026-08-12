# Live probe of the two write endpoints — 2026-08-04

`POST /invitations` and `DELETE /invitations/{invt_seq}` are the only endpoints that could not
be characterised read-only, because exercising them registers and cancels a vehicle on a real
residential building's access-control system. They were exercised **once**, with the
maintainer's explicit approval, under the safeguards below.

Everything else in [`RECON.md`](./RECON.md) was established without a write.

## Safeguards used

| Safeguard | Why |
|---|---|
| Plate **`임0000`** | Matches the vendor regex (`^임(?:\d{4}\|\d{6})$`) as a temporary-permit plate. Obviously synthetic, and vanishingly unlikely to be a real vehicle at this building — unlike a `12가1234`-shaped plate, which could be somebody's actual car. |
| Visit date **+85 days** | A reservation that far ahead cannot open a gate this week even if cleanup failed completely. |
| `invt_seq` persisted **before** cleanup was attempted | A crash mid-run leaves a manual handle rather than an orphan nobody can find. |
| `DELETE` proven **before** any second write | The original design used `try/finally` cleanup that depended on `DELETE` — itself unexercised. That is a circular safety net. |
| Real web UI open in a browser throughout | Cleanup was visually confirmable and manually recoverable. |
| Before-state recorded | The target window held **0** rows and no `임0000`, so any row found afterwards was ours. |

## Final state — verified

**0 active rows.** Two `CANCEL` rows remain for `임0000` at the target date. That residue is
unavoidable: `DELETE` does not remove a row (see finding 3). A cancelled reservation cannot open
a gate.

## Findings

### 1. `POST /invitations` returns no per-car results at all

A successful registration is exactly:

```json
{"result":"0000","resultMessage":"성공","totalCnt":0,"current_page":0,
 "records":0,"encryption":false,"pageCheck":false,"resultData":null}
```

`resultData` is **`null`**. There is no `invitationInfoList`, and no `SUCCESS` / `FAIL` /
`EXIST` array in any case that could be produced — success, duplicate, or re-register after
cancel. `RECON.md` records those three words as codes "the UI knows" but never says where they
appear in a response; the answer is that they do not appear.

**This inverted the client contract.** The pre-probe rule was *"a plate absent from
`parse_per_car()`'s mapping means the response did not say → `RegisterUncertain`"*, which would
have made **every normal registration report as uncertain**. The rule is now: the **top-level
`result` is the authority** — `0000` with no per-car data means every requested plate
succeeded. `parse_per_car()` is a fallback that only wins when it finds an explicit row.

### 2. A duplicate registration returns top-level `10003`

Registering a plate that already has an **active** row for that date:

```json
{"result":"10003","resultMessage":"방문차량 등록이 실패하였습니다. 다시 시도해주세요.",
 "resultData":null}
```

So `10003` (`registeredCar`) is the only `EXIST` signal in practice, and mapping it to a
distinct `already_registered` outcome — neither success nor generic failure — was right.

**The vendor's own message must never be surfaced.** It says *"다시 시도해주세요"* — invites
exactly the retry that turns one uncertain write into two real registrations. There is a test
asserting that string reaches neither the outcome nor the log.

### 3. `DELETE` does not remove the row — it flips the status to `CANCEL`

After `DELETE /invitations/{invt_seq}` returning `0000`, the row is **still listed**, with the
same `invt_seq`, at `inot_status: "CANCEL"`. `GET /invitations/{invt_seq}` also still resolves
and reports `CANCEL`.

And a `CANCEL` row **does not block re-registration**: registering the same plate on the same
date succeeds and mints a **new** `invt_seq`. So for one plate on one date the history can hold:

```
  3455386  임0000  CANCEL
  3455393  임0000  RESERVE
```

**`CANCEL` and active rows genuinely coexist.** This is why the register path's existence
predicate must be **existential over all matching rows** —
`any(row.status in {RESERVE, IN, OUT} for row in matching)` — and never "find the row, then
check its status":

- counting `CANCEL` as existence reports an **unregistered** car as registered, and the visitor
  meets a barrier that will not open;
- a single-row lookup landing on the `CANCEL` row reports a **succeeded** write as failed.

Both directions were live hazards, not hypotheticals. Two consequences for callers: 취소 must
be confirmed by the status flipping to `CANCEL`, **not** by the row disappearing (a re-read
looking for absence would report a working 취소 as broken); and the register recovery re-query
must filter on active statuses only.

### 4. Read-only riders, settled in the same session

- **A non-empty `carNumber` does filter server-side** — the same query returned 43 rows
  unfiltered and 19 rows for a single plate, with exactly one distinct plate present. The
  recovery re-query is therefore cheap. It remains an **optimisation only**: the client filters
  plate and date itself, because the server filter's matching rule was never characterised and a
  quirk there would read as "not registered".
- **A far-future `endDate` is accepted** (`20261231` returned `result: 0000`).
- `page_size` is honoured verbatim, and `resultData.total` is `[]` even on a range returning 43
  records — both already in `RECON.md`.

## Still unverified

- `PUT /invitations` (수정) — never exercised. Needs its own probe before anything depends on it.
- `POST /invitations/{invt_seq}/sms` — never exercised.
- Whether the **write** endpoint enforces the same 최근 3개월 window as the history endpoint.
  The +80-day cap on visit dates is a deliberately conservative guess, not a measurement.
- Whether `resultData.total` ever populates for any account or range.
