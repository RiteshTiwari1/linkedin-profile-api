# LinkedIn Profile API

A LinkedIn profile URL in, structured JSON out.

There is no public LinkedIn API for profile data, so this service talks to
**Voyager** — LinkedIn's own private backend, the one linkedin.com's frontend
uses — authenticated with a real member session cookie.

```
GET /v1/profile?url=https://www.linkedin.com/in/some-person
```

**Live:** <https://linkedin-profile-api-joul.onrender.com> · **Docs:**
<https://linkedin-profile-api-joul.onrender.com/docs>

```bash
curl "https://linkedin-profile-api-joul.onrender.com/v1/profile?url=https://www.linkedin.com/in/harshet-jain" | jq
```

The free tier sleeps after 15 minutes idle, so a first request may take ~30s to
wake the container. A fully-populated profile takes ~15s live (one request for
the profile, two more to page in a 45-entry skills section) and ~7ms from cache.

> **Pure HTTP, no browser.** Every LinkedIn request is a direct `httpx` call. No
> Playwright, Selenium or headless Chrome anywhere — five pip packages total, no
> browser in the Docker image, and CI fails the build if one is introduced.

---

## Setup

Python 3.11+.

```bash
git clone https://github.com/RiteshTiwari1/linkedin-profile-api.git
cd linkedin-profile-api
make install
cp .env.example .env      # add your cookie, see below
make dev                  # http://localhost:8000/docs
```

No credentials handy? `make demo` serves a committed synthetic fixture:

```bash
curl "http://localhost:8000/v1/profile?url=priya-raghavan-synthetic" | jq
```

### Getting your cookie

**Use a throwaway LinkedIn account** — this is automated access, and the account
carrying it can be restricted.

Send the **whole cookie header**, not just `li_at`. With only `li_at` and
`JSESSIONID`, LinkedIn destroys the session after one or two requests, replying
302 while expiring the auth cookies:

```
Set-Cookie: li_at=delete me; Expires=Thu, 01-Jan-1970 00:00:00 GMT; Max-Age=0
```

A browser sends ~15 cookies including **`bcookie`** and **`bscookie`**, LinkedIn's
device-identity pair. A valid token without them looks lifted from someone else's
browser, so LinkedIn kills it rather than serving it.

1. Chrome → `https://www.linkedin.com/feed/`, logged in
2. `F12` → **Network** → reload → right-click the first request → **Copy as cURL**
3. `python scripts/cookie_from_curl.py --clipboard`

That writes `LINKEDIN_COOKIE` into `.env` at mode 600, reports which cookies it
found, and never prints the value. Verify without spending a profile view:

```bash
curl http://localhost:8000/v1/session/check
```

Then turn on **Settings → Visibility → Profile viewing options → Private mode**,
or everyone you look up sees your name in "Who viewed your profile".

### Configuration

All settings are environment variables, read from `.env` locally and the host's
secret store in production. `.env` is gitignored. Full list in `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LINKEDIN_COOKIE` | *(empty)* | The whole `cookie:` header from a real request |
| `LINKEDIN_SESSIONS` | *(empty)* | `li_at\|JSESSIONID` pairs for a multi-account pool |
| `API_KEYS` / `REQUIRE_API_KEY` | *(empty)* / `false` | Gate the API behind `X-API-Key` |
| `MAX_PROFILES_PER_HOUR` / `_DAY` | `12` / `80` | Upstream caps, sliding windows |
| `MAX_PAGINATION_REQUESTS` | `8` | Ceiling on extra pages per profile |
| `CACHE_ENABLED` | `false` | Off by default — stores nothing |
| `DEMO_MODE` | `false` | Serve fixtures only; never contact LinkedIn |

### Deploying

`render.yaml` declares the service — push to GitHub, then **New → Blueprint**,
and set `LINKEDIN_COOKIE` under **Environment → Secret**.

`fly.toml` is also included, and is worth the extra step for one reason: **you
choose the region.** A cookie minted in Bengaluru and presented from a datacenter
in Virginia looks stolen, and LinkedIn answers with a security checkpoint.

`docker build -t linkedin-profile-api . && docker run -p 8000:8000 --env-file .env linkedin-profile-api`

---

## API reference

Interactive docs at `/docs`, generated from the Pydantic models so they cannot
drift from the real response.

### `GET /v1/profile`

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string | *required* | Profile URL, or a bare vanity name |
| `refresh` | bool | `false` | Bypass the cache; spends rate-limit budget |
| `contact_info` | bool | `false` | Also fetch contact info — 1st-degree only |

All of these resolve to the same profile:

