/**
 * settings/form.js — 방문차량 등록 · 등록 내역 · 취소, as a plain module.
 *
 * This file holds every decision the settings page makes and touches **no DOM**. That is a
 * requirement rather than a style preference: the dashboard widget planned for v0.1.1 is a
 * second surface over the same three actions, and a widget that has to re-derive "is
 * `can_register: null` a refusal?" or "does a cancelled row disappear?" is a widget that will
 * eventually answer one of them differently from this page. `settings/index.html` is therefore
 * only markup + wiring, and the widget's mount is expected to be ~30 lines of the same wiring.
 *
 * Load order on the settings page is `/homey.js`, then this file, then the inline wiring —
 * it publishes `window.IparkingForm`. Under Node (the pytest parity test) the same file is
 * `require()`-able, which is what lets `normalizePlate` be checked against
 * `iparking_lib/iparking/plate.py` instead of being trusted to agree with it.
 *
 * ## The four rules that are not negotiable
 *
 * 1. **`can_register: null` is not `false`.** `null` means "could not be determined" — usually
 *    no account saved yet. `false` is the building office refusing, and only that earns the
 *    disabled card behind the `notPermitted` banner. Read from `/status` on **every** load and
 *    never cached anywhere: the office can grant the permission later, and a user who has just
 *    been granted it must not have to re-pair anything to see the form come alive.
 * 2. **An uncertain register never offers a retry.** `POST /register` answering with
 *    `uncertain: true`, *and* the request failing in transport, are the same situation: a write
 *    to a real building's access control whose outcome is unknown. A retry is what turns one
 *    uncertain registration into two real ones, so `classifyRegister` reports `retryable: false`
 *    for both and the page renders no retry affordance at all.
 * 3. **The 취소 button keys on `is_active`, never on the row's presence.** `DELETE
 *    /invitations/{seq}` does not remove the row (verified live — `docs/PROBE.md`); it flips
 *    `inot_status` to `CANCEL` and the row stays, keeping its `invt_seq`. A table waiting for
 *    the row to vanish would report a working 취소 as broken.
 * 4. **KST is the sole date authority.** Every date bound comes from `/status`
 *    (`today_kst`, `max_date`); `new Date()` is never consulted. An `<input type="date">` value
 *    is a bare `yyyy-mm-dd` wall-clock string with no timezone attached, so feeding it KST
 *    values makes this page agree with the server for a user in any timezone.
 *
 * Pagination here is **display-only**. `client.history()` hands back the whole window already
 * assembled — `page_size: 100` covers it in one request in the normal case, and the client pages
 * on `totalCnt` when it does not — so `paginate()` slices an array that is already complete.
 * And `/history` returns no `total` aggregate (`resultData.total` came back `[]` even on a
 * 43-row range), so there is no summary row to render.
 *
 * **Row order is the handler's, not this module's.** `/history` answers newest visit first
 * (`api._newest_first`), so `paginate()` slicing in received order puts the upcoming visits on
 * page 1. Do not sort again here: a second ordering would be a second thing to keep in step
 * with the widget, which reads the same handler.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.IparkingForm = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /**
   * Page strings, ko + en.
   *
   * They live in this module rather than inline in `index.html` for the same reason the logic
   * does: the widget needs the register/history/cancel vocabulary too, and two copies of
   * `notPermitted` is one copy that will drift.
   *
   * `cleartextNotice`, `notPermitted`, `alreadyRegistered` and `registerUncertain` are
   * **verbatim copies** of the matching keys in `locales/{ko,en}.json`, and `plateHint` is the
   * example list out of `bad_plate`. They are duplicated because a settings webview cannot read
   * those files and this app adds no endpoint to serve them; `tests/test_form_js.py` asserts the
   * copies still match, so the duplication is checked rather than merely intended.
   */
  var STR = {
    ko: {
      pageTitle: "아이파킹 방문차량",
      noticeHeading: "먼저 알아두세요",
      cleartextNotice: "아이파킹 MEMBERS의 비공식 클라이언트입니다. 로그인 자체는 검증된 HTTPS로 이루어지지만, 아이파킹 API 서버가 수신하는 모든 HTTPS 요청을 평문 HTTP로 되돌립니다 — 이는 벤더 서버의 동작이며 이 앱에서 고칠 수 없습니다. 그 결과 로그인 이후의 접근 토큰과 차량번호·방문일 데이터는 암호화되지 않은 상태로 네트워크를 오갑니다. 여기서 방문 차량을 등록하면 이 단지의 실제 출입통제 시스템에 반영됩니다.",

      accountTitle: "아이파킹 계정",
      accountIntro: "아이파킹 MEMBERS 아이디와 비밀번호를 입력하세요. 자격증명은 이 Homey에만 저장되며, 아이파킹 서버 외의 어디로도 전송되지 않습니다.",
      id: "아이디",
      pw: "비밀번호",
      save: "로그인 & 저장",
      test: "연결 확인",
      clear: "계정 삭제",
      saving: "로그인 중…",
      saved: "로그인되었습니다.",
      savedLots: "주차장 {lots}곳을 찾았습니다.",
      statusSaved: "✓ 로그인됨 · 자격증명이 이 Homey에 저장되어 있습니다.",
      cleared: "계정을 삭제했습니다.",
      need: "아이디와 비밀번호를 모두 입력하세요.",
      testing: "연결 확인 중…",
      testOk: "✓ 연결 정상 · 주차장 {lots}곳.",
      notConfigured: "먼저 아이파킹 계정을 저장하세요.",

      registerTitle: "방문차량 등록",
      registerIntro: "등록하면 이 단지의 실제 출입통제 시스템에 즉시 반영됩니다.",
      lot: "주차장",
      plate: "차량번호",
      plateHint: "예시) 12가1234, 임1234, 임123456, 외교123456",
      plateStripped: "공백을 제거해 {plate} 로 등록합니다.",
      visitDate: "방문 예정일",
      dateHint: "오늘부터 {days}일 이내 (한국 표준시 기준)",
      registerBtn: "등록",
      registering: "등록 중…",
      registerOk: "✓ 등록되었습니다 · {plate} · 방문 예정일 {date}",
      toastRegistered: "{plate} 차량이 오늘 방문 등록되었습니다.",
      toastRegisteredOn: "{plate} 차량이 {date} 방문 등록되었습니다.",
      dateAmbiguous: "입력하신 날짜를 {date} 로 해석했습니다. 의도한 날짜가 아니면 취소하고 다시 등록하세요.",
      alreadyRegistered: "이미 등록된 차량입니다. 아래 등록 내역 또는 아이파킹 사이트에서 확인하세요.",
      uncertainHeading: "등록 결과를 확인할 수 없습니다",
      registerUncertain: "등록 결과를 확인할 수 없습니다 — 성공했을 수도, 실패했을 수도 있습니다. 다시 시도하지 마세요. 아무 조치도 하기 전에 아이파킹 웹사이트의 방문차량 등록 내역에서 이 차량번호가 있는지부터 확인하세요.",
      notPermitted: "이 계정에는 방문차량 등록 권한이 없습니다. 관리사무소에 문의하세요.",
      permissionUnknown: "등록 권한을 확인하지 못했습니다. 계정을 저장한 뒤 연결을 확인하세요.",
      selectLot: "주차장을 선택하세요.",
      noLots: "이 계정에서 주차장을 찾지 못했습니다.",

      historyTitle: "등록 내역",
      historyIntro:
        "지난 3개월과 앞으로 3개월치를 한 번에 불러옵니다. 앞으로 방문할 차량이 위에 옵니다. " +
        "취소는 이 표에서 바로 할 수 있습니다.",
      colDate: "방문예정일",
      colPlate: "차량번호",
      colStatus: "상태",
      colLot: "주차장",
      refreshBtn: "새로고침",
      loading: "불러오는 중…",
      historyEmpty: "등록 내역이 없습니다.",
      cancelBtn: "취소",
      cancelling: "취소 중…",
      cancelOk: "취소했습니다.",
      cancelConfirm: "이 등록을 취소할까요? 출입통제 시스템에서 즉시 해제됩니다.",
      // Shown *on the button itself* as a second step. `window.confirm` is not usable here —
      // see the note in index.html's `onCancel`.
      cancelArm: "정말 취소?",
      statusRESERVE: "예약",
      statusIN: "입차",
      statusOUT: "출차",
      statusCANCEL: "취소됨",
      pageOf: "{page} / {pages} 페이지 (총 {total}건)",
      prev: "이전",
      next: "다음",

      unknownError: "알 수 없는 오류가 발생했습니다.",
      moduleMissing: "설정 화면을 불러오지 못했습니다 (form.js). 앱을 다시 설치해 보세요."
    },
    en: {
      pageTitle: "iParking Visitor Parking",
      noticeHeading: "Before you start",
      cleartextNotice: "Unofficial client for iParking MEMBERS. Sign-in itself goes over verified HTTPS, but iParking's own API server downgrades every HTTPS request it receives to plain HTTP — that is the vendor server's behavior and cannot be fixed from this app. As a result, the access token and your plate/visit data travel over the network unencrypted after sign-in. Registering a visitor vehicle here acts on this building's real access-control system.",

      accountTitle: "iParking account",
      accountIntro: "Enter your iParking MEMBERS ID and password. Credentials are stored on this Homey only and are never sent anywhere but iParking's own servers.",
      id: "Account ID",
      pw: "Password",
      save: "Sign in & save",
      test: "Test connection",
      clear: "Remove account",
      saving: "Signing in…",
      saved: "Signed in.",
      savedLots: "Found {lots} parking lot(s).",
      statusSaved: "✓ Signed in · credentials are stored on this Homey.",
      cleared: "Account removed.",
      need: "Enter both an account ID and a password.",
      testing: "Checking the connection…",
      testOk: "✓ Connection OK · {lots} parking lot(s).",
      notConfigured: "Save your iParking account first.",

      registerTitle: "Register a visitor vehicle",
      registerIntro: "Registering here takes effect immediately on this building's real access-control system.",
      lot: "Parking lot",
      plate: "Plate number",
      plateHint: "e.g. 12가1234, 임1234, 임123456, 외교123456",
      plateStripped: "Whitespace removed — registering as {plate}.",
      visitDate: "Visit date",
      dateHint: "Within {days} days from today (Korea Standard Time)",
      registerBtn: "Register",
      registering: "Registering…",
      registerOk: "✓ Registered · {plate} · visit date {date}",
      toastRegistered: "{plate} is registered to visit today.",
      toastRegisteredOn: "{plate} is registered to visit on {date}.",
      dateAmbiguous: "Your date was read as {date}. If that is not the day you meant, cancel it and register again.",
      alreadyRegistered: "This vehicle is already registered. Check the history table below or the iParking site to confirm.",
      uncertainHeading: "The registration outcome is unconfirmed",
      registerUncertain: "The registration outcome could not be confirmed — it may have succeeded or it may not have. Do not try again. Open the iParking website's visitor-vehicle history and check whether this plate is listed before doing anything else.",
      notPermitted: "This account is not permitted to register visitor vehicles. Contact your building office.",
      permissionUnknown: "Could not determine whether this account may register vehicles. Save the account, then test the connection.",
      selectLot: "Select a parking lot.",
      noLots: "No parking lots were found on this account.",

      historyTitle: "Registration history",
      historyIntro:
        "Three months back and three months ahead, fetched in one request. Upcoming visits " +
        "come first. Cancel straight from this table.",
      colDate: "Visit date",
      colPlate: "Plate",
      colStatus: "Status",
      colLot: "Parking lot",
      refreshBtn: "Refresh",
      loading: "Loading…",
      historyEmpty: "No registrations to show.",
      cancelBtn: "Cancel",
      cancelling: "Cancelling…",
      cancelOk: "Cancelled.",
      cancelConfirm: "Cancel this registration? The access grant is withdrawn immediately.",
      cancelArm: "Really cancel?",
      statusRESERVE: "Reserved",
      statusIN: "Entered",
      statusOUT: "Exited",
      statusCANCEL: "Cancelled",
      pageOf: "Page {page} of {pages} ({total} total)",
      prev: "Prev",
      next: "Next",

      unknownError: "An unknown error occurred.",
      moduleMissing: "The settings page failed to load (form.js). Try reinstalling the app."
    }
  };

  /** Rows shown per page. Display-only — the whole window is already in memory. */
  var PAGE_SIZE = 20;

  /** How long to wait on each endpoint, in ms. See `TIMEOUTS.register` for why it is not one number. */
  var TIMEOUTS = {
    "default": 20000,
    // `client.register()` runs a 20 s write budget and then, on any error or timeout, a
    // **fresh** 25 s recovery re-query. A 20 s ceiling here would abandon the request while the
    // recovery that determines the real outcome is still running — turning a knowable result
    // into `uncertain` for no reason at all.
    register: 60000,
    cancel: 30000
  };

  // --- language + strings -----------------------------------------------------

  /** `"ko-KR"`, `"ko"`, `"en"`, anything → a key `STR` actually has. Defaults to `"ko"`. */
  function pickLanguage(raw) {
    if (typeof raw !== "string" || !raw) return "ko";
    var short = raw.slice(0, 2).toLowerCase();
    return Object.prototype.hasOwnProperty.call(STR, short) ? short : "ko";
  }

  /** Look up `key`, falling back through English to the key itself. */
  function t(lang, key, params) {
    var table = STR[pickLanguage(lang)] || STR.ko;
    var text = table[key];
    if (typeof text !== "string") text = STR.en[key];
    if (typeof text !== "string") text = key;
    return params ? fmt(text, params) : text;
  }

  /** `"{plate} on {date}"` + `{plate: "…"}`. Unknown placeholders are left alone. */
  function fmt(text, params) {
    if (!params) return String(text);
    return String(text).replace(/\{(\w+)\}/g, function (whole, name) {
      return Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : whole;
    });
  }

  /** `inot_status` → a label. Unknown codes render verbatim rather than as an empty cell. */
  function statusLabel(lang, status) {
    var code = String(status || "").toUpperCase();
    var key = "status" + code;
    var table = STR[pickLanguage(lang)] || STR.ko;
    return typeof table[key] === "string" ? table[key] : (status || "");
  }

  // --- plate normalization (requirement 7) ------------------------------------

  /**
   * Every character `iparking_lib/iparking/plate.py` strips, as one class.
   *
   * JavaScript's `\s` and Python's `str.isspace()` are *not* the same set, which is the whole
   * reason this is spelled out instead of being `/\s/g`:
   *
   * * `\s` misses U+0085 NEL and U+001C–U+001F (the information separators), all of which
   *   `isspace()` includes — so they are added here.
   * * `\s` already covers U+00A0 NBSP and U+3000 IDEOGRAPHIC SPACE (the one a Korean IME
   *   produces in full-width mode), as does `isspace()`.
   * * The zero-width `Cf` characters are in neither definition on the Python side and are
   *   listed explicitly there too: U+200B ZWSP, U+200C ZWNJ, U+200D ZWJ, U+FEFF BOM. They
   *   arrive by paste from web pages and messaging apps, and they are invisible in the input
   *   box — a plate carrying one looks exactly like a plate that is fine.
   *
   * `tests/test_form_js.py` runs both implementations over the same table and asserts they
   * agree, because "the two normalizers disagree" is a bug whose symptom is the input echoing
   * one plate while the server registers another.
   *
   * Spelled as escapes on purpose, exactly as `plate.py` spells its set: written
   * literally, half of this character class would be invisible in this source file too.
   */
  var STRIP_RE = /[\s\u0085\u001c-\u001f\u200b\u200c\u200d\ufeff]/g;

  /**
   * NFC-compose and strip — the mirror of `plate.strip_plate`. **Does not validate.**
   *
   * NFC first, and it matters: some Korean IMEs emit Hangul as conjoining jamo (`가` as
   * U+1100 U+1161 rather than U+AC00), which fails the `[가-힣]` class in the vendor's own
   * regex and gets rejected with nothing on screen to explain why. The real site has exactly
   * this bug.
   *
   * Validation stays on the Python side. The page normalizes only so the user *sees*
   * `12가 1234` become `12가1234` on blur — the vendor's site silently rejects the spaced form
   * without ever saying that a space is the problem.
   */
  function normalizePlate(value) {
    var text = value === null || value === undefined ? "" : String(value);
    return text.normalize("NFC").replace(STRIP_RE, "");
  }

  // --- error shapes -----------------------------------------------------------

  /**
   * A human sentence out of whatever a rejection turned out to be.
   *
   * Ported from `com.lomohome.localthings/settings/index.html`, where `String(err)` printing
   * `"[object Object]"` was a real defect: `homey.js` rejects with the value the Python side
   * sent, verbatim, and Homey's own API rejects with structured objects, so `err.message` is
   * frequently absent. Walk the shapes that actually occur, then fall back to JSON, which is at
   * least diagnosable.
   */
  function describe(err, lang, depth) {
    if (err === null || err === undefined) return t(lang, "unknownError");
    if (typeof err === "string") return err;
    if (typeof err !== "object") return String(err);
    if ((depth || 0) > 4) return t(lang, "unknownError");
    var keys = ["message", "error", "description", "reason", "detail", "statusText"];
    for (var i = 0; i < keys.length; i++) {
      var value = err[keys[i]];
      if (typeof value === "string" && value) return value;
      if (value && typeof value === "object") return describe(value, lang, (depth || 0) + 1);
    }
    if (Object.prototype.toString.call(err) === "[object Error]") {
      var text = String(err);
      if (text && text !== "[object Object]") return text;
    }
    try {
      var json = JSON.stringify(err);
      if (json && json !== "{}" && json !== "null" && json !== "[]") return json;
    } catch (e) { /* circular structure */ }
    return t(lang, "unknownError");
  }

  /**
   * The sentence to show for a handler that answered `ok: false`.
   *
   * `message` before `error` on purpose: `api.py` renders `message` from the locale key in the
   * **viewer's** language, while `error` is the specific Korean sentence the exception raised.
   * Preferring `message` is what keeps an English-speaking user from being handed Korean; the
   * fallback keeps a failure that carried no key from rendering as nothing at all.
   */
  function messageOf(res, lang) {
    if (res && typeof res.message === "string" && res.message) return res.message;
    if (res && typeof res.error === "string" && res.error) return res.error;
    return t(lang, "unknownError");
  }

  // --- verdicts ---------------------------------------------------------------

  /**
   * `/status`'s `can_register` (and the selected lot's, which can differ) → one of
   * `"allowed"` / `"denied"` / `"unknown"`.
   *
   * Three states, not two, because `null` and `false` mean different things and the page has to
   * render them differently: `"unknown"` is usually just "no account saved yet" and gets a
   * neutral hint, while `"denied"` is the building office refusing and gets the disabled card
   * behind the `notPermitted` banner. Collapsing them would tell a user with no account
   * configured to go talk to their building office.
   *
   * The lot's own flag wins when it is a boolean: an account can hold several stores with the
   * permission set differently on each, so the account-level summary is the coarser answer.
   */
  function registerPermission(accountFlag, lotFlag) {
    if (lotFlag === false || lotFlag === true) return lotFlag ? "allowed" : "denied";
    if (accountFlag === false || accountFlag === true) return accountFlag ? "allowed" : "denied";
    return "unknown";
  }

  /**
   * A `POST /register` response → `{state, retryable, outcome}` where `state` is one of
   * `"ok"` / `"already"` / `"uncertain"` / `"error"`.
   *
   * The ordering of these checks is the safety property:
   *
   * * **`uncertain` is tested first**, before `ok` and before `outcome`. It arrives on a
   *   response whose `ok` is `false`, and reading that as a plain failure is precisely how a
   *   user gets shown a retry button for a write that may already have registered their
   *   visitor. `retryable: false`, always.
   * * **`already_registered` is not an error.** `api.py` returns it `ok: true` with a distinct
   *   `outcome`, because re-entering a plate that is already registered is the single most
   *   likely real result of a first use. It is reported as its own state so the page can say so
   *   instead of claiming the app is broken. Not retryable either — retrying would just say the
   *   same thing again.
   */
  function classifyRegister(res) {
    if (!res || typeof res !== "object") {
      return { state: "error", retryable: true, outcome: "" };
    }
    if (res.uncertain === true) {
      return { state: "uncertain", retryable: false, outcome: "register_uncertain" };
    }
    var outcome = typeof res.outcome === "string" ? res.outcome : "";
    if (res.ok === true) {
      return {
        state: outcome === "already_registered" ? "already" : "ok",
        retryable: false,
        outcome: outcome || "ok"
      };
    }
    return { state: "error", retryable: true, outcome: outcome };
  }

  /**
   * The toast text for a register verdict, or `""` when this outcome must not get one.
   *
   * **Only `ok` earns a toast.** `already` and `uncertain` keep the distinct wording they
   * already have in the message area, and flattening either into a success toast would be a
   * lie with consequences: `already` means the vehicle was registered before this click, and
   * `uncertain` means nobody knows whether it is registered at all.
   *
   * **"오늘" is checked, not assumed.** The handler's `api_date` is compared against
   * `/status`'s `today_kst` — both server-side KST values, so `new Date()` stays uninvolved
   * (rule 4) — and a future visit date is named instead of being called today. The maintainer
   * asked for the 오늘 wording because a *tile press* is always today; this page can register
   * any date in the window, and a toast reading 오늘 for next Tuesday would be exactly the
   * silent wrong-day error the date echo exists to prevent.
   */
  function registerToast(verdict, status, lang) {
    if (!verdict || verdict.state !== "ok") return "";
    var res = verdict.response || {};
    var plate = res.car_number || "";
    if (!plate) return "";
    var apiDate = typeof res.api_date === "string" ? res.api_date : "";
    var today = String((status && status.today_kst) || "").replace(/-/g, "");
    if (apiDate && today && apiDate === today) {
      return t(lang, "toastRegistered", { plate: plate });
    }
    return t(lang, "toastRegisteredOn", { plate: plate, date: res.date || apiDate });
  }

  /**
   * May this history row be cancelled?
   *
   * `is_active` and nothing else. Cancelling does not delete the row — it flips `inot_status`
   * to `CANCEL` and the row stays in the list with its `invt_seq` intact (verified live). So
   * "the row is here" carries no information about whether it is a live access grant, and a
   * 취소 button rendered on presence would sit there offering to cancel an already-cancelled
   * registration.
   */
  function rowIsCancellable(row) {
    return !!(row && row.is_active === true && row.invt_seq);
  }

  /**
   * Slice `rows` for display. Purely local — see the module docstring on why there is no
   * network paging to do. `page` is 1-based and clamped, so a stale page number left over from
   * a longer list cannot render an empty table.
   */
  function paginate(rows, page, pageSize) {
    var all = Array.isArray(rows) ? rows : [];
    var size = pageSize > 0 ? pageSize : PAGE_SIZE;
    var pages = Math.max(1, Math.ceil(all.length / size));
    var current = Math.min(Math.max(parseInt(page, 10) || 1, 1), pages);
    var start = (current - 1) * size;
    return {
      rows: all.slice(start, start + size),
      page: current,
      pages: pages,
      total: all.length,
      size: size
    };
  }

  // --- dates ------------------------------------------------------------------

  /**
   * `<input type="date">` bounds, straight from `/status`. **`new Date()` is never called.**
   *
   * "Today" in this app means today at a parking lot in Korea, which is the only thing the
   * vendor's server accepts. `min` and the default `value` are both `today_kst`, and `max` is
   * `max_date` (KST today + `max_days_ahead`), so the input cannot submit a value
   * `resolve_visit_date` is guaranteed to reject.
   *
   * A missing field yields `""` rather than a browser-derived guess: an unbounded input is a
   * problem the user discovers at submit, while a *wrong* bound derived from their own timezone
   * is a problem they discover at a closed gate.
   */
  function dateBounds(status) {
    var s = status || {};
    var today = typeof s.today_kst === "string" ? s.today_kst : "";
    return {
      min: today,
      value: today,
      max: typeof s.max_date === "string" ? s.max_date : "",
      days: typeof s.max_days_ahead === "number" ? s.max_days_ahead : null
    };
  }

  // --- request shaping --------------------------------------------------------

  /**
   * `GET /history`'s path with its parameters in the query string.
   *
   * They go in the URL because `Homey.api("GET", path, body)` is not reliably given a body on
   * a GET — some builds drop it. `api._query()` reads several spellings for exactly this
   * reason, and `createController` also passes the same values as a body, so whichever half of
   * the contract this firmware honours, the handler sees the lot. Guessing wrong silently is
   * the failure worth this much redundancy: the table renders empty, which looks identical to
   * an account with no registrations.
   */
  function historyPath(lot, options) {
    var opts = options || {};
    var parts = [
      "park_seq=" + encodeURIComponent(String((lot && lot.park_seq) || "")),
      "stor_seq=" + encodeURIComponent(String((lot && lot.stor_seq) || ""))
    ];
    ["start_date", "end_date", "car_number"].forEach(function (name) {
      var value = opts[name];
      if (typeof value === "string" && value) {
        parts.push(name + "=" + encodeURIComponent(value));
      }
    });
    return "/history?" + parts.join("&");
  }

  function historyBody(lot, options) {
    var opts = options || {};
    var body = {
      park_seq: (lot && lot.park_seq) || 0,
      stor_seq: (lot && lot.stor_seq) || 0
    };
    ["start_date", "end_date", "car_number"].forEach(function (name) {
      if (typeof opts[name] === "string" && opts[name]) body[name] = opts[name];
    });
    return body;
  }

  // --- controller -------------------------------------------------------------

  /**
   * The stateful half: holds `/status`, the lot list, the history rows and the display page,
   * and performs the four actions. Still no DOM — `options.api` is the only thing it talks to.
   *
   * @param {object} options
   * @param {function} options.api `(method, path, body, timeoutMs) => Promise<object>`
   * @param {string}  [options.lang] initial language; `setLanguage` updates it later.
   */
  function createController(options) {
    var opts = options || {};
    var call = opts.api;
    var state = {
      lang: pickLanguage(opts.lang),
      status: null,
      lots: [],
      lotId: "",
      rows: [],
      page: 1
    };

    function request(method, path, body, timeout) {
      if (typeof call !== "function") {
        return Promise.reject(new Error("IparkingForm: no api transport supplied"));
      }
      return Promise.resolve(call(method, path, body, timeout));
    }

    function setLanguage(raw) {
      state.lang = pickLanguage(raw);
      return state.lang;
    }

    /** `/status`, on every load. Nothing here is cached between loads — rule 1. */
    function loadStatus() {
      return request("GET", "/status", {}, TIMEOUTS["default"]).then(function (res) {
        state.status = res || {};
        return state.status;
      });
    }

    /** Every lot on the account, across every authorization entry. Selects the first by default. */
    function loadLots() {
      return request("GET", "/lots", {}, TIMEOUTS["default"]).then(function (res) {
        state.lots = (res && Array.isArray(res.lots)) ? res.lots : [];
        if (!selectedLot() && state.lots.length) state.lotId = String(state.lots[0].lot_id);
        return res || { ok: false, lots: [] };
      });
    }

    function selectLot(lotId) {
      state.lotId = String(lotId === null || lotId === undefined ? "" : lotId);
      return selectedLot();
    }

    function selectedLot() {
      for (var i = 0; i < state.lots.length; i++) {
        if (String(state.lots[i].lot_id) === state.lotId) return state.lots[i];
      }
      return null;
    }

    /** `"allowed"` / `"denied"` / `"unknown"` for the lot currently selected. */
    function permission() {
      var lot = selectedLot();
      return registerPermission(
        state.status ? state.status.can_register : undefined,
        lot ? lot.can_register : undefined
      );
    }

    /** 등록 내역 for the selected lot. Resets to page 1 — the rows underneath just changed. */
    function loadHistory(historyOptions) {
      var lot = selectedLot();
      if (!lot) {
        return Promise.resolve({ ok: false, error: t(state.lang, "selectLot"), rows: [] });
      }
      return request(
        "GET",
        historyPath(lot, historyOptions),
        historyBody(lot, historyOptions),
        TIMEOUTS["default"]
      ).then(function (res) {
        state.rows = (res && Array.isArray(res.rows)) ? res.rows : [];
        state.page = 1;
        return res || { ok: false, rows: [] };
      });
    }

    /**
     * 방문차량 등록. **This writes to a real building's access-control system.**
     *
     * Resolves — never rejects — with `{state, retryable, message, response, rows}`, because
     * every outcome including a transport failure has to be rendered as a state rather than
     * thrown past the caller as something a `.catch()` might turn into "try again".
     *
     * Two things happen here that are not obvious:
     *
     * **A transport failure is `uncertain`, not `error`.** If the POST does not come back, the
     * request may well have reached the vendor and registered the vehicle — the same orphan the
     * Python side's recovery re-query exists to catch, one layer further out. Reporting it as a
     * failure would invite the retry that makes it two real registrations.
     *
     * **History is re-fetched on every non-error outcome, including `uncertain`.** For a success
     * that is the requirement (the table must not show stale rows immediately after the action
     * that changed them). For `uncertain` it is the single most useful thing available: it is a
     * read, it costs nothing, and it may well answer the question the message says to go to the
     * website for. The message stands either way — the refresh is evidence, not a verdict.
     */
    function register(input) {
      var form = input || {};
      var lot = selectedLot();
      if (!lot) {
        return Promise.resolve({
          state: "error",
          retryable: true,
          message: t(state.lang, "selectLot"),
          response: null
        });
      }
      var plate = normalizePlate(form.carNumber);
      var body = {
        car_number: plate,
        visit_date: String(form.visitDate || ""),
        park_seq: lot.park_seq,
        stor_seq: lot.stor_seq
      };
      if (form.memo) body.memo = String(form.memo);
      if (form.mobile) body.mobile = String(form.mobile);

      return request("POST", "/register", body, TIMEOUTS.register).then(function (res) {
        var verdict = classifyRegister(res);
        verdict.response = res || null;
        verdict.message = verdictMessage(verdict, res);
        return verdict;
      }, function (err) {
        return {
          state: "uncertain",
          retryable: false,
          outcome: "register_uncertain",
          response: null,
          message: t(state.lang, "registerUncertain"),
          detail: describe(err, state.lang)
        };
      }).then(function (verdict) {
        if (verdict.state === "error") return verdict;
        return loadHistory().then(function () {
          verdict.rows = state.rows;
          return verdict;
        }, function () {
          // A history refresh that fails must not downgrade the register verdict — the write
          // already happened (or didn't) and this is only the table catching up.
          verdict.rows = state.rows;
          return verdict;
        });
      });
    }

    function verdictMessage(verdict, res) {
      if (verdict.state === "ok") {
        return t(state.lang, "registerOk", {
          plate: (res && res.car_number) || "",
          date: (res && res.date) || (res && res.api_date) || ""
        });
      }
      if (verdict.state === "already") {
        return (res && res.message) || t(state.lang, "alreadyRegistered");
      }
      if (verdict.state === "uncertain") {
        return (res && res.message) || t(state.lang, "registerUncertain");
      }
      return messageOf(res, state.lang);
    }

    /** 취소 by `invt_seq`, then re-read the table — the row does not disappear, it flips. */
    function cancel(invtSeq) {
      return request("POST", "/cancel", { invt_seq: invtSeq }, TIMEOUTS.cancel)
        .then(function (res) {
          return loadHistory().then(function () { return res || { ok: false }; },
            function () { return res || { ok: false }; });
        });
    }

    function setPage(page) {
      state.page = page;
      return pageRows();
    }

    function pageRows() {
      var view = paginate(state.rows, state.page, PAGE_SIZE);
      state.page = view.page;
      return view;
    }

    return {
      state: state,
      setLanguage: setLanguage,
      language: function () { return state.lang; },
      loadStatus: loadStatus,
      loadLots: loadLots,
      selectLot: selectLot,
      selectedLot: selectedLot,
      permission: permission,
      loadHistory: loadHistory,
      register: register,
      cancel: cancel,
      pageRows: pageRows,
      setPage: setPage,
      dateBounds: function () { return dateBounds(state.status); },
      /** The success-toast text for a verdict, `""` for every other outcome. */
      toast: function (verdict) { return registerToast(verdict, state.status, state.lang); },
      t: function (key, params) { return t(state.lang, key, params); },
      statusLabel: function (status) { return statusLabel(state.lang, status); },
      describe: function (err) { return describe(err, state.lang); },
      messageOf: function (res) { return messageOf(res, state.lang); }
    };
  }

  return {
    STR: STR,
    PAGE_SIZE: PAGE_SIZE,
    TIMEOUTS: TIMEOUTS,
    STRIP_RE: STRIP_RE,
    pickLanguage: pickLanguage,
    t: t,
    fmt: fmt,
    statusLabel: statusLabel,
    normalizePlate: normalizePlate,
    describe: describe,
    messageOf: messageOf,
    registerPermission: registerPermission,
    classifyRegister: classifyRegister,
    registerToast: registerToast,
    rowIsCancellable: rowIsCancellable,
    paginate: paginate,
    dateBounds: dateBounds,
    historyPath: historyPath,
    historyBody: historyBody,
    createController: createController
  };
});
