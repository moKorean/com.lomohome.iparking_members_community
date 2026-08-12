# iParking Visitor Parking (Homey)

**An app for someone living in a building that uses iParking, to register parking for
visitors coming to their own home.** It brings the **방문 차량 관리** (visitor vehicle
management) feature of the iParking MEMBERS website into Homey: sign in, enter a plate number
and a visit date to register a visitor, see at a glance how many vehicles are expected today,
tomorrow and this week, how many are parked inside right now and when the next visit is due,
and view or cancel registrations.

This is an **unofficial, community-built app and is not affiliated with iParking**. It was
built by reverse-engineering the site's undocumented web bundles — see
[`docs/RECON.md`](./docs/RECON.md) for exactly what was observed and verified live.

> 한국어 README는 [`README.md`](./README.md)를 참고하세요.

## Before you install

This app needs **your own unit's account at a building enrolled in iParking MEMBERS**. One
account belongs to one unit and can only register visitors coming to that unit. If that is not
you, there is nothing here to use.

And this app moves a **real barrier gate**, not a document. Four things to know first:

1. **A registration takes effect on a real building's access-control system, immediately.** Do
   not enter an arbitrary plate to see what happens — that vehicle really is granted entry.
2. **Sign in with your own account, and register only visitors coming to you.** Nothing here
   technically prevents otherwise; that is not the same as it being acceptable.
3. **This is an unofficial client.** It was built by observing an internal API iParking has
   never published. **The vendor can change it and break this app without notice**, and can
   block this client. iParking is not affiliated with this app and owes it no support — if
   something breaks, raise an issue on this repository rather than contacting your building
   office or iParking.
