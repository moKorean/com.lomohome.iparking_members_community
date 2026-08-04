# iparking MEMBERS — API recon (verified live 2026-08-04)

Source: deobfuscated webpack bundles
`http://members.iparking.co.kr/javascript/{login,visit-car-home,visit-car-detail}.<hash>.js`
(hash currently `e387f2fa9938f0655679`; it is a build hash, **not** required by the API).

**Key conclusion: no browser/DOM automation is needed.** The site is jQuery + a plain JSON
REST API. Every "popup" the user described (intro slider, 공지 popup, 자동로그인) is pure
client-side UI backed by `localStorage`/`sessionStorage` — there is nothing server-side to
skip. A headless HTTP client that speaks the request-encryption scheme is sufficient.

## Transport / crypto

| Item | Value |
|---|---|
| Cipher | AES-256-CBC, PKCS#7 pad (GibberishAES `rawEncrypt`, `size(256)`) |
| Key | ASCII bytes of `DlaCkdAnr!Qwer%@)*FronT$#~KinG!!` (exactly 32 chars → 256-bit) |
| IV | 16 zero bytes (hardcoded `[0,0,...,0]`) |
| Body encoding | **double base64**: `btoa(base64(AES_CBC(utf8(json))))` |
| Response | **plaintext JSON** (jQuery `dataType:'json'`); not encrypted |

Applies to POST/PUT/PATCH bodies only. GET/DELETE send no body.

Verified round-trip with `openssl enc -aes-256-cbc -K <hex> -iv 00..00 -base64 -A | openssl base64 -A`.

## Headers

```
Content-Type: application/json;charset=UTF-8
version: 2.0.0
authorization: <access_token>      # raw UUID, NO "Bearer " prefix
```

`withCredentials: false` — no cookies needed. The `SCOUTER` cookie is irrelevant.

## Hosts and transport — TLS behaviour is asymmetric (verified live)

The bundle only ever uses `http://`. Probing directly shows the two hosts differ, and the
difference decides the app's transport policy:

| Host | Role | HTTPS? | Verified |
|---|---|---|---|
| `oauth.parkingcloud.co.kr` | login (carries the **password**) | **works fully** | `POST https://…/api/oauth/store/authorize` → `HTTP 200`, `ssl_verify_result=0`, `{"result":"0000"}` with a real `access_token` |
| `members.iparking.co.kr` | `/api/members/*` (token + plate data) | **server refuses** | `POST https://…/api/members/parkinglot/list/100001` with `--max-redirs 0` → `HTTP 301` → `http://…` (same for `https://members.iparking.co.kr/`) |

Consequences:

- **Use `https://` unconditionally for the oauth host.** Credentials must not go cleartext,
  and they don't have to. This needs a CA bundle: the Homey Python runtime ships without a
  system trust store (see `navien_lib/navien/tls.py` — `ssl.create_default_context()` there
  trusts nothing), so `certifi` must be declared in `pythonPackages`. `certifi` is a
  pure-Python **noarch** wheel, so it carries no Docker/QEMU cross-build cost.
- **Use `http://` for the API host, knowingly.** The 301 downgrade is the server's choice and
  is not fixable client-side. "https-first with fallback" there is dead code that always falls
  back. Never *follow* an https→http redirect, so no code can believe it used TLS when it
  didn't; assert the **final** (post-redirect) scheme per host.
- The bundle derives the API origin from `auth_data.operation_company[0].domain + '/api/members'`.
  That value is literally `"http://members.iparking.co.kr"` — take the **host** from it and set
  the scheme by policy, rather than using the URL verbatim.
- Only the bearer token and plate/date data ever traverse cleartext. Disclose this in the
  README and settings page.

## Connection reliability — `members.iparking.co.kr` resets ~30 % of connections

**Measured live 2026-08-04.** 20 identical read-only requests to the cleartext host:
**14 answered, 6 died mid-exchange.** One session in production hit it three times over —
a registration, its recovery re-query, and device pairing all failed at once — which is how
it was found.

The same fault has two names depending on where you are standing:

| Where | How it surfaces |
|---|---|
| macOS (dev, outside Docker) | `ConnectionResetError`, errno 54 |
| the Homey hub's Python runtime | `http.client.IncompleteRead(255 bytes read)` |

`IncompleteRead` is worth its own note: it subclasses `http.client.HTTPException`, **not**
`OSError`, so a handler that catches `URLError`/`OSError` does not catch it. That is a real
trap — it is why one failed registration produced three request log lines and then no error
and no traceback at all.

### What it is not — ruled out by measurement, so do not re-litigate

