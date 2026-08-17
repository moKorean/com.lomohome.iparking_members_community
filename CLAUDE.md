# CLAUDE.md — com.lomohome.iparking_members_community

Homey (SDK3, Python runtime) app for **iParking MEMBERS 방문 차량 관리** (visitor vehicle
registration at a Korean apartment complex), modeled architecturally on the sibling app
`com.lomohome.navien` (shared app-level session, pure `homey`-free client library,
`.homeycompose/` composition, credentials in app settings). Unlike navien, this app is a
**clean-room client** — it is not a port of anyone else's code. Its protocol knowledge comes
from reverse-engineering iParking MEMBERS' own web bundles, recorded verbatim in
[`docs/RECON.md`](./docs/RECON.md).

## Working with the maintainer's input

When working externally the maintainer sometimes has **no Hangul IME**, so they type Korean
in **dubeolsik (2-set) layout with the keyboard in English mode**. Messages then look
like random Latin letters (e.g. `gksrmf` = 한글, `aksemfwk` = 만들자, `gownj` = 해줘) but
are real Korean.

- **Decode such input as Korean and reply in Korean.**
- A converter lives one level up, in the shared parent folder:
  `../qwerty_to_hangul.py` — `python3 ../qwerty_to_hangul.py "gksrmf aksemfwk"` →
  `한글 만들자` (or pipe text in on stdin). Note it transliterates *every* mapped letter, so
  literal English words/filenames mixed into a sentence get transliterated too.

## Distribution — App Store bound (reversed 2026-08-12)

**This app is being prepared for the Homey App Store.** It was self-install only until
2026-08-12; see [`docs/DISTRIBUTION.md`](./docs/DISTRIBUTION.md) for what changed and what
did not. In short: the "audience is one apartment unit" argument was wrong — the requirement
is *an* iParking MEMBERS account, not *this* one — while the other two original objections
(an unofficial private API with the vendor's key as a source literal; writes to a real
building's access control) still stand and are being carried into review rather than
argued away.

Three consequences for anything you edit here:

- **`README.txt` / `README.ko.txt` are store copy and are load-bearing again.** They are
  written for a stranger, not the maintainer, and must contain **no occurrence of "Homey"** —
  the App Store rejects the word in store-facing text (app name, description, changelog, these
  two files). `README.md` / `README.en.md` are GitHub docs and may use it freely.
- **The READMEs open with public-release cautions, not a personal-use notice.** A stranger has
  to learn *before installing* that this moves a real gate, that it is unofficial and can break
  without notice, and what travels in cleartext. Do not soften those into marketing.
- **`homey app publish` is outward-facing** — it starts a review — so it runs only on the
  maintainer's explicit instruction, never as the tail of an ordinary change.

## Layout

The Python package is `iparking_lib` (**not** `lib`).

- `iparking_lib/iparking/` — pure client, no `homey` import (fully unit-testable):
  `aes.py` (vendored AES-256-CBC + PKCS#7 — the vendor's cipher, reimplemented rather than
  taken as a dependency; see `docs/RECON.md` for why), `crypto.py` (key/zero-IV/double-base64
  request envelope), `tls.py` (certifi-backed TLS context — the runtime has no system CA
  store, copied in spirit from `navien_lib/navien/tls.py`), `transport.py` (one urllib
  opener with a `StrictRedirectHandler` that refuses https→http downgrades and refuses any
  3xx on a body-carrying request), `plate.py` (NFC-normalize → strip whitespace incl.
  zero-width chars → validate against the site's own plate regex), `dates.py` (KST is the
  **sole** date authority — the browser's time zone is never consulted), `codes.py` (every
  vendor result code → an `error.*` key in `locales/{ko,en}.json`, plus shape-tolerant
  per-car outcome parsing for the register endpoint), `client.py` (`IparkingApi`).
- `iparking_lib/visitcar/{driver,device}.py` — the one Homey driver + device (imports
  `homey`; on-device only).
- `iparking_lib/` — `compat.py`, `i18n.py` (reads `locales/*.json` directly — Homey's own
  `homey.i18n` resolves to the app language, not the settings-page viewer's, so Python-side
  messages read the JSON files themselves; see the equivalent note in
  `navien_lib/i18n.py`), `const.py`, `selfcheck.py`, `pairing.py`.
- `app.py` — `IparkingApp` (homey_export). Owns the app-level shared session: `shared_api()`
  returns one stable `IparkingApi`, `reauth()` / `logout()` for repair and settings-page
  sign-out.
- `api.py` — settings-page API handlers (`_body()`/`_query()`/`_mask()`). Never persists
  the `access_token` — it is memory-only and crosses the wire in cleartext (see
  disclosures below), so keeping it out of `homey.settings` keeps it out of hub backups.
- `.homeycompose/` — manifest head, `capabilities/iparking_today_count.json` (오늘 등록된
  차량 수, the one capability every device carries), ten
  `capabilities/iparking_quick_{1..10}.json` tile buttons (declared up front because Homey has
  no dynamic-capability declaration; each device adds and removes them at runtime, so
  `MAX_FAVORITES` and these ten files have to change together — a test compares the count both
  ways), `flow/actions/register_visitor.json`. **Never hand-edit root `app.json`** (generated).
  The tile buttons are `getable: false` + `uiQuickAction: true`, exactly like Homey's own
  `button`: that is what makes them momentary push buttons instead of latching switches, and it
  is why nothing in the app writes a value to them.
  `capabilities/iparking_park_name.json` was **deleted** in v0.1.4 — 주차장명 was constant and
  duplicated the device's own name; `const.CAPABILITY_PARK_NAME` survives only so
  `device._shed_park_name` can take it off already-paired devices without a re-pair.
- `settings/{index.html,form.js}` — the app's **primary UI** (Homey has no free-text tile
  control). `form.js` is a plain module, not tied to the settings page's DOM, so the
  dashboard widget planned for v0.1.1 can mount the same module in ~30 lines instead of a
  rewrite.
