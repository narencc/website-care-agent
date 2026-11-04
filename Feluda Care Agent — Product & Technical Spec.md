# Feluda Care Agent — Product & Technical Spec

Oct 30, 2026 · @Naren

## Summary

Feluda Care Agent turns a customer's website change request into atomic jobs, step-by-step builder instructions, and an AI-verified "done" — built entirely on Meta models for the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026).

**Problem.** The Website Care team maintains \~20,000 live SMB WordPress sites. Requests arrive vague ("fix the contact page"), builders interpret them differently, steps get missed, and customers are sometimes told "done" before the change is right. QA is manual and does not scale.

**Solution.** A pipeline with four stages:

1. **Understand** — parse the request (text, attachments, screenshots, voice notes) into atomic jobs tied to a page and element on the live site.
2. **Guide** — generate exact WordPress steps per job, so a junior builder gets it right first time.
3. **Verify** — capture before/after screenshots, segment the target element, judge the result against the request, and check nothing else broke.
4. **Close** — mark the customer request done only when every job passes; failures return to the builder with a specific reason.

**Hackathon fit.** "Build for Your World" theme, Indie track (solo submission by naren0207): a problem we live daily in website care, built to be judged on technical execution and whether it holds up in production.

## Goals, non-goals and constraints

The hackathon build proves one loop end to end — request in, verified done out — on real test sites, not breadth across every request type.

**Goals (hackathon scope)**

- Parse free-text requests and attachments into a structured job list with ≥90% of jobs correctly scoped on a test set of 30 real (anonymised) requests.
- Generate WordPress builder steps a junior builder can follow without asking a question.
- **Core demo:** verify text/phone/email updates and button/link changes at desktop and mobile widths on one known WordPress configuration.
- **Next priority:** image swaps and logo changes, once the core loop passes its evaluation gates.
- **Stretch goals:** menu changes and one contact-form submission with confirmed test-inbox delivery. Flow checks use existing test accounts and approved test sites only.
- Intake for the core demo is pasted text and screenshots; email/ticket integrations, voice intake, and replacement-image generation follow after the core loop works.
- Dashboard showing every request, its jobs, status and evidence (before/after crops).

**Non-goals**

- The agent does not edit customer sites. Builders make every change.
- No customer-facing UI in the hackathon; customer messaging is a drafted notification only.
- No payment or booking flow verification yet (phase 2).
- No non-WordPress platforms.

**Hard constraints (from existing Website Care decisions)**

- Customer is told "done" only after verification passes.
- Website URL is a required intake field.
- Builders need very detailed, step-by-step guidance.
- Test accounts for forms, payments and bookings already exist and must be used for flow checks.
- External AI vendors are acceptable for customer files.
- All runtime models are Meta models.

## Users and workflow

Three people touch a request — the customer, the builder and the Care lead — and the customer hears "done" only at the last step.

| User | Needs from the tool |
| --- | --- |
| Builder | Exact steps per job and the exact element to change; a clear reason when a job fails |
| Care lead | Queue of all requests, what is stuck, evidence for every "done" |
| Customer | A correct change and proof of it, without follow-up emails |

**End-to-end flow**

1. Customer request arrives by email, ticket or voice note; site URL is mandatory.
2. Planner fetches the site's sitemap and target pages, then splits the request into jobs with steps and acceptance criteria. A Care lead approves a requirement-to-job coverage map before any job becomes ready; unmapped requirements block work.
3. Capture before-evidence at desktop and mobile widths. Locate targets through DOM text, accessible names and attributes first, with SAM as a visual supplement. Unresolved or ambiguous targets go to a human before work starts.
4. Builder reviews (and may correct) each job, makes the change in WordPress, and clicks "Mark done".
5. Verifier captures after-evidence and runs checks; a fail goes back to the builder with the reason and loops to step 4.
6. When every current job revision passes, run a final consistency check across all affected pages and shared components. Close only if the evidence is current and no required check is unresolved; then draft the customer message with after-crops.

## System architecture

The agent plans the work and a human builder makes the change. Verification is read-only except for explicitly enabled contact-form tests on approved test sites.