- **Not header-dependent.** Tried default urllib UA, a browser UA, `Accept-Encoding: gzip,
  deflate`, and no `Accept-Encoding`. Failures land randomly across all of them: the
  default-UA request *passed* while the browser-UA one failed, then the same browser UA
  passed once another header was added.
- **Not a dev/Docker artefact.** Reproduced from a Mac entirely outside Docker.
- **Not a rate limit and not a block.** `curl` succeeds interleaved with failing `urllib`
  calls. `curl` survives because it retries internally — **that is the entire difference.**
- **Not the oauth host.** HTTPS to `oauth.parkingcloud.co.kr` is reliable. Only the cleartext
  host does this.

### Retry policy, per endpoint — never per HTTP method

At P(fail) = 0.3 the arithmetic that sizes this is:

| attempts | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| P(all fail) | 30 % | 9 % | 2.7 % | **0.8 %** | 0.24 % |

This API serves **reads over POST**, so "retry POSTs" is not a usable rule — it would retry
the vehicle registration. The policy is therefore keyed on what an endpoint *means*:

| Endpoint | Attempts | Why |
|---|---|---|
| `POST {oauth}/api/oauth/store/authorize` | 4 | a retry just mints another token |
| `POST {origin}/invitations/list` | 4 | read |
| `POST {origin}/parkinglot/list/{seq}` | 4 | read (pairing — the most exposed path) |
| `GET {origin}/invitations/{seq}` | 4 | read |
| `DELETE {origin}/invitations/{seq}` | 4 | idempotent: re-cancelling returns `13001`, a no-op |
| recovery re-query after a register | **5** | it resolves the uncertainty the line below creates |
| **`POST {origin}/invitations`** | **1 — zero retries** | see below |

**`POST /invitations` must never retry.** A reset cannot distinguish "the request never
arrived" from "it arrived, the vehicle was registered, and the reply died with the socket". A
second POST resolves nothing and risks registering a visitor vehicle **twice at a real
building**. The answer to that ambiguity is the recovery *read*, which is exactly why the
re-query retries harder than anything else in the app: when it fails, a knowable outcome
becomes a bare "we cannot tell you" for the user.

**A reset is not a timeout, and the two must stay separate classes.** A reset means the
connection is gone and nothing is in flight, so a read may safely ask again. A timeout means
the request may **still be running and may still land**. Conflating them is what would make
the write retryable by accident — note that `TimeoutError` is an `OSError` subclass, so any
classifier that leads with an errno test gets this wrong.

Backoff is exponential and **jittered** (0.3 s base, ×2, capped at 2 s, ±50 %). The jitter is
not decoration: paired devices poll on the same hour and must not line their retries up.

## Endpoints (all verified live except the two marked)

### 1. Login — `POST {oauth}/api/oauth/store/authorize`
Encrypted body:
```json
{"client_id":"<id>","client_pwd":"<pw>","client_os_type":"WEB"}
```
(`client_device_id` / `client_device_token` are push-only and dropped when empty by
`SearchParamsTheorem`. Omitting them works — verified.)

Response (real, redacted):
```json
{"result":"0000","resultMessage":"성공",
 "auth_data":{
   "access_token":"<redacted-uuid>","token_type":"bearer","expires_in":604800,
   "operation_company_cds":[1],
   "operation_company":[{"domain":"http://members.iparking.co.kr","operation_cmpy_cd":1}],
   "memb_name":"101동0000호","stor_name":"101동0000호",
   "invitation_authorization_list":[{"stor_seq":100001,"invitation_register_authorization_yn":"Y"}],
   "cmpy_seq":100002,"alliance_group_id":"iparking",
   "parkinglot_list":[{"parkSeq":9001,"operationCd":1,…}]}}
```
- Token TTL **7 days** (604800 s).
- `auth_data` on success; some deployments return `resultData` instead — handle both.
- `stor_seq` comes from `invitation_authorization_list[0].stor_seq`; that entry also
  carries `invitation_register_authorization_yn` — gate registration on `"Y"`.
- Wrong password ⇒ `result` `2002` (`loginError`).

### 2. 주차장 목록 — `POST {origin}/parkinglot/list/{stor_seq}`
No body. Verified:
```json
{"result":"0000","totalCnt":1,
 "resultData":[{"park_seq":9001,"lot_id":"1160009001",
                "park_name":"예시동 샘플아파트[출입통제A]","park_group_id":null}]}
```
→ this `park_name` is the 주차장명. It is used at **pairing time** to name the device and is
stored in the device store; it was a sensor capability up to v0.1.3 and is no longer one — the
value never changes and it duplicated the device's own name. The device's sensor is now
오늘 등록된 차량 수, read from `POST /invitations/list` (§4) with a one-day window.