- `drivers/visitcar/` — driver shim, pairing views, assets. One device **per parking lot**.
  `driver.compose.json` carries the twenty 자주 오는 차량 settings (10 이름 + 10 차량번호) in a
  `favorites` group; the driver's static `capabilities` list holds exactly
  `iparking_today_count`, because a freshly paired device must start with **zero** buttons.

## The device's five sensors, one request, and the poll

`iparking_today_count` (오늘 방문 예정), `iparking_tomorrow_count`, `iparking_week_count`
(rolling `WEEK_DAYS` from today), `iparking_parked_now` (현재 주차 중, `IN` only) and
`iparking_next_visit` (a short string, `—` when nothing is booked) are **all derived from one
등록 내역 read** in `device._apply_values`. Adding the last four cost no extra traffic — only a
wider window. Polled at 600 s ± 10 % (0–10 % start offset, **one request per tick**,
144/day/device since v0.3.1, up from hourly; `const.POLL_INTERVAL_S` carries the trade). Two consecutive failures mark the device unavailable
(`MAX_POLL_FAILURES`), because the capabilities keep the last values they read and would
otherwise look exactly like a lot with no visitors today.

Four rules, and each is a defect avoided rather than a preference:

- **`CANCEL` rows are not counted.** 취소 flips `inot_status` and leaves the row in the list, so a
  day's rows are frequently mostly cancellations — counting them read 6 on the maintainer's own
  account where the honest answer was 1. One rule, one home: `client.count_registered_between`,
  over `const.ACTIVE_STATUSES`.
- **현재 주차 중 excludes `OUT` even though `OUT` is an active status.** A departed car is still a
  valid uncancelled registration — that is what `ACTIVE_STATUSES` means — and is exactly what
  must not be counted as present.
- **The window is recomputed every tick** from `dates.today_api()`, never cached. A cached window
  survives KST midnight and holds yesterday's count all day, looking perfectly fine.
- **This app's own actions update it at zero extra requests.** `api.get_history` feeds its rows to
  the matching device (`device.note_history`), which covers the settings page's register *and*
  cancel because `form.js` re-reads the table after both; a Flow/tile register calls
  `refresh_counts`. The poll is then only there to catch registrations made on the vendor's
  website and the midnight rollover.

