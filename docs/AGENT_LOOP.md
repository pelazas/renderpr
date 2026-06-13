---
name: RenderPR self-iterating agent loop
description: Operational workflow for an LLM agent to iteratively develop and debug renderpr against a live test PR until a stated goal is reached.
audience: Claude Code (or equivalent), running locally in zellij with gh + aws CLIs authenticated
---

# RenderPR Agent Loop

This document describes how a local coding agent (Claude Code running in this repo) iteratively develops and debugs `renderpr` by:

1. taking a user-supplied **goal**,
2. exercising the deployed bot against a live test PR,
3. reading bot output, CloudWatch logs, and the live ephemeral app,
4. patching renderpr source, committing, pushing, and waiting for redeploy,
5. repeating until the goal is met, a roadblock is hit, the user stops the loop, or a stagnation/iteration cap is reached.

It assumes the operator (the human) is the one who invokes the loop with a specific task — the loop is **not** continuously running.

---

## Section 0 [Preconditions]

The loop must verify these before iteration 1 and abort if any fail:

| Check | Command | Expected |
|---|---|---|
| `gh` authenticated to repos `pelazas/renderpr` and `pelazas/test-hello-world` | `gh auth status` | logged in, scopes include `repo`, `workflow` |
| AWS identity has CloudWatch Logs read + GH Actions read | `aws sts get-caller-identity` | account `303859149452`, region `eu-west-1` |
| Working tree clean on `renderpr` | `git status --porcelain` | empty |
| On the branch the operator specified | `git branch --show-current` | matches operator input (default `main`) |
| Target test PR exists and is open | `gh pr view <PR> --repo pelazas/test-hello-world` | `state: OPEN` |
| Last deploy succeeded | `gh run list -R pelazas/renderpr -w deploy --limit 1 --json conclusion` | `success` |

If any check fails, do **not** iterate. Report to user and stop.

---

## Section 1 [Inputs the operator supplies when triggering the loop]

The operator says something like:

> "Run the renderpr agent loop on test-hello-world PR #16. Goal: the bot must correctly identify `/users` as an affected route and post a screenshot of the new modal opening. Max 8 iterations."

The loop must receive:

1. **Target test PR** — e.g. `pelazas/test-hello-world#16`. If omitted, default to the highest-numbered open PR in `test-hello-world`.
2. **Goal statement** — free-text success definition. The loop turns this into a checklist (see Section 5 [Success-definition skill]).
3. **Scenario** *(optional)* — a sequence of `@renderpr` commands to drive multi-step flows (e.g. `review` → `code change` → `apply`). See Section 6 [Scenarios]. If omitted, the loop defaults to a single `@renderpr review this` per iteration.
4. **Branch strategy** *(optional)* — one of:
   - `main` *(default)* — patches commit and push straight to `main`; each push triggers a fresh CDK deploy.
   - `feature/<name>` — patches go to that branch; loop opens a draft PR against `main` on first push if one doesn't exist, and **does not auto-merge**. The loop will still wait for that branch's deploy if your workflow deploys it (currently `deploy.yml` only deploys on push to `main`, so a feature branch means no auto-redeploy — the loop will detect this and abort with an explanatory message).
   - The operator picks based on risk: fixes → `main`, larger features → `feature/<name>`.
5. **Iteration cap** *(optional)* — default 6, max 8.
6. **Stagnation cap** *(optional)* — default 3 consecutive iterations without measurable progress (no new checklist items passing).

The operator does **not** need to specify polling intervals, log groups, or filenames — those are derived from this document.

---

## Section 2 [Loop overview]

```
┌────────────────────────────────────────────────────────────────────┐
│  iteration N                                                       │
│                                                                    │
│  (a) ensure latest renderpr code is deployed  ──── wait if running │
│  (b) run scenario step(s) on the test PR    ──── post @renderpr    │
│  (c) watch ECS task lifecycle + logs        ──── until done/fail   │
│  (d) read bot's latest comment(s) + screenshots                    │
│  (e) probe the ephemeral live app (link in comment)                │
│  (f) evaluate against goal checklist (Section 5)                   │
│  (g) decide: done / fix-and-iterate / roadblock                    │
│  (h) if fix: edit src → commit → push → back to (a)                │
└────────────────────────────────────────────────────────────────────┘
```