### 3. 방문차량 등록 — `POST {origin}/invitations`  *(NOT yet exercised — write op)*
```json
{"parkSeq":9001,"storSeq":100001,
 "userId":"<client_id>","userName":"<memb_name>",
 "invitationDate":"20260805",
 "invitationInfoList":[{"carNumber":"12가4567","memo":"",
                        "mobile1":"010","mobile2":"1234","mobile3":"5678"}]}
```
- `invitationDate`: `yyyyMMdd`, no separators (UI does `.replace(/\./g,'')`).
- `mobile1/2/3` present only when a phone was entered (split by
  `/^(?:(010|011|016|018|019)(\d{3,4})(\d{4}))$/`); omit all three otherwise.
- `invitationInfoList` is an array — batch registration is native.
- Per-car result codes the UI knows: `SUCCESS`, `FAIL`, `EXIST` (기등록 차량);
  top-level `10003` = `registeredCar`.

### 4. 등록 내역 — `POST {origin}/invitations/list`
```json
{"startDate":"20260501","endDate":"20260811","carNumber":"",
 "storSeq":100001,"parkSeq":9001,"current_page":1,"page_size":15}
```
Verified response:
```json
{"result":"0000","totalCnt":43,
 "resultData":{
   "total":[{"inot_status":"RESERVE","cnt":n},{"inot_status":"IN","cnt":n}],
   "invitationList":[{"invitation_date":"20260501","invt_seq":3184553,
                      "car_number":"12가3456","inot_status":"RESERVE",
                      "park_name":"예시동 샘플아파트[출입통제A]","seq_num":1.0}]}}
```
- `inot_status`: `RESERVE` 미입차 / `IN` 주차중 / `OUT` 출차 / `CANCEL` 취소.
- `resultData.total` was `[]` on my query (date range had no IN/RESERVE aggregation) —
  treat as optional and default counts to 0.
- History window server-side: **최근 3개월만** (UI sets `minDate:'-3m'`).
- Pagination: `totalPage = ceil(totalCnt / page_size)`; UI infinite-scrolls
  `current_page += 1`.
- **`page_size` is honoured verbatim — verified.** Same query at `page_size:15` → 15 rows;
  at `page_size:100` → **all 43 rows in one response** (`totalCnt` 43 both times). So the
  whole history can be fetched in a single request; client-side pagination is a display
  concern, not a fetch concern.
- **`resultData.total` is `[]` even on a range returning 43 records — verified twice.** Do
  not rely on it for status counts. Treat it as optional display metadata: render an
  aggregate row if it is ever non-empty, omit it otherwise. Per-row `inot_status` is the
  authoritative status source.

### 5. 상세 — `GET {origin}/invitations/{invt_seq}`
Returns `park_name`, `invitation_date`, `car_number`, `inot_status`, `mobile_1/_3`, `memo`.

### 6. 수정 — `PUT {origin}/invitations`
`{parkSeq, parkName, invtSeq, invitationDate, carNumber, memo, mobile1..3}`

### 7. 취소 — `DELETE {origin}/invitations/{invt_seq}`
No body. Codes: `13001` alreadyDeleted, `13002` cannotDelete.

Exercised live 2026-08-04 (see `docs/PROBE.md`). Two facts that shape the client:

- It does **not** delete the row. It flips `inot_status` to `CANCEL`; the row keeps its
  `invt_seq` and stays in 등록 내역. A caller looking for the row's *disappearance* would
  report a working 취소 as broken.
- Re-cancelling an already-cancelled row returns `13001 alreadyDeleted` and changes nothing.
  That makes this endpoint **idempotent**, which is why it may retry on a connection reset
  even though it is a write — the two readings of a reset leave the same end state. Contrast
  `POST /invitations`, which has no such property.

### 8. SMS 발송 — `POST {origin}/invitations/{invt_seq}/sms`
No body.

## Car-number validation (client-side, newest of 3 regexes in the bundle)

```
/^(?:(?:[가-힣]{2}|\d)\d{1,2})[가-힣]\d{4}$|^임(?:\d{4}|\d{6})$|^(?:(?:외교|영사|준외|준영|국기|협정|대표)\d{6})$/
```
Accepts `12가1234`, `123가1234`, `서울12가1234`, `임1234`, `임123456`, `외교123456`.
UI hint text: `예시) 12가1234, 임1234, 임123456, 외교123456`.

