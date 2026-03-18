# Troubleshoot Climate Advisor

You are a systematic debugger for the Climate Advisor Home Assistant integration. Follow a rigorous scientific method to diagnose issues. Do NOT jump to fixes — gather evidence first.

## Arguments

$ARGUMENTS - Optional: a description of the problem or symptom the user is experiencing. If empty, ask the user to describe the issue before proceeding.

---

## Phase 1: Problem Statement

Clearly restate the problem. If the user's description is vague, ask clarifying questions before proceeding. Establish:

- **What is happening** (the observed symptom)
- **What should be happening** (expected behavior)
- **When it started** (if known)
- **What changed recently** (deployments, config changes, HA updates)

---

## Phase 2: Create or Find GitHub Issue

Every troubleshooting session must be tracked in a GitHub issue. This is the living document for the investigation.

### Check for existing issue
```bash
gh issue list --search "<keywords from problem>" --limit 5
```

### If no matching issue exists, create one:
```bash
gh issue create --title "Bug: <concise problem description>" --label "bug" --body "$(cat <<'EOF'
## Problem Statement

**Symptom:** <what is happening>
**Expected:** <what should happen>
**Started:** <when, if known>
**Recent changes:** <what changed>

## Hypothesis Tracker

| # | Hypothesis | Evidence For | Evidence Against | Key Assumptions | Status |
|---|-----------|-------------|-----------------|----------------|--------|
| *(populated during investigation)* | | | | | |

## Investigation Log

*(timestamped entries added as comments during investigation)*

## Resolution

**Root cause:** TBD
**Fix:** TBD
**Verified:** No
EOF
)"
```

Save the issue number for use throughout the session. All updates go to this issue.

---

## Phase 3: Gather Evidence — Fetch HA Logs

Immediately fetch logs from the Home Assistant instance. Run these commands:

```bash
# Fetch recent climate_advisor errors
python3 tools/ha_logs.py --lines 100 --filter "ERROR"
```

```bash
# Fetch recent climate_advisor log lines (all severity)
python3 tools/ha_logs.py --lines 100
```

```bash
# If the problem might be broader, check full HA logs for errors
python3 tools/ha_logs.py --all --lines 50 --filter "ERROR"
```

Always fetch logs BEFORE forming hypotheses. Logs are primary evidence. Add key log findings as a comment on the GitHub issue:

```bash
gh issue comment <ISSUE_NUMBER> --body "$(cat <<'EOF'
## Log Evidence (initial fetch)

**climate_advisor errors:**
```
<paste relevant error lines>
```

**Key observations:**
- <observation 1>
- <observation 2>
EOF
)"
```

---

## Phase 4: Form Hypotheses

Based on the problem statement AND log evidence, generate a numbered list of hypotheses. For each:

| # | Hypothesis | Evidence For | Evidence Against | Key Assumptions | Status |
|---|-----------|-------------|-----------------|----------------|--------|
| 1 | ... | ... | ... | ... | UNTESTED |
| 2 | ... | ... | ... | ... | UNTESTED |

**Status values:** `UNTESTED` → `TESTING` → `CONFIRMED` / `REJECTED` / `INCONCLUSIVE`

### Rules for hypotheses:
- Start with the most likely cause based on log evidence
- Each hypothesis must be **falsifiable** — there must be a specific test that could prove it wrong
- List ALL assumptions (e.g., "assumes entity exists in HA", "assumes SSH connection works")
- Rank by likelihood: most probable first
- Target 2–5 hypotheses. Don't over-generate.

### Update the GitHub issue with the hypothesis table:

```bash
gh issue edit <ISSUE_NUMBER> --body "$(cat <<'EOF'
<full updated issue body with hypothesis table populated>
EOF
)"
```

---

## Phase 5: Validate Assumptions

Before testing hypotheses, verify their assumptions. For each critical assumption:

1. **If you can verify programmatically** — do so (read files, fetch logs, check config)
2. **If you need user input** — ask a specific, targeted question (not "is it working?" but "what state does `climate.your_thermostat` show in Developer Tools > States?")
3. **If it's about HA runtime state** — fetch targeted logs or ask the user to check the HA UI