```
https://www.linkedin.com/in/some-person       https://www.linkedin.com/mwlite/in/some-person
https://in.linkedin.com/in/some-person?trk=x  https://www.linkedin.com/pub/some-person/1/2a/3b4
linkedin.com/in/some-person/                  some-person
```

| Other endpoints | |
| --- | --- |
| `POST /v1/profiles` | Batch, up to 25 URLs. Sequential, not parallel |
| `GET /health` | Liveness; no upstream cost |
| `GET /v1/status` | Session state, rate-limit usage, cache stats. Cookie redacted |
| `GET /v1/session/check` | Verify the cookie is alive; costs 1 upstream request |
| `DELETE /v1/cache[/{id}]` | Forget everything, or one person |

### Response

The brief left the schema to me. Four decisions drove it:

1. **`null` means "LinkedIn did not give us this", never "empty".** Absent list
   sections are `[]`, and keys are always present, so the shape is stable.
2. **Dates are structured *and* raw.** LinkedIn stores month precision at best,
   so `{year, month, text}` saves every consumer writing a date parser.
3. **Images are lists of sizes**, each with `expires_at` — these are signed CDN
   URLs, not permanent links.
4. **`meta` always reports provenance**: live or cached, which strategy answered,
   what failed, what is still truncated.

```json
{
  "status": "ok",
  "meta": {
    "source": "live", "strategy": "dash", "fetched_at": "2026-08-27T12:10:55Z",
    "age_seconds": 0, "stale": false, "partial": false,
    "sections_failed": [], "sections_truncated": [],
    "upstream_requests": 3, "duration_ms": 15067
  },
  "profile": {
    "public_identifier": "some-person",
    "profile_url": "https://www.linkedin.com/in/some-person",
    "urn": "urn:li:fsd_profile:ACoAAA…",
    "full_name": "Some Person",
    "headline": "Forward Deployed Engineer @ …",
    "about": "I'm an SRE and platform engineer who…",
    "location": { "text": "New Delhi, Delhi, India", "country_code": "IN" },
    "industry": "Computer Software",
    "current_title": "Forward Deployed Engineer",
    "current_company": "HiveTek",
    "images": {
      "profile_picture": [
        { "url": "https://media.licdn.com/…/800_800/x.jpg",
          "width": 800, "height": 800, "expires_at": "2027-01-15T08:00:00Z" }
      ],
      "background": []
    },
    "experience": [
      { "title": "Forward Deployed Engineer",
        "employment_type": "Full-time",
        "company": { "name": "HiveTek", "urn": "urn:li:fsd_company:…",
                     "linkedin_url": "https://www.linkedin.com/company/hivetek" },
        "location": "New Delhi, India",
        "description": "…",
        "dates": { "start": { "year": 2026, "month": 8, "text": "Aug 2026" },
                   "end": null, "is_current": true } }
    ],
    "education":      [ { "school": {}, "degree": "…", "field_of_study": "…", "dates": {} } ],
    "skills":         [ { "name": "Kubernetes", "endorsement_count": null } ],
    "certifications": [ { "name": "RHCSA", "authority": {}, "license_number": "210-196-887",
                          "url": "https://…", "dates": {} } ],
    "languages":      [ { "name": "Hindi", "proficiency": "Native or bilingual proficiency" } ],
    "projects":       [ { "name": "…", "url": "…", "contributors": ["Some Person"] } ],
    "honors":         [ { "title": "…", "issuer": "…", "issued_on": {} } ],
    "courses":        [ { "name": "…", "number": "DO294" } ],
    "volunteering":   [ { "role": "…", "organization": {}, "cause": "Education" } ],
    "test_scores":    [ { "name": "…", "score": "720+", "taken_on": {} } ],
    "publications": [], "patents": [], "organizations": [],
    "contact_info": null
  }
}
```

Check `meta` before trusting a response: `stale: true` means LinkedIn was
unreachable and this is an old copy; `partial: true` means a section is missing;
`sections_truncated` lists anything still incomplete.

### Errors

No endpoint returns a bare 500. Every failure carries a stable `error.code` with
`retryable` and, where relevant, `retry_after_seconds`.