**No arrival trigger, and that was a decision.** `iparking_parked_now` is a sensor only. A
`visitor_arrived` device trigger was implemented, tested and then deleted: iParking has no
webhook and no push, so an arrival is only visible as `RESERVE → IN` on a poll — up to ten
minutes late since v0.3.1, and invisible entirely for a visit shorter than one interval.
Making it *prompt* means polling harder still, and the ceiling to weigh that against is the
vendor's willingness to keep serving this client, not the hub's capacity. A Flow card would have advertised
a promptness the data cannot support. **Do not reintroduce it without a push channel** — the
sensor plus the documented delay is the honest version.

**What not to "restore":** the poll used to refresh 주차장명, a constant that was also the device's
own name. Polling a constant was waste; polling a count is what makes it true. That distinction is
the whole justification for the traffic — do not reattach the loop to a value that cannot change.

## The 등록 내역 window reaches forward, and that is the point

`client.history()` defaults to `HISTORY_DAYS_BACK` (90) **behind** and `HISTORY_DAYS_AHEAD`
(90) **ahead** of KST today. An `endDate` of today reads like "the whole history" and is not:
it hides every visit that has not happened yet, which is most of what the table exists to
show — a registration made for next week was simply absent, reported from the maintainer's own
hub. A future `endDate` is accepted by the server (recon queried seven days ahead and got a
normal answer), and 90 ≥ `dates.MAX_DAYS_AHEAD` (80) so the read window always covers every
date this app is able to write.

Two consequences worth keeping:

- **`history()` pages.** The window is twice the range that measured 43 rows, and the vendor
  answers **oldest first**, so a read truncated at `page_size` drops the *newest* rows — the
  upcoming visits. The loop follows `totalCnt`, stops on a page that adds nothing new (a
  server ignoring `current_page` would otherwise repeat forever) and is bounded by
  `HISTORY_MAX_PAGES`.
- **Explicit bounds still win.** The device poll and the register path's recovery both pass
  `start_date == end_date`; the wide default must never leak into either, or the 오늘 등록 count
  silently starts counting six months.

## Disclosures — not boilerplate, keep them precise

These describe a design the review deliberately chose, and they appear in both READMEs and
at the top of the settings page. Keep the asymmetry precise if you ever edit them — do not
flatten it into "everything is insecure" or drop it into "everything is fine":

- Unofficial client for a private, undocumented API.
- **`members.iparking.co.kr` 301-redirects every HTTPS request to plain HTTP** — the access
  token and plate/visit data travel in cleartext as a result. This is the vendor server's
  choice; it cannot be fixed client-side.
- **The password does travel over verified TLS**, to `oauth.parkingcloud.co.kr` — say this
  explicitly whenever the transport disclosure is stated, so it reads as precise rather than
  as blanket alarmism.
- The request "encryption" is vendor obfuscation with a fixed key shipped to every browser,
  **not** confidentiality.
- Credentials are stored on the user's own hub, never sent anywhere but the vendor's own
  servers.
- **Registering a visitor vehicle acts on a real building's access-control system.**

## Sample plates — one synthetic family, enforced

Every example plate is `12가1234`, or `123가1234` where a three-digit prefix is the point.
Tests that must tell two vehicles apart vary **only the trailing digits** — `12가1235`,
`12가1236`, … — so the family reads as generated rather than observed.

The reason is not tidiness: **any string shaped `NN가NNNN` may be a real car.** An
invented-looking plate outside the family carries no mark that it is invented, and this repo
is public. `tests/test_sample_plates.py` scans the
working tree (tracked + untracked-not-ignored) and fails with file and line on anything
outside the family; it exempts `NNN동NNNN호`, which is a 세대 address, and the deliberately
invalid fixtures (`1234가1234`, `12가45678`) via lookarounds.

`docs/RECON.md` Appendix A is the trap here. Its plaintexts are prose and its envelopes are
base64, so a rename touches one and not the other — which happened, and the appendix test
only checked the ciphertext, so it stayed green while the document claimed one body encrypts
to another body's envelope. That test now asserts both halves. **To change a sample plate in
the appendix, edit `scripts/gen_aes_fixtures.sh` and re-run it** — never the document alone.

## Artwork — three drawings, three jobs, all generated from SVG