4. **Traffic after sign-in is not encrypted**, because iParking's API server redirects every
   HTTPS request down to plain HTTP. The [disclosures](#what-you-should-know-before-using-this-disclosures)
   below say exactly what travels in the clear. The password itself does go over verified TLS.

**What it does not do**, stated as plainly: it sends your credentials nowhere but iParking's
own servers, performs no analytics, telemetry or remote logging, and runs no server that
anyone — the author included — could reach. Your ID and password live only on your own Homey.
The access token is held **in memory only** and never persisted, so it cannot reach a hub
backup.

The distribution decision and its reasoning are in
[`docs/DISTRIBUTION.md`](./docs/DISTRIBUTION.md).

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

Licensed **MIT** (see `LICENSE`). No **code** here is ported from anywhere.

**The artwork is a different matter.** The device tile's capability icons come from
[Material Design Icons](https://pictogrammers.com/library/mdi/) by the Pictogrammers group,
under Apache 2.0. [`NOTICE`](./NOTICE) records which icon is used where and what was changed
from the originals; the full licence text is at `assets/capabilities/LICENSE-mdi.txt`.

The Homey app itself is © 2026 Geunwon Mo.

## Features

- **Account sign-in** — sign in with your iParking MEMBERS ID/password from the app
  settings.
- **Five sensors on the device tile — one request** — the state of this lot, as device
  capabilities.

  | Sensor | What it says |
  |---|---|
  | Expected today | Visitor vehicles registered for today (`2 cars`) |
  | Expected tomorrow | The same, one day on |
  | Expected this week | The next 7 days **from today** — rolling, not a calendar week, so a Sunday evening still shows Monday's guests |
  | Parked now | Visitor vehicles currently inside the complex. The counts say *expected*; this says *here* |
  | Next visit | The soonest upcoming day and plate (`8/15 Sat · 12가1234`) |

  All five come out of **the same single read** — four extra sensors, and not one extra
  request. They refresh hourly (± 10 %), update immediately and at **no extra request** when
  this app registers, cancels or reads the history, and roll over at midnight KST. The counts
  have Insights enabled.

  **Cancelled registrations are not counted** — iParking's 취소 flips a row's status to
  `CANCEL` rather than removing it, so counting them shows 6 on a day whose honest answer is
  1. **"Parked now" excludes vehicles that have left** — a departed car is still a valid
  registration, just not a present one.
- **Register a visitor** — the app settings page (the app's primary UI) lets you pick a
  lot, enter a plate number and a visit date, and register. Whitespace in the plate is
  stripped automatically and the cleaned-up value is echoed back once you leave the field
  (on blur), so the normalization is visible rather than silent. "Today" for the visit
  date is always computed in **KST (Korea Standard Time)** — the same regardless of what
  time zone your Homey is in.
- **Two Flow actions** — `register_visitor` ("Register a visitor (choose a date)") takes a
  plate and an optional visit date; `register_visitor_today` ("Register a visitor (today)")
  takes only a plate and always registers for today in KST. Both cards go through the same
  register path and differ only in where the date comes from.
  <br>The date-picking card **echoes the date it actually used back in the success
  notification** (`12가1234 · registered for 2026-08-05 (Wed)`). Whether Homey hands a `date`
  argument over as `dd-mm-yyyy` or `mm-dd-yyyy` is not pinned anywhere, and the two are
  **shape-identical**, so a misread would register the wrong day *silently*. Where the values
  decide it (`25-12-2026`, whose first field exceeds 12) the app resolves it correctly on its
  own; where they cannot, one glance at the notification's date exposes it. Worth checking the
  first time you use the date-picking card.
- **Frequent-vehicle buttons (up to ten)** — the device settings hold twenty text fields:
  **frequent vehicle name 1–10** and **plate number 1–10**. Every slot where **both** halves are
  filled in and the plate validates gets its own button on the device tile — `[엄마차 방문 등록]`
  — and pressing it registers that vehicle for **today in KST**. They are **momentary push
  buttons**: a press does not leave the control switched on, so you can press one as often as
  you like. Each runs the **same register path** as the Flow cards, so the no-retry write, the
  "already registered" outcome and the uncertain-outcome guidance all behave identically. Saving
  normalizes the plate in place so you see what was stored (`12가 1234` → `12가1234`). A
  half-filled slot or an invalid plate produces no button, and the log says which slot and why.
- **Registration history** — view and cancel registrations from the app settings page,
  **newest visit first**. The window runs **three months back and three months ahead**, so
  visits that have not happened yet sit at the top of the table — those are the ones you
  actually want to check. Cancelling takes **two presses**: the first turns the button into
  `Really cancel?`, the second performs it, and it reverts after five seconds. It withdraws a
  real access grant immediately, so it should not fire on one stray click.
- **Already-registered guidance** — re-registering the same plate is reported as a
  distinct "already registered" outcome, not a generic error.
- **Uncertain-outcome guidance** — if a registration attempt times out and its outcome
  cannot be confirmed, the app never guesses success or failure. It tells you to check the
  iParking website directly and **never suggests retrying** — a retry is exactly what
  turns one uncertain registration into two real ones on a live building.

## Support

"Supported" and "verified on hardware" are listed separately on purpose. This app was written
by observing an undocumented API, so having implemented something and having watched it work at
a real building are two different claims.

| Item | Status |
| --- | --- |
| Sign-in · "expected today" count sensor (with Insights) | **Verified on hardware** |
| The four newer sensors (tomorrow, this week, parked now, next visit) | Supported — same read, same filters, but not yet watched on a real lot |
| Visitor registration (settings page) | **Verified on hardware** |
| Registration history · cancel (two-press) | **Verified on hardware** |
| Frequent-vehicle buttons (20 device settings → up to 10 push buttons) | **Verified on hardware** |
| Visitor registration (2 Flow actions) | Supported — see the note above on the date argument |
| Multi-building / multi-lot accounts | Supported — the code handles N buildings × M lots, but the real account is 1 × 1, so only that case is verified |
| Editing a registration (`PUT /invitations`) | Not supported — the endpoint is known but was never exercised |
| Notifying a visitor by SMS | Not supported — as above |
| Dashboard widget | Planned only |

Verified against: a Homey Pro (firmware 13.x), Python runtime 3.14, one iParking MEMBERS account
(one building, one lot).

## Setup

1. Install from the Homey App Store if it is listed there, or from source with
   `homey app install`.
2. Open the **app settings** and sign in with your iParking MEMBERS account.
3. Add a device → **iParking Visitor Parking** → pick the lot tied to your account.
4. From the app settings page, enter a plate number and visit date to register, and
   review the registration history.
5. Optional: put your frequent visitors into the **device settings** as name/plate pairs. Each
   complete pair gets a button on the device that registers it for today in one press.

## Known limits

Not defects in this app but properties of the server it talks to, and you will meet them.

- **The iParking API host resets about a third of its plain-HTTP connections.** Measured: 20
  identical read requests returned 14 answers and 6 `ConnectionReset`, at random and regardless
  of headers or environment. So reads, sign-in and cancel **retry** (4 attempts, 0.8 % residual).
  **A registration never retries** — a reset cannot distinguish "the request never arrived" from
  "it arrived and only the reply was lost", so retrying could register the same vehicle twice.
  Instead the app re-queries to find out what happened, and if that cannot settle it either, it
  tells you the outcome is **uncertain** rather than guessing.
- **"Parked now" can be up to an hour stale.** iParking offers no webhook and no push, so the
  only way to notice a car entering is the hourly poll — and a visit that starts and ends
  between two polls never appears at all. For the same reason there is **no "a visitor
  arrived" Flow trigger**: making one prompt would mean polling hard enough to risk the vendor
  blocking this client, which is a bad trade for a notification.
- **Pressing a button on the device tile shows no toast.** The Homey SDK has no success-toast
  API — the whole surface was checked — so the outcome goes to the **timeline** instead.
  Registering from the app settings page does show an in-page toast.
- **A visit date must be within 80 days of today.** The history endpoint's *backward* reach is
  limited to three months; whether the *write* endpoint enforces the same bound was never
  verified, so this is a deliberately conservative cap. The history table reads 90 days ahead,
  so every date this app can register is a date it can also show you.

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
homey app validate --level publish    # run after every change; also the submission gate
uv run pytest -q                      # unit tests for the pure client logic
```

Store images are generated from the source vector artwork in `docs/app-image.svg` via
`scripts/make_images.py`.