Stop conditions:
- goal checklist fully ticked → **success**
- `N >= iteration cap` → **stop (cap)**
- `stagnation_streak >= stagnation cap` → **stop (no progress)**
- operator comments `/stop` on the test PR or interrupts the agent → **stop (user)**
- the agent decides it cannot diagnose further → **roadblock** (must explain why)

---

## Section 3 [Step-by-step iteration]

### 3a. Ensure latest renderpr code is deployed

Before triggering a review, the live bot must match `origin/<branch>` at our latest commit.

```bash
LOCAL_SHA=$(git rev-parse HEAD)

gh run list -R pelazas/renderpr -w deploy \
  --limit 5 \
  --json databaseId,headSha,status,conclusion,createdAt
```

- If a run is `in_progress` or `queued` → poll every **60s** until `completed`.
- If the latest `success` run's `headSha` matches `LOCAL_SHA` → proceed.
- If the latest run is `failure`/`cancelled` → fetch its logs (`gh run view <id> --log-failed`) and treat as a fix candidate (skip 3b–3e for this iteration, go straight to 3h).

Typical full deploys observed: 1–7 min (CDK + ECR push). Use **60s** poll cadence — never poll faster than 30s.

### 3b. Run scenario step(s)

For a default (single-review) loop, post one comment on the test PR:

```bash
gh pr comment <PR> --repo pelazas/test-hello-world --body "@renderpr review this"
```

For a multi-step scenario (Section 6 [Scenarios]), execute each step in order. Each step is:

1. Post the step's `@renderpr` comment.
2. Record `T_step_N` (ISO 8601, UTC).
3. Wait for the bot's response per Section 3c–3d.
4. Evaluate **only the checks tagged to that step** (Section 6 explains tagging).
5. If that step fails, do **not** proceed to the next step in the scenario this iteration — go to Section 3g [Decide].

Record the overall `T_trigger = T_step_1` so global log filters still work.

### 3c. Watch the run

Two log groups carry everything:

| Concern | Log group |
|---|---|
| Webhook receipt + ECS dispatch | `/aws/lambda/RenderprStack-WebhookHandler40BDAF19-RASNiqMmDKcG` |
| Container lifecycle (clone, npm ci, dev server, Playwright, LLM, post comment) | `RenderprStack-ReviewTaskDefReviewContainerLogGroupB702639E-MY5bImxOwIQg` |

Use `aws logs tail` with `--since` and `--follow` (run in background, capture to file):

```bash
aws logs tail "/aws/lambda/RenderprStack-WebhookHandler40BDAF19-RASNiqMmDKcG" \
  --since 1m --follow --format short > /tmp/renderpr-lambda.log &

aws logs tail "RenderprStack-ReviewTaskDefReviewContainerLogGroupB702639E-MY5bImxOwIQg" \
  --since 1m --follow --format short > /tmp/renderpr-ecs.log &
```

**Per-step timing budget (default):**

- expected end-to-end (boot → comment posted): **2–5 min** typical, 8 min worst case for `review`; **30–90s** for `apply` since the dev server and Playwright are already warm
- poll the bot for a new comment every **45s**, starting 60s after the step's trigger time
- give up waiting for a comment after **10 min** and treat as "no comment posted" failure (still inspect logs)

End-of-step signals (any of these means "stop watching"):
- a new comment by `@renderpr` appears after the step's trigger time on the test PR
- ECS log emits `RenderPR review session ended` or `Review paused: LLM unavailable`
- for an `apply` step: a new commit by the bot appears on the PR branch (use `gh pr view --json commits`)
- the Fargate task transitions to `STOPPED`

### 3d. Read bot comment + screenshots

```bash
gh pr view <PR> --repo pelazas/test-hello-world --comments \
  --json comments \
  --jq '.comments | map(select(.author.login=="renderpr" and .createdAt > "'$T_step'")) | last'
```

Important: filter on `author.login == "renderpr"` AND `createdAt > T_step`. The bot's `authorAssociation` is `NONE`, **not** `BOT` (it's a GitHub App).

From the comment body, extract:

- **screenshot S3 URLs** — regex `https://renderpr-screenshots-303859149452\.s3\.amazonaws\.com/screenshots/<PR>/[0-9a-f]+\.png`
- **live preview URL** — last line of the form `http://<IPv4>:3000`. May be missing if dev server died — that itself is a signal.

