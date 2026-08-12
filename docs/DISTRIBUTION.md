# Distribution

**Decision (maintainer, 2026-08-12): prepare for the Homey App Store.**
This reverses the 2026-08-04 decision recorded below, which was self-install only.

## What changed, and what did not

The original decision rested on three arguments. Only the first has actually been
withdrawn:

1. ~~"There is no store audience — the app is for one apartment unit."~~ **Withdrawn.**
   The requirement is an iParking MEMBERS account, not *this* account. iParking is
   deployed across many Korean apartment complexes, and every resident of an enrolled
   building with their own unit account can use this. That is a small audience, but it
   is not a personal one, and "personal use only" was the wrong description of it.
2. **It speaks an unofficial, private API and embeds the vendor's AES key as a source
   literal.** *Unchanged, and still true.* Store review may object, and the vendor may
   object. This is a real risk the submission carries, not one that was argued away.
3. **It writes to a real building's physical access-control system.** *Unchanged, and
   still true.* The mitigation is disclosure rather than avoidance: the READMEs, the
   store text and the settings page all say so before a user can act, and the register
   path's design (zero retries, uncertain-outcome guidance that never offers a retry)
   exists precisely because this write is consequential.

Publishing is therefore a decision made **with** items 2 and 3, not despite having
resolved them. If review rejects on either, that is a defensible outcome and the app
falls back to self-install with nothing lost.

## What being store-bound turns back on

These were all switched off by the self-install decision and are now load-bearing
again:

| Item | State |
|---|---|
| `README.txt` / `README.ko.txt` are store copy | **Load-bearing.** Rewritten for a stranger, not for the maintainer. |
| No "Homey" in store-facing text | **Enforced.** The App Store rejects the word in app name, description, changelog and the two `README.txt` files. `README.md` / `README.en.md` are GitHub docs and may use it freely. |
| Public-release cautions in the READMEs | **Required.** A stranger installing this must know it moves a real gate, that it is unofficial and can break without notice, and what travels in cleartext — before they install, not after. |
| Widget store screenshots / preview image | Required **if and when** the dashboard widget ships. It has not. |
| App Store review turnaround gates a release | Yes, for store releases. `homey app install` to the maintainer's own hub stays immediate. |

## What does not change

- **Licence: MIT** (matches `com.lomohome.navien`). This is a clean-room client
  (no upstream code ported), so there is **no `NOTICE` file** — MIT only requires
  reproducing a *ported* work's notice, and nothing here is ported.
- **Version bump + bilingual `.homeychangelog.json` entry per release.**
- **`homey app validate --level publish` after every change.** It was always run as a
  hygiene gate rather than a submission step, and that does not change — it is simply
  now also the thing a submission must pass. It is the strictest level the CLI offers
  (manifest schema, capability schema, image sizes, Flow argument types, i18n
  completeness).
- **The privacy gate.** `scripts/check_no_personal_data.py` runs in CI over tracked
  files, untracked-but-not-ignored files and all reachable git history. Publishing
  raises the cost of a leak; it does not change the mechanism.
- **Sample plates stay synthetic.** `tests/test_sample_plates.py`. A plausible-looking
  Korean plate is indistinguishable from a real vehicle's, and a published app is a
  worse place for one than a private repo.

## Release steps, store-bound

```sh
# 1. bump "version" in .homeycompose/app.json
# 2. add a KO + EN entry to .homeychangelog.json
# 3. refresh README.md / README.en.md, and README.txt / README.ko.txt if the
#    store copy changed — those two must contain no occurrence of "Homey"
uv run pytest -q
uv run ruff check .
python3 scripts/check_no_personal_data.py
homey app validate --level publish
git commit && git push
homey app install          # to the maintainer's own hub, for a last look
homey app publish          # guidelines: y   ·   "update version?": n
```

`homey app publish` is the one step that had never been run for this app. It is
outward-facing and starts a review, so it is run only on the maintainer's explicit
instruction — never as the tail of an ordinary change.

---

## Appendix: the original decision (2026-08-04), superseded

Kept because the reasoning is still the best statement of what this app is, and
because two of its three arguments survive intact above.

> **Self-install only, via `homey app install`. Not published to the Homey App Store.**
>
> This app speaks a reverse-engineered private API, embeds a third party's AES key as a
> source literal, and writes to a real residential building's physical access control
> system. The addressable App Store audience is roughly one apartment unit — an
> iParking MEMBERS account is a single unit at a single enrolled building — so store
> distribution buys essentially nothing while adding review surface for all three of the
> above.
>
> Consequences recorded at the time: store-text constraints and `README.txt` /
> `README.ko.txt` dropped off the critical path (**verified:** `homey app validate
> --level publish` does not require them to exist — that constraint belongs to
> `homey app publish`'s submission path); widget store assets dropped off the critical
> path; and `validate --level publish` still ran after every item as a hygiene gate
> rather than a publish commitment.

The middle claim about the audience is the one that turned out to be wrong, and it was
wrong in a specific way worth remembering: it reasoned from *the maintainer's* account
to *every possible* account.