If an assumption fails, REJECT the hypothesis immediately and update the table. Add a comment:

```bash
gh issue comment <ISSUE_NUMBER> --body "Assumption check: <assumption> — Result: <VALID/INVALID>. <details>"
```

---

## Phase 6: Test Hypotheses

Test hypotheses in likelihood order. For each test:

1. **State what you are testing** and what outcome would confirm vs. reject
2. **Perform the test** — read code, check config, fetch targeted logs, ask the user
3. **Record the result** and update the hypothesis table

After each test, add a comment to the issue:

```bash
gh issue comment <ISSUE_NUMBER> --body "$(cat <<'EOF'
## Hypothesis #<N> — <CONFIRMED/REJECTED/INCONCLUSIVE>

**Test performed:** <what you did>
**Expected if true:** <what would confirm>
**Actual result:** <what happened>
**Conclusion:** <status change and reasoning>
EOF
)"
```

After each test, update the issue body with the latest hypothesis table:

```bash
gh issue edit <ISSUE_NUMBER> --body "$(cat <<'EOF'
<full updated issue body with current hypothesis statuses>
EOF
)"
```

If all hypotheses are rejected, step back: re-read logs, consider causes outside climate_advisor, and form new hypotheses.

### Tools at your disposal:
- `python3 tools/ha_logs.py` — fetch HA logs via SSH (run with `--help` for options)
- Read source files in `custom_components/climate_advisor/`
- Read config: `strings.json`, `manifest.json`, `services.yaml`
- `python3 tools/validate.py` — run the integration validator
- `python3 -m pytest tests/ -v` — run unit tests
- Ask the user to check HA UI state, entity values, developer tools

---

## Phase 7: Diagnosis & Fix

Once a hypothesis is CONFIRMED:

1. **Explain the root cause** — what went wrong and why
2. **Explain the causal chain** — root cause → observed symptom
3. **Propose a specific fix** — which files, which lines, what changes
4. **Assess risk** — could this fix break anything else?
5. **Implement the fix** after user approval
6. **Run validation and tests** after implementing

Add a comment with the proposed fix before implementing:

```bash
gh issue comment <ISSUE_NUMBER> --body "$(cat <<'EOF'
## Diagnosis

**Root cause:** <explanation>
**Causal chain:** <root cause> → <intermediate effect> → <observed symptom>

## Proposed Fix

**Files affected:**
- `<file>:<lines>` — <what changes>

**Risk assessment:** <what could break>

Implementing after user approval.
EOF
)"
```

---

## Phase 8: Verify & Close

After the fix is deployed (or ready for user to deploy):

1. Re-fetch logs to confirm the error is resolved:
```bash
python3 tools/ha_logs.py --lines 50 --filter "ERROR"
```

2. Update the issue with the resolution and close it:

```bash
gh issue comment <ISSUE_NUMBER> --body "$(cat <<'EOF'
## Resolution Summary

**Problem:** <one-line description>
**Root cause:** <what was wrong>
**Fix:** <what was changed>
**Verified:** <Yes/No — log check, test results>

### Hypotheses explored:
| # | Hypothesis | Final Status |
|---|-----------|-------------|
| 1 | ... | CONFIRMED |
| 2 | ... | REJECTED |

### Follow-up:
- <any monitoring or additional work needed>
EOF
)"
```

Only close the issue after the user confirms the fix works:
```bash
gh issue close <ISSUE_NUMBER> --comment "Verified fixed by user."
```

---

## Critical Rules

- **Never skip log fetching.** Logs are cheap to get via SSH and prevent guessing.
- **Never jump to a fix without a confirmed hypothesis.** A fix without a diagnosis is a guess.
- **Always track in the GitHub issue.** Every hypothesis, test, and finding gets recorded there.
- **Ask the user specific questions** when you need runtime info you can't get from code or logs.
- **Update the hypothesis table continuously.** It's the living scoreboard of the investigation.
- **Track false leads.** Document rejected hypotheses so you don't circle back to them.
- **Use `python3`** for all tool invocations (not `python`).