# iParking Visitor Parking (Homey)

**An app for someone living in a building that uses iParking, to register parking for
visitors coming to their own home.** It brings the **방문 차량 관리** (visitor vehicle
management) feature of the iParking MEMBERS website into Homey: sign in, see the parking
lot's name, enter a plate number and a visit date to register a visitor, and view or cancel
past registrations.

This is an **unofficial, community-built app and is not affiliated with iParking**. It was
built by reverse-engineering the site's undocumented web bundles — see
[`docs/RECON.md`](./docs/RECON.md) for exactly what was observed and verified live.

> 한국어 README는 [`README.md`](./README.md)를 참고하세요.

## ⚠️ Personal use only — which is why it is not on the App Store

**This app was written for the author's own household, for personal use.** Using it requires
your **own unit's account** at a building enrolled in iParking MEMBERS. One account belongs
to one unit, and it can only register visitors coming to that unit.

So it is **not published to the Homey App Store**. It is installed directly with
`homey app install`. The reasons, in order:

1. **It is a personal-use app to begin with.** It was never built for distribution. The only
   people who *can* use it are those with their own unit's account at an iParking building,
   so there is effectively no store audience to reach.
2. It speaks iParking's **unofficial, private API** and embeds the vendor's encryption key as
   a source literal. The vendor may well not want that, and store review could reasonably
   object to it.
3. It **writes to a real apartment building's physical access-control system** — not the kind
   of behaviour that belongs in broad distribution.

See [`docs/DISTRIBUTION.md`](./docs/DISTRIBUTION.md) for the full reasoning, and for which
parts of the release process apply under self-install and which do not.

**If you use it:** sign in with your own account and register only visitors coming to your
own home. Do not use someone else's credentials, and do not register vehicles unrelated to
you. A registration takes effect on a real barrier gate.

## What you should know before using this (disclosures)

- **This is an unofficial client.** It talks to iParking's own, undocumented internal API.
- **iParking's API host (`members.iparking.co.kr`) downgrades every HTTPS request it
  receives to plain HTTP (a 301 redirect).** As a result, the access token and your plate
  number / visit date travel over the network **unencrypted** after sign-in. This is the
  **vendor server's** behavior, and there is nothing this client can do to fix it.
- By contrast, **the password does travel over verified TLS** to the login server
  (`oauth.parkingcloud.co.kr`) — this disclosure is precise, not alarmist: one host is
  protected and the other cannot be.
- The "encryption" applied to request bodies is **vendor obfuscation using a fixed key
  shipped in every browser**, not confidentiality.
- Your credentials (ID/password) are stored **only on your own Homey**.
- **Registering a visitor vehicle here acts on a real building's access-control system.**
  Do not register test values against a real building.

## Credits · License

This app is not a port of anyone else's code — it is a **clean-room client** written from
observations of the site's public web bundles, recorded in
[`docs/RECON.md`](./docs/RECON.md).

Licensed **MIT** (see `LICENSE`). Because nothing here is ported, there is no `NOTICE`
file.

The Homey app itself is © 2026 Geunwon Mo.

## Features

- **Account sign-in** — sign in with your iParking MEMBERS ID/password from the app
  settings.
- **Parking lot name sensor** — the lot name tied to your account, as a device
  capability.
- **Register a visitor** — the app settings page (the app's primary UI) lets you pick a
  lot, enter a plate number and a visit date, and register. Whitespace in the plate is
  stripped automatically and the cleaned-up value is echoed back once you leave the field
  (on blur), so the normalization is visible rather than silent. "Today" for the visit
  date is always computed in **KST (Korea Standard Time)** — the same regardless of what
  time zone your Homey is in.
- **Flow action** `register_visitor` — register a visitor from an automation.
- **Registration history** — view and cancel past registrations from the app settings
  page.
- **Already-registered guidance** — re-registering the same plate is reported as a
  distinct "already registered" outcome, not a generic error.
- **Uncertain-outcome guidance** — if a registration attempt times out and its outcome
  cannot be confirmed, the app never guesses success or failure. It tells you to check the
  iParking website directly and **never suggests retrying** — a retry is exactly what
  turns one uncertain registration into two real ones on a live building.

## Support

| Item | Status |
| --- | --- |
| Sign-in · parking lot name sensor | Supported |
| Visitor registration (settings page · Flow action) | Supported |
| Registration history · cancel | Supported |
| Multi-building / multi-lot accounts | Supported (only 1 building × 1 lot verified live) |
| Dashboard widget | Planned for v0.1.1 |

## Setup

1. Install the app on your Homey (`homey app install`).
2. Open the **app settings** and sign in with your iParking MEMBERS account.
3. Add a device → **iParking Visitor Parking** → pick the lot tied to your account.
4. From the app settings page, enter a plate number and visit date to register, and
   review the registration history.

## Build

A Homey **Python runtime** app (SDK 3). Its only runtime dependency is `certifi`,
declared in `app.json`'s `pythonPackages` (needed because the Homey Python runtime ships
no system CA store). The request-body AES-256 encryption is vendored in this repo with no
external package (`iparking_lib/iparking/aes.py`) — about 130 lines of a fully specified
primitive, pinned correct against externally generated test vectors.

```sh
homey app build                       # compose .homeycompose/* into app.json
homey app install                     # build and install to the connected Homey
homey app run                         # dev mode with live logs (pairing/login diagnosis)
homey app validate --level publish    # hygiene gate run after every change, not a submission
uv run pytest -q                      # unit tests for the pure client logic
```

Store images are generated from the source vector artwork in `docs/app-image.svg` via
`scripts/make_images.py`.
