<!-- Nav: ← [Tier 2 parent — e.g., docs/state-persistence.md] | → [code file#line — e.g., state.py#L51] | ↔ [sibling Tier 3 specs] -->

# [Subsystem Name] — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| (write 3–6 real Q&A rows after authoring this spec — anchor the pre/post/invariant that agents will most often need to verify) | (≤2 sentences) | [Section](#section-anchor) or `file.py#Lxx` |

<!-- Example rows — replace with real anchors for this spec:
| What pre-condition must hold before save() is called? | The state dict must be serializable to JSON; version is injected by save() itself. | [Pre-conditions](#pre-conditions) |
| What does save() guarantee after it returns without error? | The file on disk contains the new state and no partial write is visible. | [Post-conditions](#post-conditions) |
| What happens if the temp file write fails mid-way? | The original file is untouched; the .tmp file is deleted; an OSError is logged at ERROR. | [Error Conditions](#error-conditions) |
-->

## Scope

Which code section this spec covers.

- **File:** `custom_components/climate_advisor/file.py`
- **Approximate line range:** L_start – L_end
- **Entry point:** `function_name()` or `ClassName.method_name()`

What this spec does NOT cover (name the sibling spec that covers it, if one exists).

## Pre-conditions

What must be true before this code section executes:

1. (e.g., "The config directory path exists and is writable")
2. (e.g., "`load()` has been called at least once since construction")
3. (e.g., "All list fields in state are capped before this method is called")

## Post-conditions

What is guaranteed to be true after this code section executes successfully:

1. (e.g., "The JSON file on disk reflects the exact state dict passed in")
2. (e.g., "No .tmp file remains in the config directory")
3. (e.g., "The returned value satisfies: 0 ≤ result ≤ 1")

## Invariants

Properties that hold throughout execution of this code section (not just before/after):

1. (e.g., "The original file is never modified until os.replace() succeeds — partial writes are isolated to the .tmp file")
2. (e.g., "The version field is always written by save(), never by the caller")

## State Transitions

(Include if this spec covers a state machine; omit or replace with a simpler table if not.)

| From state | Trigger | To state | Side effects |
|---|---|---|---|
| `state_a` | `event_x` | `state_b` | logs WARNING, clears pending |
| `state_b` | `event_y` | `state_c` | commits to disk |

## Error Conditions

What can fail, how it is handled, and what the caller receives:

| Failure | Handling | Caller receives |
|---|---|---|
| JSON decode error on load | Log WARNING, return `{}` (fresh state) | Empty dict |
| OS error during .tmp write | Log ERROR, delete .tmp, leave original intact | (returns None / no-op) |
| Unexpected field type in loaded data | Log WARNING, reset field to default | Corrected LearningState |

**Rejection codes** (if applicable — e.g., thermal observation rejections):

| Code | Meaning |
|---|---|
| `code_name` | Short description of why the observation was rejected |

## Code Reference

- [`function_name`](../custom_components/climate_advisor/file.py#Lxx) — entry point
- [`helper_name`](../custom_components/climate_advisor/file.py#Lxx) — supporting function