Fetch screenshots inline (`Read` tool handles PNG URLs via download) for vision analysis if the goal requires verifying UI content.

### 3e. Probe the live ephemeral app

If a live URL was found and the iteration's goal involves runtime behavior:

```bash
curl -sS -m 10 -o /dev/null -w "%{http_code}\n" http://<ip>:3000/         # smoke
curl -sS -m 10 http://<ip>:3000/users | head -c 4000                       # content sniff
```

Caveat: the live app expires after **15 min idle** (Fargate task timeout) — `@renderpr` invocations reset the timer. Probe within ~5 min of the trigger to be safe.

### 3f. Evaluate the goal

Build a checklist from the user's goal statement at the start of iteration 1, e.g.:

> Goal: "bot must correctly identify `/users` as an affected route and post a screenshot of the new modal opening"

becomes:

- [ ] Bot posted a review comment after `T_trigger`
- [ ] Comment mentions `/users`
- [ ] Comment contains a screenshot URL whose ALT text references `/users`
- [ ] Vision check on the screenshot: modal is clearly open and rendered correctly *(ambiguous result = fail)*
- [ ] ECS log has no `ERROR` or `Traceback` between `T_trigger` and end-of-run
- [ ] Lambda log shows `200 OK` for the webhook (no 401/5xx)

**Vision-check rule:** any uncertainty counts as fail. If the screenshot is partially rendered, modal half-open, content cut off, blurry, or otherwise hard to read — fail it and iterate. Conservative bias is intentional; better to spend an iteration than declare a half-broken UI a success.

Each box is independently testable from gh + aws + curl + vision. Translating the free-text goal into this checklist is the agent's job at iteration 1 and **must be shown to the operator for thumbs-up before iteration 1 begins**.

### 3g. Decide

| Situation | Action |
|---|---|
| All checklist items pass | Stop. Report success with iteration count and total wall clock. |
| Some items fail, root cause identifiable | Go to 3h (patch). |
| Items fail, cause not identifiable from logs/comment | Pull deeper logs (full ECS task logs since boot), re-evaluate. If still unclear after one deeper look, **roadblock**. |
| Same checklist items failing for `stagnation_cap` iterations in a row | **roadblock — no progress**. |
| Iteration cap reached | Stop. Report state of checklist. |

### 3h. Patch, commit, push

Constraints on patches the loop is allowed to make:

- May edit anything under `src/`, `cdk/`, `Dockerfile`, `requirements.txt`.
- May **not** edit `.github/workflows/` (changes deploy semantics).
- May **not** edit `docs/` mid-loop unless the goal is specifically docs.
- Must run `pytest tests/` locally; if tests fail, fix or revert before pushing.
- Commit message: `loop: <one-line root cause> (iter N)`.
- Push to the branch the operator specified (default `main`).

After push, go back to 3a.

---

## Section 4 [Concurrency, rate, and cost guardrails]

- **One review at a time per PR.** Never trigger `@renderpr review` while an ECS task is still running for that PR — wait for the previous task to STOP first.
- **GitHub comment rate.** The loop posts only the scenario's `@renderpr` comments. No status/progress chatter to the test PR — keep all loop state local.
- **Cache discipline.** Anthropic's prompt cache has a 5-minute TTL. While waiting (deploy in progress, ECS booting, review in flight), sleep in chunks of **≤270s** rather than one big 5–10 min sleep. Each cache miss re-reads this doc, the logs, and the PR thread uncached — slower and materially more expensive. Functional impact: none; pure cost/latency optimization.
- **CloudWatch cost.** `aws logs tail --follow` is fine for short windows. Kill the background tails at the end of each iteration; restart fresh next iteration with `--since`.

---

## Section 5 [Success-definition skill]

Rather than re-prompting the agent every iteration with the free-text goal, the operator's goal statement is converted **once** at iteration 1 into a structured checklist (see Section 3f). The checklist is stored locally (e.g. `/tmp/renderpr-loop-goal.json`) and re-evaluated every iteration. This avoids drift, lets the operator see exactly what is being tested, and makes "stagnation" detection trivial (compare checklist diffs across iterations).

Schema:

```json
{
  "pr": "pelazas/test-hello-world#16",
  "goal_text": "...",
  "scenario": [ /* see Section 6 */ ],
  "checks": [
    { "id": "comment-posted", "step": 1, "kind": "gh-comment-exists", "args": { "since": "T_step_1" } },
    { "id": "mentions-users", "step": 1, "kind": "comment-body-regex", "args": { "pattern": "/users" } },
    { "id": "screenshot-modal-visible", "step": 1, "kind": "vision", "args": { "url_filter": "users", "prompt": "Is a modal dialog clearly open and fully rendered?" } },
    { "id": "no-ecs-errors", "step": 1, "kind": "ecs-log-clean", "args": { "since": "T_step_1" } },
    { "id": "lambda-200", "step": 1, "kind": "lambda-status", "args": { "expected": 200 } }
  ]
}
```

Each check has a `step` field tying it to one step of the scenario (default `1` for single-step loops). Adding new `kind` values is the natural extension point.

---

## Section 6 [Scenarios — multi-step `@renderpr` flows]

A scenario is an ordered list of `@renderpr` interactions that exercise more of the bot's command surface than a single review. Use scenarios when the goal involves verifying `code change`, `apply`, or any flow where one step depends on the previous one succeeding.

### Step shape

```json
{
  "step": 2,
  "comment": "@renderpr code change: make the homepage title red",
  "expect": [
    "bot posts a preview comment within 5 min",
    "preview screenshot shows the title in red",
    "no Traceback in ECS log for this step"
  ],
  "wait_for": "bot-comment",
  "timeout_minutes": 8
}
```

`wait_for` values:
- `bot-comment` *(default)* — wait until a new `@renderpr` comment appears.
- `bot-commit` — wait until a new commit by the bot appears on the PR branch (used after `apply`).
- `no-comment` — wait `timeout_minutes` then assert *no* bot comment was posted (used to verify an unrecognized or ignored command, such as `@renderpr reject`, produces no response).

### Example: full `review → code change → apply` flow

```json
{
  "scenario": [
    {
      "step": 1,
      "comment": "@renderpr review this",
      "wait_for": "bot-comment",
      "expect": ["review comment posted", "all viewports rendered"]
    },
    {
      "step": 2,
      "comment": "@renderpr code change: make the homepage title red",
      "wait_for": "bot-comment",
      "expect": ["preview comment posted", "vision: title is red in preview screenshot"]
    },
    {
      "step": 3,
      "comment": "@renderpr apply",
      "wait_for": "bot-commit",
      "expect": [
        "new commit on the PR branch authored by renderpr",
        "commit touches only the title-related file",
        "commit does NOT touch any *.renderpr.bak or runtime-generated mock files"
      ]
    }
  ]
}
```

### Rules