**User requirement:** strip *all* whitespace before validating/sending —
`"12가 4567"` → `"12가4567"`. (The site itself does **not** do this; it just rejects.)

## Result codes worth mapping (from `Ajax.resultCode`)

`0000` success · `1001` fail · `1002` dbError · `1009` sessionExit · `2001` noId ·
`2002` loginError · `2031` tokenNotFind · `2041` tokenUserNotFind · `2042` passwordError ·
`10003` registeredCar · `12100` notFindStore · `12105` notAllowed · `13001` alreadyDeleted ·
`13002` cannotDelete

`2031`/`2041`/`1009` ⇒ token expired/invalid ⇒ re-login and retry once.

## Risks / notes for planning

1. **Hardcoded shared AES key.** The encryption is obfuscation, not security: key and zero
   IV are shipped to every browser. Disclose in README/settings; never log credentials.
   Transport is *not* uniformly cleartext — see the host table above: login can and must go
   over TLS; the API host forces plain HTTP by 301.
2. **No dependency for AES available by default.** Homey's Python runtime has stdlib
   only + whatever `pythonPackages` declares. `hashlib`/`hmac` exist; there is **no**
   stdlib AES. Options: (a) declare `pycryptodome` in `pythonPackages`, (b) vendor a
   ~120-line pure-Python AES-256-CBC. Decision needed.
3. **Token TTL 7 days**, no refresh endpoint → re-login on `2031/2041/1009`.
4. `stor_seq` / `park_seq` are per-account and discovered at login; there may be more
   than one parking lot per store (`parkinglot/list` returns an array).
5. Registration is a **real-world mutation** on a live building's access-control system.
   Any automated test must clean up via `DELETE /invitations/{seq}`.
6. **The API host drops ~30 % of connections** (measured; see the reliability section above).
   Every read has to retry to be usable at all, and the one write that must *not* retry needs
   a recovery read behind it. Any new endpoint added to this client needs an explicit
   attempts decision made on its **semantics**, because the method cannot tell you: this API
   serves reads over POST.

---

## Appendix A — the double-base64 envelope, as recorded values

The body-encoding section above states the *method* (`btoa(base64(AES_CBC(utf8(json))))`)
but records no envelope **value**, so there was nothing for a byte-exact test to target.
This appendix supplies that: four bodies with the exact wire string each produces.

**Provenance.** The ciphertext below was produced by `openssl`, not by this repository's
`iparking_lib/iparking/aes.py`. That is the point — `tests/test_crypto.py` asserts our
encoder reproduces these strings, so the assertion is anchored outside the code under
test. `scripts/gen_aes_fixtures.sh` regenerates them into
`tests/fixtures/envelope_kat.json`; the tests read the committed file and never invoke
`openssl` themselves. Generated with OpenSSL 3.6.3.

```sh
KEY=446c61436b64416e7221517765722540292a46726f6e5424237e4b696e472121   # ASCII of the key literal
IV=00000000000000000000000000000000
inner=$(printf '%s' "$BODY" | openssl enc -aes-256-cbc -K $KEY -iv $IV -base64 -A)
printf '%s' "$inner" | openssl base64 -A          # ← the wire value
```

The command substitution around the inner call is load-bearing: it strips the trailing
newline `openssl` writes. Leave that newline in and the outer base64 encodes it too,
making every envelope wrong by four characters — and the server's only answer is a parse
failure indistinguishable from a bad password.

**Bodies are `JSON.stringify` byte-exact:** no space after `:` or `,`
(`separators=(",", ":")`), and **no `\uXXXX` escaping** (`ensure_ascii=False`). The
Hangul cases exist to pin that second point — `ensure_ascii=True` would turn
`12가4567` into `12가4567`, changing the plaintext's bytes *and* its length, which
lands it in a different PKCS#7 bucket.

**None of these carries real account data.** The register bodies use `999동9999호` rather
than the account's actual `memb_name`, which is a home address. (For the same reason the
`memb_name` in the login-response sample earlier in this document should be redacted —
see the note at the end of this appendix.)

### A.1 `login` — 96 bytes ≡ 0 mod 16

```
{"client_id":"iparking-dev","client_pwd":"synthetic-not-a-real-password","client_os_type":"WEB"}
```
```
S1F2ZVpPeUZYblhNUFplTlJ6RDVvRGRxZWVUSklYUUh1cGdueGZ5dFZIUFdMRlRsWjkrUytySkhWcyt0RGhoSGJjMDZyMVFyTTJabzdoVmRYZ3JtQ3VhenhjUFZUUGlmSUlQQkNlTEhsSmczN1g3bHNxNVVpdk05eUhBbzZXUTJ1OXRScjZQak9JRDNQeWtsWllFQmF3PT0=
```