```text
Intake -> Request + attachments -> Planner -> Builder review -> Builder changes site
                                     |                              |
                              Site profile + recipes           Mark done
                                                                    |
FastAPI -> durable task queue -> Worker: capture -> checks -> visual judge
                                   |                          |
                           Postgres + object storage    pass / fail / needs human
                                                              |
                                              Final consistency gate -> Draft done note
```

FastAPI serves the dashboard and enqueues work. A background worker handles browser captures, model calls, retries and optional inbox polling. Postgres stores durable task status; object storage holds immutable evidence. A Postgres-backed queue is sufficient for the prototype; do not run long verification work inside an HTTP request.

Each task has an idempotency key, timeout, bounded retry policy and recorded outcome. Each builder submission has a stable `submission_id`, tied to the job revision and the builder's assertion that the site is ready. Duplicate clicks and HTTP retries reuse that ID and return the same logical verification attempt. After a failed attempt and a site correction, a new submission creates a new attempt even when the job revision is unchanged. Infrastructure retries resume the existing attempt through distinct task executions. Enforce unique submission and task keys in Postgres; only the current submission may commit a verdict. Optional form submissions have a separately persisted operation ID and receipt state; neither a worker retry nor a new builder submission may blindly resubmit an unresolved operation. Infrastructure errors produce `verification_error`, with a retry action; they are not evidence that the builder made an incorrect change.

Suggested stack: Python (FastAPI) backend, Postgres for records, object storage for screenshots, Playwright for capture and flow checks, a simple React dashboard, and the Meta Model API for all model calls.

## Meta model choices

**Validation status:** the model names, capabilities, availability, pricing and hackathon requirements below are planning assumptions until checked against current official sources and direct API probes. Record the source, date, accessible model ID and probe result before depending on each capability. A successful Wonder text request does not establish Spark vision or SAM support.

Muse Spark 1.3 is the backbone of the runtime, SAM 3.1 is the eyes, and Muse Wonder is reserved for hard cases; for development, use Muse Code with Muse Spark 1.3 day to day and Muse Wonder for planning and hard debugging.

### Runtime (inside the tool)