App Store review (2026-08-13, approved with feedback) named all three. Keep them distinct:

| File | Job | Review's note |
|---|---|---|
| `assets/icon.svg` | identifies the **app** | was identical to the driver icon — now a **vector trace of iParking's logo**, coordinates measured off `docs/icon.png` (which stays as the reference, not as a build input) |
| `drivers/visitcar/assets/icon.svg` | identifies **one paired lot** on its tile | unchanged: barrier-and-car line art |
| `docs/app-image.svg` + `docs/app-image.jpg` → `assets/images/*.png` | the **store page** picture | "an illustration"; now a **photograph** of a real iParking installation at an apartment complex, supplied by the maintainer. The SVG is only a viewBox crop frame over the JPEG — edit the four numbers to re-frame |
| `docs/device-image-visitcar.svg` + `docs/app_intro_img03.png` → `drivers/visitcar/assets/images/*.png` | the **device card** picture | review asked for a plain P instead of an illustration; two of my drawings (a barrier scene, then the logo's P) were both rejected by the maintainer, and it is now **iParking's own isometric illustration**, framed by the SVG. Their artwork of their own equipment beats anything drawn here — but it *is* an illustration, so review may raise it again. The P is in git history. |

**The app icon must be real geometry, not a raster in an SVG wrapper.** Embedding
`docs/icon.png` as a base64 data URI validated at `publish` level and previewed correctly in a
browser — and came out wrong on the hub. Homey wants paths here. The trace's numbers are
measured from the PNG (plate `x=26`, `y=47..399`, apex `(425,223)`, corner radius 21; stem
`x=70..86`, `y=95..354` with a round bottom; bowl arc about `(138,180)`, outer radius 85, inner
69) and the bowl's lower terminal stops at `x=105` rather than meeting the stem — that notch is
in the original and is the detail most likely to be "tidied away" by someone who has not
compared the two side by side.

**The `i PARKING™` wordmark is omitted on purpose.** It is 12 px tall on a 447 px canvas; at
icon size it is an illegible smudge, and the alternative was faking letterforms that cannot be
traced faithfully. Omitting a tiny wordmark is ordinary icon design — approximating a
registered wordmark is not.

**The app icon is iParking's logo, on the maintainer's instruction.** Review suggested it and
they supplied the file after being told twice that a third party's trademark on a published
listing is a rights question they own. Both the logo and the store photograph are used on that
basis. Every README still states the app is unaffiliated — keep that, because it is the claim
that makes using the mark a reference rather than a pretence of endorsement.

**The store photograph carries iParking's branding and was supplied by the maintainer**, who
was told plainly that a third party's promotional image on a public listing is a rights
question they own. It is used on their instruction. If it ever has to come out, the vector
scene that stood there for one commit is in git history.

**`assets/icon.svg` must never be made identical to the driver icon again.** It was, on an
explicit request in v0.1.3, and that is exactly what review flagged;
`test_the_app_icon_is_not_the_driver_icon` pins the reversal.

**Every PNG is generated — run `python3 scripts/make_images.py` after touching any SVG.** The
script rasterizes the SVGs directly now. It used to resize a hand-made `docs/app-image.png`
that could silently fall behind the drawing, and did: an edited SVG shipped as the previous
picture for nine days. `test_the_generated_images_are_newer_than_the_art_they_come_from`
makes forgetting the script a test failure instead of a surprise on the store page.

## Capability icons — filled paths, because Homey masks them

Every capability icon lives in `assets/capabilities/mdi-*.svg` and comes from **Material Design
Icons** (Pictogrammers, Apache 2.0). Attribution is in `NOTICE`; the licence copy is beside the
icons. Adding or swapping one means updating `NOTICE` too — that is a licence obligation, not
bookkeeping.

**Homey renders a capability icon as a mask**: it takes the path geometry and ignores `stroke`
entirely. The icon this app shipped until v0.2.2 was a line drawing (`fill="none"
stroke="#000"`), so every sensor and every tile button rendered as a solid black blob — the
outline's own shape filled in. It previewed perfectly in a browser and was only wrong on a hub,
which is why `tests/test_capability_icons_are_filled_paths_not_strokes` exists: any icon whose
geometry is meant to be read as a stroke is wrong here regardless of how it looks in an editor.
MDI is a natural fit because every icon in it is a single filled 24×24 path.

The mapping is one icon per meaning, and a shared icon on the ten tile buttons:
`calendar-today` / `calendar-arrow-right` / `calendar-week` for 오늘·내일·이번 주,
`car` for 현재 주차 중, `car-clock` for 다음 방문 예정, and `boom-gate-arrow-up` for every
자주 오는 차량 button — the buttons do one thing, which is open a gate for a specific car.

## Pair and repair views — English first, Korean layered on

App Store review (2026-08-17, approved with feedback) found `drivers/visitcar/pair/start.html`
and `drivers/visitcar/repair/reconnect.html` **Korean-only** and asked for English in the app's
own UI with translations on top. So in both files:

- the **markup ships English**, which is what a viewer sees before any language resolves;
- an inline `STR = {en, ko}` table supplies the rest, `LANG` starts at `"en"`, and every lookup
  falls back through `STR.en`;
- Korean is applied after `resolveLanguage()` answers — `Homey.language`, then `getLanguage()`
  in callback / promise / string form, then `navigator.language`, then English after 1.5 s so a
  lookup that never answers cannot stall pairing.

`tests/test_store_text.py` pins all three: no Hangul in the markup, both tables present, and
the English fallback intact.

**The strings are duplicated on purpose.** A pair view has no route to `locales/*.json` and this
app serves no endpoint for them — the same constraint `settings/form.js` documents. The
canonical copies still live in `locales/{ko,en}.json` (`pair_need_login`, `pair_slow_login`,
`pair_no_lots`) for the Python side.

**Python-side pair text is English on the wire, with a key beside it.** `pairing.py` returns
`reason` *and* `reason_key`; the view prefers the key because it knows the viewer's language and
the server does not — `compat.ui_language` reports what a *settings* webview last announced,
which during a pairing session is stale or absent. Keep both: the key is the translation, the
text is what an older view or a bare exception message still shows.

## Never logged

The password in any form; the `access_token` value (presence/length only); raw request
bodies, encrypted or plain; `memb_name` (a home address, e.g. `101동0000호`); plates —
mask as `12가****` before any diagnostic output, because diagnostic reports get shared.
Also **a 자주 오는 차량 nickname**: it is free text the user typed and can carry a person
(`장모님차`), the tile already shows it to the only person who needs it, and the slot number
is what a log line actually needs. Notifications are the exception on purpose — they are the
answer to the person who pressed the button, so they carry the full name *and* the full plate.

## Build / install

```sh
homey app validate --level publish   # hygiene gate — run after every change, not a submission
homey app build                      # composes .homeycompose/* into app.json
homey app install                    # build + install to the connected Homey Pro
homey app run                        # dev mode with live logs (diagnose pairing/login)
uv run pytest -q                     # unit tests for the pure client (iparking_lib/iparking/*)
```

**"배포" (deploy)** now matches navien's protocol in full — bump `version` in
`.homeycompose/app.json`, add a KO+EN `.homeychangelog.json` entry, refresh
`README.md`/`README.en.md` (and `README.txt`/`README.ko.txt` if the store copy changed),
commit + push, then `homey app install`. **`homey app publish` is a separate, explicit step**
(guidelines: `y`, "update version?": `n`) and is never run as the tail of an ordinary change —
it starts an external review. Store-text scrubbing applies: `README.txt`/`README.ko.txt` must
contain no "Homey". See `docs/DISTRIBUTION.md`. Never add a `Co-Authored-By: Claude ...`
trailer to commits.

## Status

v0.1.9. `docs/RECON.md` is the verified API contract and `docs/PROBE.md` records the two write
endpoints (`POST /invitations`, `DELETE /invitations/{seq}`) exercised live, once, against the
maintainer's own account. Verified on hardware: sign-in, the 오늘 방문 예정 count, registration
from the settings page, history and its two-press cancel, and the 자주 오는 차량 buttons. Not
yet watched on a real lot: the four sensors added in v0.1.9, and the Flow `date` argument's
field order (mitigated — the success notification echoes the date that was used).