### A.2 `plate_hangul` — 25 bytes ≡ 9 mod 16

```
{"carNumber":"12가4567"}
```
```
cXBqeUpKVXhxbHVnb2NELzd6QTg4dENYWEo5OXIvc0JvYjNmaXpUQ3ByOD0=
```

### A.3 `register` — 221 bytes ≡ 13 mod 16 (the §3 body shape)

```
{"parkSeq":9001,"storSeq":100001,"userId":"iparking-dev","userName":"999동9999호","invitationDate":"20260805","invitationInfoList":[{"carNumber":"12가4567","memo":"","mobile1":"010","mobile2":"1234","mobile3":"5678"}]}
```
```
a2g5OE5yTGlIamg2cERvVDRGSmpTaWwrVXk3eHpadlVId253U0hteEJ6cHBTWFdoRWNpZ0NNSGQ4ME4yajd1ckI4WWUvdlZVMUQwSVVhSGxhdWovVGpIRDlsWEtyWXp1Y1hQNTArTEJxTjJpMUdDVi9JZUlDc3hRc1VMd0VwMlNaSzVpcE90S3JjUm13N3FKSWJLK2FjeXRVMXUxTHB3WW91ZDBlb0NoMjR0VDlXZS96aVgvWTZoTW1FOVR3QUIyeHNTeWZoTVFrYnd2N1llZ0hmeUdnK0IwTUFGWU1tVmtLSlptVkN6VDY0eWlGQ3Q5b0hKeTZPZCt0QmcrSHJvQ09SS2RzNDJJZmNVNTVuTlRVZGVvWnVIMTJkdSt4OUdmWWJUZ0pUdXdQcXM9
```

### A.4 `register_block_aligned` — 224 bytes ≡ **0** mod 16

Same as A.3 with `"memo":"abc"`. This is the case that exists for a specific quiet bug:
a register body landing exactly on a block boundary. An implementation that skips PKCS#7's
whole-extra-block rule on aligned input still logs in fine (A.1 is aligned too, but a
login failure is loud and immediate) and still round-trips against its own decoder — it
breaks `POST /invitations` **only** for plates whose JSON happens to hit a 16-byte
multiple, surfacing later as a partial per-car result nobody can reproduce.

```
{"parkSeq":9001,"storSeq":100001,"userId":"iparking-dev","userName":"999동9999호","invitationDate":"20260805","invitationInfoList":[{"carNumber":"12가4567","memo":"abc","mobile1":"010","mobile2":"1234","mobile3":"5678"}]}
```
```
a2g5OE5yTGlIamg2cERvVDRGSmpTaWwrVXk3eHpadlVId253U0hteEJ6cHBTWFdoRWNpZ0NNSGQ4ME4yajd1ckI4WWUvdlZVMUQwSVVhSGxhdWovVGpIRDlsWEtyWXp1Y1hQNTArTEJxTjJpMUdDVi9JZUlDc3hRc1VMd0VwMlNaSzVpcE90S3JjUm13N3FKSWJLK2FjeXRVMXUxTHB3WW91ZDBlb0NoMjR0VDlXZS96aVgvWTZoTW1FOVR3QUIyeHNTeWZoTVFrYnd2N1llZ0hmeUdnMks3cUM0OEtsR3FLRCtOeVNWL1pnelhRcGduMGg0dGYwcG9IdmVWTWREQS80YkpsRnZieXo5MVpiUWpxT3I4NlRkYkFVb3JrWE5valY1cTV5dkFPTno3cGZMbkQrRUhuVWtSeGluVnBvVjQ=
```

### A.5 Note on the AES test anchors

The plan and the item-2 brief both cite "NIST SP 800-38A **F.2.1 / F.2.2** AES-256-CBC".
That numbering is wrong: in SP 800-38A, F.2.1/F.2.2 are CBC-**AES128**.Encrypt/Decrypt.
The AES-**256**-CBC vectors are **F.2.5 / F.2.6**, and those are what `tests/test_aes.py`
uses, together with FIPS 197 C.3 for the bare block cipher. Honouring the citation
literally would have meant adding a 128-bit key schedule to `aes.py` that nothing else
needs — a new unexercised branch, added to satisfy a typo.

### A.6 Privacy note — please redact

`memb_name` is `101동0000호`, which is **the maintainer's home address**, and it appears
in the login-response sample in §1 of this document. The no-log rule already covers it at
runtime; this document is the remaining copy. Recommend replacing it with `101동0000호`
there. No fixture in `tests/` contains it.
