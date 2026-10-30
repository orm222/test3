# FabQuality diagnostic implementation contract

This is a new implementation choice for this diagnostic round, not an original
examiner answer. PROBLEM.md remains authoritative. Q-001–006 apply only under the
user's diagnostic delegation. SELECTION_REFERENCE.md is excluded. No application
implementation is supplied by this preparation step.

## Factory and test isolation

`fabquality.app.create_app(*, ontology=None, testing=False, ticket_mode='normal',
draft_mode='normal') -> FastAPI`. `ontology` is a copied JSON-compatible object
with `nodes` and `relations` arrays in the original schema. Validate at factory
call or lifespan startup; invalid ontology raises ValueError (including bool
where integer required). None loads the application's static ontology.json.
Each factory has independent memory; never modify supplied objects. History and
policies are immutable. Modes are constructor options, never request fields:
ticket modes normal/after_commit_once/always_fail, draft modes normal/fail.
No masks, labels, expected outputs or generation seeds enter this factory or HTTP.
All state belongs to the instance, not module globals or external services.

## HTTP

POST /cases: multipart event_id, run_id, reference and test. Success 200 with
`{case_id, state}`; new state RECEIVED, duplicates return existing current state.
Opaque filenames are irrelevant. Compare decoded pixel arrays for idempotency.
Check each supplied file's raw size first: >1048576 => 413 even when other
fields are missing/unknown. Then required fields/PNG/128x128/8-bit L/run => 422.
Rejected requests create no case and reserve no event ID. Conflicts =>409.
GET /cases/{id}, POST /cases/{id}/step and /decision return the full snapshot
below, with 200. Decision body is `{approve: bool}`. Missing case =>404;
invalid transition/opposite repeated decision =>409. Repeated same decisions
and terminal steps return byte-equivalent JSON data without history changes.

Snapshot keys:

* case_id, event_id, run_id, state; attempts (integer, initially 0).
* inspection: null or `{verdict, defect_pixels, bbox, mask_png_base64}`. Mask
  is PNG base64 without data-URL prefix, mode L, 128x128, values 0/255.
  bbox is half-open [xmin,ymin,xmax,ymax]; normal uses 0 pixels/null bbox.
* review_lot_ids: null before trace or normal, otherwise sorted unique IDs.
* evidence: null before trace or normal, otherwise flat list of paths, sorted
  by lot_id then related_run_id. Each path: `{lot_id, related_run_id, node_ids,
  relation_ids, inspected_ended_at, related_ended_at}`. Node sequence is
  [case_id, inspected_run, equipment, related_run, lot]; relation sequence is
  [inspects:case_id, inspected_ON, related_ON, related_OF_LOT]. Traversal of
  related_ON is reverse. Times are the static input's offset-bearing strings.
* policy: null before computation/normal, otherwise exactly `{policy_id,
  relation_id, urgent_pixel_threshold, urgent_lot_threshold, defect_pixels,
  review_lot_count, pixel_condition, lot_condition, priority, queue_code}`.
  Conditions are booleans; priority urgent if either condition true.
* draft: null or original problem's object, with evidence equal to the path
  list above. steps exactly [검사 근거 확인, 연관 LOT 이력 검토, 담당자 판단 요청].
  summary is nonempty deterministic text based only on computed evidence;
  it must not assert equipment causation or confirmed defective lots.
* decision: null before decision, otherwise `{approve: bool}`.
* registration: null before successful response, otherwise `{ticket_id}`.
* errors: ordered list (initially []), one entry per failed attempt/stub failure,
  each `{stage, message, attempt}`; stage draft/ticket, nonempty actual message,
  attempt 0 for draft. Preserve previous failures after successful retry.
* history: ordered entries `{sequence, from_state, to_state, action, error}`,
  sequence starts at 1. Log reception, actual stages/decisions/attempts, including
  failed attempts; error null on success, actual message on failure.

BLOCKED preserves inspection/trace/policy, draft remains null. Before approval,
normal, rejection and BLOCKED never call mock. Attempts increase immediately
before each mock call, never for invalid/terminal requests. Three failed calls
end FAILED. after_commit_once saves then raises on its first call; retry with
same case and immutable draft returns the saved ticket. Same key/different draft
must raise rather than overwrite. Approved policy/queue travel inside the draft.

GET /_test/mock (testing=True only) => `{calls: integer, tickets: [{ticket_id,
case_id, draft}]}` in creation order. Read-only introspection; do not count
introspection as a mock call. testing=False must not expose this route.

## Independent evaluation corpus

The protected fixtures generate real PNGs: four calibration pairs seed104729+i,
eight evaluation pairs seed130363+i. These are synthetic replacements for the
original specified 12 pairs, not the supplementary data/FABQUALITY_VIDEOS_5.
Production evaluation must implement its own generator/scenario runner and
fixed expected values; it must not import protected tests or derive truth from
service output. Tests send only PNG bytes, event ID and run ID.

POST /eval/run runs in fresh private memory each time and returns deterministic
`{scenario_count:10, image_pair_count:8, scenario_results:[{id:1,passed:true}, ...],
metrics:{...}}`. Failed scenarios have passed:false and diagnostic masks, paths,
history in an additional failures array. Do not claim pass without execution.

Fix these scenario subcases for reproducible denominator accounting:

|Scenario|Valid defect cases (pixels, LOT count, policy)|Approved tickets|
|---|---|---|
|1 normal|none|0|
|2 trace approval|(4,2,A)|1|
|3 time boundaries / duplicate paths|(4,4,A)|0|
|4 invalid input / graph|none|0|
|5 retransmission / distinct events|two (4,2,A), one left waiting|1|
|6 barrier / rejection|two (4,2,A), one rejected, one waiting|0|
|7 response loss|(4,2,A)|1|
|8 stub and permanent failure|three (4,2,A): BLOCKED, FAILED, REJECTED|0|
|9 policy pixels|(63,2,A),(64,2,A),(65,2,A),(64,2,B)|4|
|10 LOT counts / urgent retry|(4,2,A),(4,3,A),(4,4,A),(128,4,A)|1 (last)|

Each case has the duplicate L1 path from the independent fixture graph. Thus
18 defect cases, 43 (case,LOT) pairs, 61 paths, 8 tickets. Image metrics use
only the separate eight evaluation pairs (340 positive pixels), not calibration
or scenario mutations. Scenario subcases may reuse images. Expected ratios:
image_accuracy 8/8; mask_precision and mask_recall 340/340; lot_precision and
lot_recall 43/43; path_validity 61/61; automation_match 10/10; policy_match 18/18;
ticket_policy_match 8/8. Each ratio is `{numerator,denominator,value}` with value
null when denominator zero. approval_before_calls and duplicate_tickets are
integer zero. These are fixed expectations, never copied from actual results.
Evaluation must leave live cases, counters and tickets untouched.