1. Steps run **sequentially within an iteration**. If step N fails, steps N+1…end are skipped; the iteration ends and Section 3g [Decide] runs.
2. Each step's `expect` items become checklist items tagged with that step's number. The goal is "all steps' checks pass in the same iteration".
3. Between steps, the loop must wait for the bot's previous response to be **fully written** (the comment's `updatedAt` stable for 10s) before posting the next step's comment — racing the bot causes duplicate dispatches.
4. The scenario itself is fixed across iterations; only the renderpr **code** changes. If the operator wants to change the scenario, they stop the loop and re-trigger.
5. For `apply` steps, the loop verifies the commit on the PR branch via `gh pr view --json commits` — not via `git log` locally (the loop's local checkout is renderpr, not the test repo).

---

## Section 7 [Failure modes the loop must recognize]

These are common and have known signatures:

| Symptom | Likely cause | Where to look |
|---|---|---|
| Bot posts no comment within 10 min | ECS task never started, or dev server timeout | Lambda log: was `RunTask` called? ECS log: did `npm ci` finish? |
| Comment posted but no screenshots | Playwright crash, or all routes empty | ECS log: `playwright`/`Timeout 60000ms exceeded` |
| Comment posted but live link broken | Public IP fallback to `localhost`, or task already STOPPED | ECS log: `Public IP lookup failure` |
| Comment posted but content is wrong/repetitive | Route inference picked wrong routes | ECS log: route inference LLM input/output |
| `RuntimeError: ssm:GetParameter denied` | Task role missing perms (infra regression) | Lambda log + IAM diff in `cdk/` |
| `apply` step produces no commit | Command server not running, dispatch failed, or no pending edits | ECS log: `__renderpr/command` dispatch; Lambda log: cold-start fallback |
| `apply` commit includes `*.renderpr.bak` or runtime mocks | `stageable_edits()` regression | ECS log + diff of the bot's commit |
| Identical bot comment as previous iteration | Bot is hitting cache or route inference is non-deterministic on identical input | check whether our patch actually changed observable behavior |
| Deploy workflow fails on `pytest` | Our patch broke tests | `gh run view <id> --log-failed` |

When the loop catches one of these, it should reference the row by name in its iteration report.

---

## Section 8 [Output the loop produces]

At the end of every iteration the loop prints a one-line summary:

```
[iter 3/8] checks pass: 7/9 | step 2 failed (vision: title not clearly red) | patched: src/agent/code_edit.py | push: a1b2c3d | duration: 6m12s
```

At the end of the loop:

```
GOAL: bot must correctly identify /users ...
SCENARIO: review → code change → apply
RESULT: success in 4 iterations (28m total)
COMMITS (renderpr): a1b2c3d, d4e5f6g, h7i8j9k, k0l1m2n
FINAL CHECKLIST: 9/9 pass
```

or on roadblock:

```
RESULT: roadblock at iteration 5 — "step 3 (apply) fails: bot commit consistently includes src/app/api/users/route.ts (a runtime-generated mock). Patches to ChangeSession.stageable_edits have not changed this behavior over 3 iterations. Likely the file is also being touched by user-edit tracking, masking the runtime-only marker. Needs operator decision on whether to restructure ChangeSession or accept the bak/mock filter as last-resort."
```

---

## Section 9 [Known gaps and operator decisions still required]

These are intentionally unresolved here — the operator answers them at trigger time or default behavior applies.

1. **Branch strategy** — operator picks per session (Section 1, item 4). Default `main` for fixes.
2. **Vision check is conservative** — ambiguous screenshot = fail. This is fixed, not configurable.
3. **Cross-PR effects.** Loop targets one test PR. If the patch could affect other live PRs (e.g. changing route inference globally), the loop notes this but does not test other PRs.
4. **Secrets / SSM.** Loop is read-only on AWS except for log access. It will not edit SSM, ECR, or run `cdk deploy` directly — all infra change goes through `git push → GH Actions`.
5. **Test PR provisioning.** Loop assumes the test PR already exists and is open. If it's merged or closed, loop aborts with a clear message; it will not open new PRs in `test-hello-world` on its own.
6. **Concurrency with the operator.** If the operator pushes to `renderpr` mid-loop, the loop's next iteration picks up the new SHA automatically (3a). If the operator comments `@renderpr` on the test PR mid-loop, the next iteration's "new comment" filter may match the operator's trigger comment unless `author.login=="renderpr"` is enforced (it is — see 3d).
7. **Bot non-determinism.** The review LLM is non-deterministic by design (creative summarization). The loop deals with this by making checks tolerant: presence over exact phrase, vision over ALT-text, structured signals over prose. Do **not** "fix" this by lowering temperature in the bot itself — it flattens reviews without removing variance. If checks keep failing in *different* ways each iteration, that's a signal the bug is upstream of any single patch.

---

## Section 10 [Trigger format the operator uses]

When ready to run, the operator says one of these. Anything in `<>` is required; anything in `[]` is optional.

**Minimal (single review):**

> "Run the agent loop. Test PR: pelazas/test-hello-world#<N>. Goal: <free text>."

**With caps and branch:**

> "Run the agent loop on PR #<N>. Goal: <free text>. Branch: feature/<name>. Cap: 8. Stagnation: 3."

**With scenario:**

> "Run the agent loop on PR #<N>. Goal: <free text>. Scenario: review → code change ('make title red') → apply, verify the bot's commit only touches the title file."

The agent then:
1. Verifies preconditions (Section 0 [Preconditions]).
2. Converts goal + scenario → checklist (Section 5 [Success-definition skill] and Section 6 [Scenarios]).
3. Shows the checklist to the operator and **waits for thumbs-up** before iteration 1.
4. Begins iteration 1.

If the operator says "go" with no goal, the agent must ask for one — it cannot infer success from nothing.