| Job in the pipeline | Model | Why this one | Price (from Meta's model page) |
| --- | --- | --- | --- |
| Request parsing and job planning | Muse Spark 1.3 | Native tool calling and MCP, 1M context fits the request plus a site map and page HTML | $1.25 in / $4.25 out |
| Builder step generation | Muse Spark 1.3 | Same context as parsing; long, precise structured output | $1.25 in / $4.25 out |
| Element location in screenshots | SAM 3.1 | Text-prompted detect and segment ("phone number in header", "site logo") gives exact before/after crops | $2.50 per 1,000 images |
| Visual judge (did the change match the request?) | Muse Spark 1.3; fallback Muse Spark 1.1 | Compares crops and full pages against the job spec; 1.1 is explicitly documented for multimodal perception | $1.25 in / $4.25 out |
| Flow check (submit contact form with test account) | Muse Spark 1.3 with browser use | Improved browser and computer use across harnesses | $1.25 in / $4.25 out |
| Escalation (ambiguous request, repeated verify failure) | Muse Wonder | Frontier reasoning, long tasks, recovers from errors; used on a small share of jobs only | $4.00 in / $20.00 out |
| Voice-note and call requests | Muse Voice Transcribe | Speech to text with speaker separation | $0.18 per hour |
| Replacement image drafts (optional) | Muse Image 1.0 | Generate or edit an image when a customer asks for new visuals, with accurate text | $0.01 per image |
| Local first-pass triage (phase 2) | Muse Glimmer | 30B, Apache 2.0, single GPU; cheap high-volume classification without sending data out | Self-hosted |

**Not used at runtime:** the Contributor tiers (Muse Spark 1.3 / 1.2 Contributor). Their inputs and outputs train Meta's models, so customer requests and screenshots must never go to them. Muse Spark 1.2 is superseded by 1.3. Muse Spark Open is a phase-2 option to self-host the backbone at 20,000-site scale.

### Development (building the tool)

| Use | Model | Notes |
| --- | --- | --- |
| Daily coding, tests, refactors | Muse Code agent on Muse Spark 1.3 | Long-horizon coding, fewer tokens than 1.2 |
| Architecture, planning, hard debugging | Muse Wonder | Use sparingly; it is 3–5× the cost of Spark 1.3 |
| Cheap bulk dev work on synthetic data | Muse Spark 1.3 Contributor | \~95% cheaper, but only for non-customer code and fake data; check it is available in India |
| Prompt tuning for SAM and the judge | Meta playground (SAM 3.1, Spark 1.3) | Iterate on real screenshots before coding |

**To verify on day 1:** that Muse Spark 1.3 accepts image input via the API (if not, the judge uses Muse Spark 1.1), and that the listed prices are per 1M tokens.

## Data model and job schema

Four primary records carry the pipeline: Request, Job, Evidence and Verdict. Model outputs are schema-validated proposals; application code validates permissions and state transitions before writing records. Supporting SiteProfile, Submission, Task and ClosureRun records hold editing knowledge, builder submissions, background execution state and final consistency evidence.

| Record | Key fields | Written by |
| --- | --- | --- |
| Request | id, site\_url (required), customer, channel (email / ticket / voice), raw\_text, attachments\[\], received\_at, requirements[], scope_revision, coverage_approval, status | Intake + Care lead review |
| Job | id, request_id, revision, type, scope, targets[], exclusions[], depends_on[], site_profile_id, requirement_ids[], recipe_snapshot, builder_steps[], acceptance_checks[], current_submission_id, status, attempts | Planner proposal + builder review |
| Evidence | id, job_id, job_revision, submission_id, attempt_id, closure_run_id (if final), phase, requested_url, final_url, viewport, captured_at, page_screenshot, element_crop, optional element_mask, dom_snippet, content_hash | Capture + optional SAM |
| Verdict | id, job_id, job_revision, submission_id, attempt_id, evidence_ids[], check_results[], result (pass / fail / needs_human), reasons[], regressions[], model, prompt_version, confidence | Checks + judge, committed by application |

Baseline Evidence is captured before any builder submission, so its `submission_id` and verification `attempt_id` are null and its capture task is recorded separately. After-evidence must identify its submission and attempt; final evidence identifies its ClosureRun.

**Supporting execution records:** Submission stores `id`, `job_id`, `job_revision`, `submitted_by`, `submitted_at` and `attempt_id`. Task stores its attempt, execution count, idempotency key, timeout and retry outcome. ClosureRun stores the request scope revision, job revisions and submission IDs, site lease token, capture window, normalized content fingerprints, check results and outcome.

**Request completeness:** assign stable IDs to every customer requirement, with source text or attachment references. The coverage map links each requirement to one or more jobs, an unresolved clarification, or an explicitly reviewed exclusion with reason. The Care lead must review the original request and attachments as well as the planner output; approval records the author, time, scope revision and mapped job revisions. Any unresolved requirement blocks readiness and closure. Changing requirements, mappings, exclusions or referenced job revisions invalidates approval. Before closure, application code rechecks coverage, current approval and all mapped required jobs; passing the jobs that happen to exist is insufficient.

**Job types:** core `text_update` and `link_button_change`; subsequent `image_swap` and `logo_change`; stretch `menu_change` and `form_check`. Unsupported types go to a human with the parsed draft and cannot be auto-passed.

**Atomic scope:** one job covers one independently verifiable change to one target or one shared component. `scope` is `element`, `page`, `shared_component` or `page_set`. Each target records its page URL, locator candidates, description, expected values and supported viewports. A site-wide update is a parent work item with explicit child jobs for header, footer and page-body occurrences. Preserve the customer's requested scope; the planner must not silently expand it.

For a site-wide claim, save the crawl manifest, same-origin boundary, page limit and excluded routes. An incomplete crawl cannot support “old value absent everywhere”; it requires human review or a narrower, explicitly reviewed scope. Known exclusions remain visible in the builder card and evidence report.

**SiteProfile:** record the approved theme, editor, relevant plugins, verified editing recipes and validation date for each demo site. Builder steps must include an immutable snapshot of a versioned recipe from this profile, with its ID, version, validation date, source hash and full numbered instructions. Render the complete snapshot in the builder card. Instructions must identify the exact editor path and control labels, values to enter, publish action, applicable cache steps and observable completion signals; a recipe reference alone is insufficient. Missing instructions, unresolved placeholders, an unvalidated recipe, or missing validation date/source hash block `ready`. Generated builder steps must match the approved snapshot after explicit value substitution; the model cannot invent additional navigation. Recipe updates do not silently change existing jobs; adopting one increments the job revision. If the editing path is unknown, ask for human review instead of inventing WordPress navigation. The agent does not need wp-admin access to use a profile maintained by the Care team.

Example job the planner must output (strict JSON, schema-validated). The recipe below illustrates a fixture with a block theme, a Header template part containing one phone link, and no WordPress cache plugin. Its navigation and controls must be validated on the selected demo site before this profile can be approved; it is not a claim about an existing site configuration.

```json
{
  "id": "job_header_phone",
  "revision": 1,
  "type": "text_update",
  "scope": "shared_component",
  "site_profile_id": "demo_site_verified",
  "depends_on": [],
  "exclusions": [],
  "targets": [
    {
      "id": "header_phone",
      "page_urls": [
        "https://example-dental.com/",
        "https://example-dental.com/contact"
      ],
      "target_description": "phone number in the shared site header",
      "locator_candidates": [
        {
          "phase": "before",
          "within_role": "banner",
          "role": "link",
          "name": "(555) 201-4410"
        },
        {
          "phase": "after",
          "within_role": "banner",
          "role": "link",
          "name": "(555) 201-9900"
        }
      ],
      "current_value": "(555) 201-4410",
      "desired_value": "(555) 201-9900",
      "viewports": [
        "desktop",
        "mobile"
      ],
      "structural_anchor": {
        "landmark_role": "banner",
        "element_role": "link",
        "baseline_match_count": 1,
        "context": "Phone link in the reviewed shared Header template part"
      }
    }
  ],
  "builder_steps": [
    {
      "number": 1,
      "instruction": "Open WordPress admin for the approved site. Select Appearance > Editor."
    },
    {
      "number": 2,
      "instruction": "Open Patterns > Manage all template parts > Header. Confirm the preview matches the reviewed before-crop; stop for review if the labels or header differ."
    },
    {
      "number": 3,
      "instruction": "Select the header Phone link block in List View. Replace its visible text with (555) 201-9900."
    },
    {
      "number": 4,
      "instruction": "Edit that block's link destination to tel:+15552019900 and apply the link change."
    },
    {
      "number": 5,
      "instruction": "Select Save, confirm only the Header template part is selected, and confirm Save again. Wait for the saved confirmation. This fixture has no WordPress cache plugin; any other cache configuration requires a separately validated recipe."
    },
    {
      "number": 6,
      "instruction": "Open the public home and contact pages at the supported desktop and mobile widths. Confirm the header shows (555) 201-9900 and its link destination is tel:+15552019900, then return to the job card and select Mark done."
    }
  ],
  "acceptance_checks": [
    {
      "id": "phone_visible",
      "kind": "visible_text",
      "target_id": "header_phone",
      "expected": "(555) 201-9900",
      "required": true
    },
    {
      "id": "phone_destination",
      "kind": "link_destination",
      "target_id": "header_phone",
      "expected": "tel:+15552019900",
      "required": true
    },
    {
      "id": "header_layout",
      "kind": "visual_layout",
      "target_id": "header_phone",
      "expected": "Header remains readable and usable at both supported viewports",
      "required": true
    }
  ],
  "requirement_ids": [
    "req_header_phone"
  ],
  "recipe_snapshot": {
    "recipe_id": "demo_site.header_phone.v1",
    "version": 1,
    "validation_status": "requires_site_validation",
    "validation_date": null,
    "source_hash": null,
    "steps": [
      "Open WordPress admin for the approved site. Select Appearance > Editor.",
      "Open Patterns > Manage all template parts > Header. Confirm the preview matches the reviewed before-crop; stop for review if the labels or header differ.",
      "Select the header Phone link block in List View. Replace its visible text with (555) 201-9900.",
      "Edit that block's link destination to tel:+15552019900 and apply the link change.",
      "Select Save, confirm only the Header template part is selected, and confirm Save again. Wait for the saved confirmation. This fixture has no WordPress cache plugin; any other cache configuration requires a separately validated recipe.",
      "Open the public home and contact pages at the supported desktop and mobile widths. Confirm the header shows (555) 201-9900 and its link destination is tel:+15552019900, then return to the job card and select Mark done."
    ]
  }
}
```

Acceptance checks are the contract. Each check has a stable ID, kind, target, expected value and required flag. Each result records `pass`, `fail`, `needs_human` or `error`, with evidence IDs and a builder-readable explanation. The example verifies only the shared header on its declared pages; a footer update requires a separate child job.

**State and revision rules:** `draft -> needs_review -> ready -> with_builder -> queued -> verifying -> passed | failed | needs_human | verification_error`. Reviewed drafts can advance to `ready`; `failed` returns to `with_builder`, and `verification_error` can retry through `queued`. Human review must resolve ambiguity or explicitly approve a corrected scope before retrying. No manual action may mark an unverified job passed.

Editing targets, steps or checks increments the revision and invalidates previous verdicts. Workers commit a verdict only if both its revision and submission are still current; superseded submissions cannot overwrite newer results. Correcting the site alone creates a new submission without changing the job revision. Before-evidence is captured after scope review and before editing; do not silently replace the original baseline during a retry. Dependent jobs wait for prerequisite completion. Serialize overlapping edits to shared components and invalidate affected passes when those components change. Request closure is an atomic application operation requiring current passes for every required job, completed dependencies, a current requirement-coverage approval, and a successful ClosureRun under the consistency protocol below. Reopening or revising a closed request invalidates its draft done message.

## Verification design

Verification combines complementary signals: deterministic DOM checks, visual evidence and a model judgement. They can share targeting or planning errors, so agreement is useful but does not prove independence.

1. **Before capture:** save reviewed targets at fixed desktop/mobile viewports, including DOM, screenshot, final URL, timestamps and capture settings. Use consistent cookie state, fonts, scroll position and animation settings.
2. **Locate:** bind the reviewed target to stable structural anchors such as its landmark, template/component identity and surrounding DOM context, alongside baseline text, accessible name, attributes and bounding box. Store phase-specific locator candidates for both old and expected values; changing a phone number or button label must not lose its identity. After editing, resolve within the same reviewed structure and require a unique visible match per page and viewport. Missing or multiple matches require review; never fall back to a same-text element elsewhere or use a crop alone as identity proof. Use SAM to supplement visual localization. Missing SAM output alone does not block a DOM-resolved target.
3. **After capture:** capture the same pages and viewports for the current job revision. Retry stale cached pages within a bounded window; cache-busting alone is not proof of freshness. Unresolved freshness is `needs_human`, never pass.
4. **Deterministic checks:** execute the checks appropriate to the change type and declared scope. Restrict absence searches to the reviewed crawl manifest and exclusions. Follow permitted redirects and validate intended destinations rather than relying solely on HTTP 200.
5. **Visual judge:** compare before/after crops and page context against structured check IDs. Return a result and evidence reference per visual check. Model-reported confidence is a routing signal to calibrate on the evaluation set, not a probability guarantee.
6. **Regression scan:** normalize captures and mask documented dynamic regions. Check changes outside the full expected affected region, including responsive reflow; a SAM mask alone is too narrow. Unexplained material changes go to review. Bound pages, image sizes, model calls and retries per attempt.
7. **Optional flow check:** on an approved test site, submit once with an existing test account and unique correlation ID. Confirm success feedback and receipt of the matching message through a designated test-inbox connector within a timeout. Never blindly retry a submission after an uncertain response; inspect receipt first. Missing inbox access or uncertain delivery cannot pass.
8. **Escalation:** two genuine builder-change failures or ambiguous visual results can escalate to Wonder. Infrastructure failures use task retries instead. Cap escalation and send unresolved cases to human QA.
9. **Final consistency gate:** after all required jobs pass and coverage is approved, run the site-level consistency protocol below. Recapture all affected pages/shared components and rerun relevant checks. Any mismatch invalidates the affected verdicts.

**Closure consistency protocol:** acquire a site-level lease shared across all requests for that site before the final scan. Participating builders must finish edits and acknowledge the edit freeze; the dashboard blocks new work and verification submissions on that site until the lease is released. Renew the lease while scanning and use a fencing token so an expired worker cannot close a request. This coordinates tool participants; it cannot lock WordPress or other editors.

For the prototype, all final captures and the closure commit must fit within a configurable five-minute window, with every final fingerprint recheck no more than 30 seconds before commit. Record the settings in ClosureRun. Capture normalized DOM/content fingerprints for every affected page and shared component at both supported viewports, rerun checks, then revisit the same targets and compare fingerprints before closure. Persist normalization and dynamic-mask versions; masks cannot hide acceptance targets. A changed fingerprint invalidates affected passes; an expired window, lost lease or unresolved capture blocks closure and requires a fresh scan. Close in a database transaction only if the lease token, scope approval, job revisions and submission IDs still match the run.

Sequential browser captures cannot establish a single atomic WordPress snapshot. An external edit can occur after the final read or between reads without being detected. Evidence and the done draft must state the verification interval and reviewed scope. If exclusive editing cannot be coordinated on the demo site, route closure to human review; do not claim continuous correctness or production-grade protection from external edits.

| Change type | Required verification |
| --- | --- |
| Text/phone/email | Expected visible value in scope; old-value absence only where required; matching `tel:`/`mailto:` target |
| Button/link | Label, visibility, normalized intended destination and supported interaction |
| Image/logo | Intended asset or approved visual match, visibility, dimensions and responsive rendering |
| Menu (stretch) | Label, order, destination and desktop/mobile navigation behavior |
| Contact form (stretch) | Single correlated submission, success feedback and matching test-inbox receipt |

**Pass rule:** all required deterministic and visual checks pass on the current revision, with current evidence and no unresolved material regression. `error` and `needs_human` block closure. Only genuine change failures return correction instructions to the builder; capture/API/inbox outages show a verification error and retry action.

## Dashboard and UX

One internal web app with three views; the builder view is the one that must be excellent.

| View | Who | What it shows |
| --- | --- | --- |
| Request queue | Care lead | All requests with site, age, job count, status (parsing / with builder / verifying / done / needs human) |
| Builder job card | Builder | Job, numbered steps, before-crop of the exact element, acceptance criteria, a "Mark done" button; after a fail, the failing criterion and after-crop side by side |
| Evidence report | Care lead, QA | Per job: before/after crops, verdict and reasons, regressions, model used; exportable as the proof sent with the "done" message |

**Customer message:** on full pass, the system drafts a short "done" note with the after-crops attached; a human sends it in the hackathon version.

**Edit before build:** builders can correct scope, targets and checks before starting; corrections are logged with author, time and revision. Show target pages, exclusions, editing recipe, per-check evidence and pending dependencies. Show the approved requirement-coverage map and full recipe snapshot. Reuse the current submission for duplicate clicks; after correction, allow a new submission on the same revision and display its attempt history. Show the site edit freeze and block participating builders from starting edits during final verification. Disable duplicate verification submissions; distinguish “change failed”, “needs review” and “verification unavailable”. Display stale evidence explicitly and block the done draft until the final consistency gate passes.

## Security, privacy and cost

The provisional model-only estimate is about $0.20 per request. Treat it as a hypothesis until measured with the implemented capture and retry policy; it excludes browser workers, storage, database, network and inbox integration costs.

**Security and privacy**

- The agent never logs in to wp-admin. Browsing is read-only except for explicitly enabled form tests on approved test sites, with designated recipients, identifiable payloads, duplicate protection and a cleanup procedure.
- Validate intake URLs and all browser/tool navigation. Allow approved HTTP(S) destinations only; block loopback, private/link-local addresses, cloud metadata endpoints and unsafe redirects, including DNS changes at connection time. Apply network restrictions to browser subrequests as well as top-level pages.
- Treat website content, attachments and customer text as untrusted data, never as authority to change tool permissions, read secrets or visit arbitrary URLs. Enforce tool boundaries in application code.
- Enforce customer/site ownership checks on records, tasks and evidence; use short-lived signed evidence links and customer-scoped storage paths. Redact credentials and unnecessary personal data from logs and model payloads. Restrict attachment formats and sizes; never execute uploaded content.
- Test-account credentials for flow checks live in a secrets store, injected only into the flow-check browser session.
- Customer data goes only to standard-tier Meta Model API endpoints, never Contributor tiers.
- Screenshots and evidence retained 90 days, then deleted (proposed; confirm with the Care lead).
- Log model-call metadata with request ID, job revision, model, prompt version, tokens, latency and cost. Keep credentials out of traces; restrict and redact any separately retained prompt/evidence payloads. Standard-tier selection does not replace review of provider retention and data-use terms.

**Cost estimate per request** (assumes 2 jobs per request, 10% escalation, and that listed prices are per 1M tokens)

| Step | Model | Assumed volume | Cost |
| --- | --- | --- | --- |
| Parse and plan | Muse Spark 1.3 | 30k in / 2k out | $0.05 |
| Builder steps | Muse Spark 1.3 | 20k in / 3k out | $0.04 |
| Locate elements | SAM 3.1 | 8 images | $0.02 |
| Visual judge | Muse Spark 1.3 | 2 × (15k in / 1k out) | $0.05 |
| Escalation | Muse Wonder | 10% × (50k in / 5k out) | $0.03 |
| **Total** |  |  | **≈ $0.19** |

At 1,000 requests a month the rounded baseline projects roughly $190 in model spend before retries and operational overhead. Measure actual image-token billing, desktop/mobile judge calls, final consistency scans, retries and escalations; report model-only and total operating cost separately. Record cost per attempt and request, with a configurable spending cap that routes unfinished work to review. Self-hosting savings require a separate utilization and infrastructure estimate.

## Build plan and demo

The hackathon started Oct 26, so six build days remain; the plan gets the core loop working by Nov 2 and leaves two days for testing and the demo.

| Date | Deliverable | Exit gate |
| --- | --- | --- |
| Oct 30 | Confirm official model IDs/capabilities/prices and event requirements; probe structured text, image input and SAM access. Select approved test sites, one WordPress profile, recipes and evaluation cases. | Working text + vision path, known editing recipe and supported demo scope; SAM fallback documented |
| Oct 31 | Implement request/job schemas, scope review, revisions, durable worker and baseline desktop/mobile captures. | Text request becomes coverage-approved jobs with located targets, validated recipe snapshots and traceable before-evidence |
| Nov 1 | Implement phone/text and link checks, visual judge, bounded retries and evidence storage. | Correct edit passes; wrong number, wrong link and missing mobile update fail; infrastructure error stays distinct |
| Nov 2 | Complete builder cards, queue, final consistency gate and drafted done report. | Full loop works; stale revisions/submissions, omitted requirements, duplicate clicks, lease expiry and shared-component changes block incorrect closure; same-revision correction can be reverified |
| Nov 3 | Run held-out evaluation, address false passes and capture instability; measure latency and cost. Add image/logo checks only if core gates pass. | Zero false passes across the supported held-out release suite; junior-builder recipe checks and raw metrics recorded; repeatable demo and fallback recording available |
| Nov 4 | Freeze scope, rehearse the three-minute demo, prepare submission and evidence. | Correct-change pass, planted-error fail and blocked closure demonstrated end to end |

**Priority rule:** do not delay the core loop for integrations, voice, image generation, menu verification or form automation. Contact-form delivery is attempted only after the core is stable and the test-inbox connector and approved submission policy are ready.

Day 1 must confirm the two unknowns from the model section (Spark 1.3 image input, pricing units) before any judge code is written.

**Demo script (3 minutes)**

1. Paste a real, vague customer request; show it split into jobs with steps and criteria.
2. Show reviewed header and footer jobs, grounded editing instructions, explicit criteria and DOM/SAM before-crops. The request explicitly asks to update both locations.
3. Make the change correctly on a test site; show the pass with before/after evidence.
4. Leave the footer unchanged while marking its job done; show the failed footer criterion and blocked request closure. Correct it, rerun verification and show the final consistency gate.
5. Close on the dashboard and the drafted done message.

## Success metrics, risks and open questions

The demo succeeds if the system catches a deliberately wrong builder change that a quick human glance would miss.

**Metrics (on the 30-request test set)**

| Metric | Target |
| --- | --- |
| Planner precision: correctly scoped predicted jobs / all predicted jobs | ≥ 90% |
| Planner recall: required jobs correctly captured / all ground-truth required jobs | ≥ 90% |
| Correct changes automatically passed / all known-correct test cases | ≥ 95% |
| Incorrect changes explicitly failed / all planted-error test cases | ≥ 90% |
| Incorrect changes automatically passed | Zero across the entire supported held-out release suite; report raw counts and rates by defect type and viewport |
| Human-review rate and verification-error rate | Report separately with reasons; neither counts as an automatic pass or explicit defect detection |
| Median time from "Mark done" to verdict | < 2 minutes |
| Model cost per request | < $0.25 |
| Builder guidance usability | A junior builder completes each core recipe on the approved fixture without clarification or undocumented steps |

**Evaluation protocol:** label the 30 anonymised requests with intended scope, required jobs, targets, exclusions and expected checks. Keep held-out cases separate from prompt-tuning examples. Report numerator/denominator for every percentage and results by supported change type and viewport; do not imply that a small test set establishes production reliability. Use paired correct/incorrect site states and include stale-cache, duplicate-target, wrong-link, omitted-footer, mobile-layout and shared-component-change cases. Measure verdict latency from enqueue to completion, including retries, and report median and p95. Include successful rematching after text changes, duplicate clicks versus corrected resubmissions on the same revision, an omitted requirement despite all generated jobs passing, and changes during the final capture window or lease expiry. Demo gates require correct-change pass, planted-error rejection, stale-verdict blocking and honest infrastructure-error handling. Observe a junior builder following each core recipe; record questions and missing steps, revise the recipe and repeat before approval.

**Release gate:** zero false passes are required across the entire supported held-out release suite, including every declared core change type and viewport, rather than only the rehearsed demo. Any known false-pass case blocks release until fixed and rerun or explicitly excluded from supported scope with a recorded reason and Care lead approval. Preserve the failed result in the report; publish exclusions and route matching requests to human review before verification. Do not silently remove failed cases or reclassify them to improve metrics. After tuning on a held-out failure, treat that case as a regression case and use additional untouched cases for held-out reporting. The ≥90% explicit-failure target remains separate: review/error outcomes may block an incorrect change but do not count as explicit defect detection. These small-suite gates support a prototype release only.

**Risks and mitigations**

| Risk | Mitigation |
| --- | --- |
| SAM mis-locates small text elements | DOM text check runs first; crop padding; fall back to judge on full page |
| Page caching shows stale content after the change | Cache-bust query string; retry capture after 60 s |
| Dynamic content (sliders, popups) causes false regressions | Disable animations in capture; mask known dynamic regions |
| Spark 1.3 image input not available via API | Use Muse Spark 1.1 for the judge |
| Judge too lenient | Plant known-bad changes in tests; require per-criterion pass |

**Open questions**

- [ ] Which 5–10 real sites can we use as test sites, and can we make real changes on them during the hackathon?
- [ ] Can we anonymise 30 past requests for the test set?
- [ ] Is Muse Spark 1.3 Contributor available in India for dev use?
- [x] Solo (Indie track) or vSplash team (Startup track)? — **Decided 2026-11-04: solo, Indie track.**
- [ ] Evidence retention period — is 90 days right?
- [ ] Which WordPress theme/editor and approved editing recipes define the core demo?
- [ ] What crawl/page limits, viewports and freshness window apply to each request?
- [ ] Who acts as Care lead for requirement-coverage approval and ambiguous scope, and who maintains the labeled evaluation set?
- [ ] Who coordinates the site edit freeze, and are the prototype five-minute capture window and 30-second final fingerprint age appropriate for the selected sites?
- [ ] Which current official model IDs support the required image/structured-output calls, and what are their actual billing units and data terms?
- [ ] If the form stretch goal proceeds, which sites, recipients, inbox connector and cleanup policy are approved?

