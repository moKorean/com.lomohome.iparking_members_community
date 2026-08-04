# Distribution

**Decision (maintainer, 2026-08-04): self-install only, via `homey app install`.
Not published to the Homey App Store.**

## Why

This app speaks a reverse-engineered private API, embeds a third party's AES key
as a source literal, and writes to a real residential building's physical access
control system. The addressable App Store audience is roughly one apartment
unit — an iParking MEMBERS account is a single unit at a single enrolled
building — so store distribution buys essentially nothing while adding review
surface for all three of the above. See `.omc/plans/iparking-members-community.md`
§9.1 for the full reasoning; this file records only the consequence for how the
repo is built.

## Consequences

1. **Store-text constraints and `README.txt`/`README.ko.txt` drop off the v0.1.0
   critical path.** The App Store rejects the word "Homey" in store-facing text
   (app name/description, changelog, `README.txt`/`README.ko.txt`). Since this
   app is never submitted for review, that constraint does not apply: `README.md`/
   `README.en.md` (the GitHub-facing docs) may mention "Homey" freely.
   **Verified:** `homey app validate --level publish` does not require
   `README.txt`/`README.ko.txt` to exist at all — this repo validates clean at
   `publish` level with no README files present (confirmed empirically at item
   1: `homey-lib`'s validator has no README check; that constraint belongs to
   `homey app publish`'s App Store submission path, never exercised here).
   `README.txt`/`README.ko.txt` are still produced later (item 8) for hygiene
   parity with the sibling app (`com.lomohome.navien`), but no one is
   proof-reading them against store copy rules, and their absence is not what
   `validate --level publish` is gating.

2. **Widget store assets drop off the v0.1.0 critical path.** The dashboard
   widget itself is deferred to v0.1.1 (see the architecture plan §3.3): a
   widget requires configured credentials to be anything but inert, so it
   cannot be the primary surface. Store-only widget assets (store screenshots,
   the widget preview image) that the App Store review process would otherwise
   require are moot with no submission — they are not produced for v0.1.0 and
   are not blocking when the widget ships in v0.1.1 either, unless that release
   also flips distribution.

3. **`homey app validate --level publish` still runs, every item, as a hygiene
   gate — not a publish commitment.** Self-install does not relax the schema:
   this is the strictest validation level Homey's CLI offers (manifest schema,
   capability schema, image sizes, Flow argument types, i18n completeness), and
   running it after every item catches the same class of mistakes a reviewer
   would catch, before they reach the maintainer's actual hub. It is run because
   it is free and thorough, not because a submission is coming.

## What does NOT change

- **Licence: MIT** (matches `com.lomohome.navien`). This is a clean-room client
  (no upstream code ported), so there is **no `NOTICE` file** — MIT only
  requires reproducing a *ported* work's notice, and nothing here is ported.
- **Version bump + bilingual `.homeychangelog.json` entry per release** — same
  discipline as navien's 배포 protocol, because it costs nothing and keeps the
  two sibling apps consistent for the same maintainer to read later.
- **`homey app build` / `homey app install`** — identical mechanics to a
  store-bound app; only `homey app publish` is never run.

## What DOES change vs. navien's 배포 protocol

| navien (published) | this app (self-install) |
|---|---|
| `homey app publish` (answer guidelines **y**, "update version?" **n**) | never run |
| Store text scrubbed of "Homey" | not scrubbed — no store review reads it |
| Widget store screenshots/preview required before a widget ships | not required at any point |
| App Store review turnaround gates a release | nothing external gates a release; `homey app install` is immediate |

Everything else — version bump, bilingual changelog, refreshed READMEs, commit,
then `homey app install` to the maintainer's hub — carries over unchanged.