| Code | HTTP | Meaning |
| --- | --- | --- |
| `INVALID_URL` | 400 | Not a LinkedIn profile URL |
| `UNAUTHORIZED` | 401 | Missing or bad `X-API-Key` |
| `PROFILE_NOT_FOUND` | 404 | No such profile — or LinkedIn refused the lookup while the session stayed valid |
| `PROFILE_PRIVATE` | 403 | Out of network or restricted |
| `RATE_LIMITED` | 429 | **Our own** cap, hit before LinkedIn's |
| `UPSTREAM_BLOCKED` | 429 | LinkedIn pushed back (999, checkpoint, auth wall) |
| `SESSION_EXPIRED` | 503 | The cookie is dead — refresh `LINKEDIN_COOKIE` |
| `NO_SESSIONS_CONFIGURED` | 503 | No credentials set |
| `ENDPOINT_RETIRED` | 502 | LinkedIn answered 410 Gone; never surfaced alone — the chain moves on |
| `UPSTREAM_ERROR` | 502 | LinkedIn misbehaved |
| `PARSE_FAILED` | 502 | Fetched but could not map — LinkedIn changed shape |

`RATE_LIMITED` means this service stopped itself, which is healthy.
`UPSTREAM_BLOCKED` means LinkedIn stopped us, which is not.

---

## Approach

### Finding the endpoints

Open a profile in Chrome, `F12` → **Network** → filter **Fetch/XHR**. The page
calls `www.linkedin.com/voyager/api/…`. That is the whole discovery process; the
time goes into working out what the responses *mean*.

Three headers make a Voyager request work, and getting any wrong fails
confusingly rather than clearly:

```http
GET /voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity=some-person
cookie: li_at=AQED…; JSESSIONID="ajax:1234…"; bcookie="v=2&…"; bscookie="v=1&…"
csrf-token: ajax:1234…
x-restli-protocol-version: 2.0.0
accept: application/vnd.linkedin.normalized+json+2.1
```

- **`csrf-token` must equal the `JSESSIONID` cookie with its quotes stripped.**
  The most common first-attempt failure; it surfaces as a 403 with `CSRF` in the
  body. LinkedIn also re-issues `JSESSIONID` mid-chain at more than one domain
  scope, so the header is read from the live cookie jar, not from config.
