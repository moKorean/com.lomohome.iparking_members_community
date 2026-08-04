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

## Distribution — self-install, not the App Store

**This app is not published.** It is installed directly with `homey app install`. See
[`docs/DISTRIBUTION.md`](./docs/DISTRIBUTION.md) for the full reasoning and for exactly
which parts of navien's 배포 protocol carry over (version bump, bilingual changelog,
refreshed READMEs) and which don't (`homey app publish`, store-text scrubbing, widget store
assets — none of them apply here). Because there is no submission, `README.txt`/
`README.ko.txt` are produced for parity with the sibling app but are **not** load-bearing:
`homey app validate --level publish` does not check for their existence at all — that
constraint belongs to `homey app publish`, which this app's release process never runs.

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
- `.homeycompose/` — manifest head, `capabilities/iparking_park_name.json`, five
  `capabilities/iparking_quick_{1..5}.json` tile buttons (declared up front because Homey has
  no dynamic-capability declaration; each device adds and removes them at runtime, so
  `MAX_FAVORITES` and these five files have to change together),
  `flow/actions/register_visitor.json`. **Never hand-edit root `app.json`** (generated).
- `settings/{index.html,form.js}` — the app's **primary UI** (Homey has no free-text tile
  control). `form.js` is a plain module, not tied to the settings page's DOM, so the
  dashboard widget planned for v0.1.1 can mount the same module in ~30 lines instead of a
  rewrite.
- `drivers/visitcar/` — driver shim, pairing views, assets. One device **per parking lot**.
  `driver.compose.json` carries the ten 자주 오는 차량 settings (5 이름 + 5 차량번호) in a
  `favorites` group; the driver's static `capabilities` list stays at one, because a freshly
  paired device must start with **zero** buttons.

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

**"배포" (deploy)** carries over from navien's protocol only in part — bump `version` in
`.homeycompose/app.json`, add a KO+EN `.homeychangelog.json` entry, refresh
`README.md`/`README.en.md`, commit + push, then `homey app install`. It does **not** include
`homey app publish` (never run for this app) or store-text scrubbing (`README.txt`/
`README.ko.txt` may say "Homey" freely — no store review reads them). See
`docs/DISTRIBUTION.md` for the full table. Never add a `Co-Authored-By: Claude ...` trailer
to commits.

## Status

v0.1.0 is under active development toward the acceptance criteria tracked by the
maintainer's planning workspace (outside this repo). `docs/RECON.md` is the verified API
contract; `docs/PROBE.md` (once item 3 lands) will record the two write endpoints
(`POST /invitations`, `DELETE /invitations/{seq}`) exercised live, once, against the
maintainer's own account.