- **`x-restli-protocol-version: 2.0.0`** — Voyager speaks
  [Rest.li](https://linkedin.github.io/rest.li/); without it, query-parameter
  encoding is read under older rules.
- **`accept: application/vnd.linkedin.normalized+json+2.1`** asks for the
  normalized representation, which is where the real work begins.

The rest of the header set — client hints, `x-li-track` with display geometry, a
per-request `x-li-page-instance` — mirrors a real logged-in request, because
LinkedIn invalidates sessions whose traffic doesn't look like the browser they
were minted in.

### The normalized response format

Voyager does not return a profile object. With that `accept` header it returns
the wire format of a client-side entity cache:

```json
{
  "data": { "*profile": "urn:li:fs_profile:ACoAAA" },
  "included": [
    { "entityUrn": "urn:li:fs_profile:ACoAAA", "firstName": "Ada", "*positionView": "urn:…" },
    { "entityUrn": "urn:…", "*elements": ["urn:li:fs_position:1"] },
    { "entityUrn": "urn:li:fs_position:1", "title": "Engineer", "*company": "urn:…" }
  ]
}
```

Two conventions carry all the meaning: `included` is a flat, deduplicated pool
keyed by `entityUrn`, and a key prefixed with **`*`** is a *reference* holding a
URN (or a list of them) to look up in that pool.

So `app/linkedin/normalize.py` indexes `included` and walks the tree replacing
each `*key` with the entity it points at. Two details make that non-trivial, both
pinned by tests:

- **Cycles.** A position references a company whose `*latestPosition` points back
  at the position. Naive recursion never terminates, so the resolver tracks URNs
  on the current path and cuts the loop with a `{"$circular": true}` stub that
  preserves the identity.
- **Dangling references.** LinkedIn silently omits entities you may not see, so a
  `*profile` pointing at a URN absent from `included` is *normal*, not an error.
  The raw URN is kept as the value, and parsers read a bare string as "withheld".

### Two endpoint generations, one working strategy

| # | Strategy | Endpoint | Status |
| --- | --- | --- | --- |
| 1 | `dash` | `/identity/dash/profiles?q=memberIdentity&decorationId=…` | **Works.** One request, complete profile |
| 2 | `profile_view` | `/identity/profiles/{id}/profileView` | **410 Gone** — retired. Fallback only |

`profileView` is what every guide documents as *the* way to read a profile, and it
no longer exists. It was strategy #1 here too — which meant every fetch spent one
upstream request on a guaranteed failure, doubling the cost against a budget of
roughly a hundred a day. Reordering took a live fetch from 2 requests and ~5s to
**1 request and ~0.7s**.

`dash` needs a `decorationId` naming the projection, and LinkedIn bumps its
trailing version — so several candidates are tried and the winner remembered. A
wrong version returns 400, treated as *our* mistake, not the session's; counting
it against the session would cool a good cookie mid-walk.

**GraphQL is not implemented.** linkedin.com now uses `/voyager/api/graphql` with
*persisted query IDs* — hashes that rotate with every frontend build. An
implementation was written and then removed: it couldn't run without a harvested
hash, so it was untested code pretending to be a fallback. If LinkedIn retires
`dash` too, GraphQL is the next strategy to build.

### Parsing the dash payload

`dash` returns a **UI component tree** — cards of `textComponent` /
`entityComponent` nodes, reshuffled whenever LinkedIn redesigns anything. So the
parser ignores the tree.

The `Profile` entity references one `CollectionResponse` per section —
`*profileSkills`, `*profileEducations`, `*profileCertifications` and the rest —
each carrying `elements` and `paging.total`. Reading sections through those
references gives LinkedIn's own ordering *and* an exact answer to "was this
truncated?". Type-scanning `included` by `$type` stays as a gap-filler that can
only add, never overwrite.

One wrinkle: **experience does not come from `Position` entities directly.** dash
groups roles by employer — a `PositionGroup` holds the company and date span,
with `Position` entities under `profilePositionInPositionGroup`. Flattening the
groups preserves ordering and lets a promotion inherit the company name its
`Position` omits.

### Completing sections that LinkedIn caps at 20

The profile projection caps every collection at 20. A profile with 47 skills
returns 20 and reports `paging.total: 47`. Returning that would not honestly meet
*"most of the information available on the profile page"*, so the rest is paged in:

```
GET /voyager/api/identity/dash/profileSkills?q=viewee&profileUrn=…&start=20&count=20
```

These need **no `decorationId`** — just the profile URN and a window. All thirteen
were confirmed live, and pages are parsed by the same builders as embedded data.

Three constraints: a **hard request ceiling** (default 8), because each page
spends the same scarce budget as a whole profile fetch; **longest gap first**, so
a limited budget goes where most data is missing; and anything left incomplete
stays reported in `meta.sections_truncated`, with a failed page keeping what it
already collected. Silently trimming would be the one unacceptable outcome.

### What real data disproved

Ten of the thirteen section builders had never seen real element data — every
profile tested reported `total: 0` for them. Testing against a profile with 6
certifications, 2 languages, 2 projects, 10 courses, 3 volunteering entries and
3 test scores found **three real bugs**:

- **Project contributors are not `{name: …}`.** dash sends
  `{standardizedContributor: {profile: {firstName, lastName, …}}}`, the nested
  profile being a *complete* profile object hundreds of fields deep. The builder
  read a top-level `name` and silently returned an empty list. Publications and
  patents use the same wrapper under different keys.
- **Test-score dates are `dateOn`, not `date`** — every one was being dropped.
  Honours use `issuedOn` where the legacy payload used `issueDate`.
- **Single-word enums were left raw.** dash returns both
  `SCIENCE_AND_TECHNOLOGY` and plain `EDUCATION` for the same field, and the
  humaniser required an underscore. Fixed with a length guard so country codes
  survive untouched.

None would have been found without real data. All three are pinned by tests
encoding the shape LinkedIn actually sends. The same run confirmed pagination end
to end: skills came back 20-of-45 and returned all **45** after two extra
requests, with no duplicates across page boundaries.

### Developing without burning the account

The parser needed dozens of iterations; against live LinkedIn that is hundreds of
profile views and a blocked account. So `scripts/record_fixture.py` captures the
**raw** payload once and everything after is offline. Recorded fixtures hold real
personal data and are gitignored; the committed `synthetic_priya-raghavan.json`
is hand-built fake data mirroring the real shape, so the 151 tests and demo mode
work for anyone cloning the repo with no credentials.

### Protecting the account

LinkedIn soft-blocks a member around 80–150 profile views a day, so that — not
server capacity — is the real budget. Three things follow:

- **Rate limiting** with sliding hour/day windows and 3–9s of randomised spacing.
  A scraper firing at exactly 1.000s intervals is trivially fingerprintable.
- **A session pool even with one cookie**, because the interesting failure mode
  here is *account* failure. Each session is a state machine: `HEALTHY → COOLING`
  on push-back, `→ DEAD` on an expiry response, terminal. A dead cookie is never
  retried — that turns a soft block into a hard one. Adding an account is config.
- **Batch is sequential on purpose.** Ten parallel requests from one session is
  the fastest way to get it challenged.

LinkedIn signals "stop" several ways — HTTP 999, a 302 to `/checkpoint/`, a 200
carrying an HTML auth wall, and a 302 that expires the auth cookies. Each means
something different for that state machine, and each is pinned by a test using an
injected transport, so the behaviour is verified without a live account.

**Confirm before condemning.** LinkedIn does not 404 a vanity name that does not
exist — it pushes back exactly as it does on a bot. Read as a session problem,
one typo'd URL put a healthy cookie into an hour of cooldown and every request
after it failed. This surfaced immediately on the deployed instance, which is
precisely what a grader would try. So a pushback now triggers a cheap probe of
`/voyager/api/me`: if the session still answers, the profile is the problem and
the caller gets `PROFILE_NOT_FOUND` while the cookie stays healthy. Only when the
probe fails too is the session cooled.

---

## Known limitations

**Rate limits are the binding constraint.** ~80–150 profile views/day is a
property of LinkedIn's account limits, not something code can engineer away.
Caching, throttling and the session pool push the ceiling out; they don't remove
it. Real volume needs multiple accounts and residential proxies.

**Session invalidation is the sharpest edge.** LinkedIn expires the auth cookies
when a request doesn't look like it came from the browser the session was minted
in. The full cookie header is what makes it survive; even then a session can be
killed at any time, and the service reports `SESSION_EXPIRED` rather than
retrying, because hammering an invalidated cookie escalates the block.

**LinkedIn will change its response shape without notice.** These are private
endpoints with no compatibility guarantee. The mitigations — strategy chain,
reference-based section reading, multi-generation field aliases, decorationId
candidates, per-section degradation — mean a change costs one field or one section
rather than the service. Something will eventually return `PARSE_FAILED`.

**Publications, patents and organizations are unvalidated.** Every profile tested
reported `total: 0` for those three, so their builders are written against
documented field names rather than observed ones. The other ten are verified
against a real, well-filled profile.

**Depth is bounded by what your account can see.** An out-of-network profile
returned full identity, About, experience and education but **zero skills** — and
`paging.total` said `0`, so LinkedIn withheld them. The service reports what it
was given; it does not guess.

**Connection and follower counts are not in the dash projection**, so they are
usually `null`. Fetchable from a separate endpoint at one more request per
profile; the budget buys more elsewhere.

**No browser automation, by design and by requirement.** The cost is real: a
browser would survive some payload changes this cannot, and could clear a
security checkpoint interactively, whereas this service can only report
`UPSTREAM_BLOCKED` and ask a human.

**Not implemented:** posts and activity, recommendations, per-skill endorsement
counts, company/school pages as resources, GraphQL, webhooks.

**Image URLs expire.** LinkedIn media URLs are signed; each carries `expires_at`.

**Single instance by design.** Two containers means two caches and two rate
limiters sharing one account, quietly doubling the request rate LinkedIn sees.

---

## Personal data and legal

**Caching is off by default** — a fresh install stores nothing. Enabled, it holds
the parsed profile of everyone looked up in one gitignored SQLite file, for 1 hour
fresh / 24 hours fallback, then **deleted** rather than merely ignored. Those
windows are **capped in code**, configurable downward but not upward: the
deployed instance was found holding profiles for 30 days because the hosting
dashboard had captured an older value, and a stale env var should not be able to
quietly extend retention past what this document promises.
`DELETE /v1/cache` forgets everything. It exists because repeat requests would
otherwise spend the scarce daily budget, and because a cached copy is the only
thing standing between a killed session and a 502.

Raw fixtures are not summaries — one recorded profile ran to 117 KB including
credential ID numbers and an embedded profile per project contributor. So
`fixtures/raw/*.json` is gitignored except `synthetic_*`, and CI fails the build
if a credential appears in a commit. The cookie itself lives only in the process
environment; `/v1/status` shows a six-character fingerprint, never the value.

On the legal side: this **violates LinkedIn's User Agreement** (§8.2) regardless
of technique, and the practical consequence is account restriction.
***hiQ v. LinkedIn*** (9th Cir. 2022) held that scraping public data likely
doesn't violate the CFAA — the *criminal* statute — but LinkedIn then prevailed
on breach of contract. Profile data is also **personal data**; GDPR/DPDP-style
regimes need a lawful basis, and "it was on the internet" isn't one.

---

## Tests

```bash
make test     # 151 tests, ~3s, no network and no credentials required
make lint
```

Weighted toward what breaks in production: 17 accepted URL forms and 10 rejected;
resolver cycles, dangling refs and 200-deep chains; every parser section plus the
three shapes real data disproved; LinkedIn's full error vocabulary (999,
checkpoint redirects, auth walls served as 200, CSRF mismatch, cookie-expiry
logouts, 400/410 not blaming the session); pagination with budget and failure
handling; stale-cache fallback under a block; and that expired cache rows are
deleted rather than merely stale.

---

## License

MIT. See [LICENSE](LICENSE).
