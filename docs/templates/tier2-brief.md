<!-- Nav: ← [Tier 1 parent — e.g., 01-STRATEGY-AND-DESIGN.md] | → [Tier 3 spec or code file] | ↔ [sibling Tier 2 docs] -->

# [Module Name] — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| (write 3–6 real Q&A rows after authoring this doc — one per major responsibility or invariant) | (≤2 sentences; enough to confirm relevance without reading the full section) | [Section](#section-anchor) or `path/to/file.py#Lxx` |

<!-- Example rows — replace with real anchors for this module:
| What does this module own and what does it explicitly not own? | It owns X and Y. It does not own Z (that belongs to coordinator.py). | [Scope](#scope) |
| What happens when this module receives invalid input? | It logs a warning and returns a safe default; it never raises. | [Error Handling](#error-conditions) |
| Where is the persisted data format defined? | In LearningState (learning.py:512); JSON file at /config/climate_advisor_learning.json. | [Data Structures](#data-structures) |
-->

## Scope

**Owns:**
- (list what this module is solely responsible for)

**Explicitly does NOT own:**
- (list adjacent concerns this module delegates or defers — name the module that owns each)

## Responsibilities

- (bullet each distinct behavior this module implements)
- (keep to observable actions, not implementation details)

## Interfaces

Key public functions/methods exposed by this module. List callers where known.

```python
# Primary entry point
def function_name(param: Type) -> ReturnType:
    """One-line description of contract."""
```

| Symbol | Caller(s) | Purpose |
|---|---|---|
| `function_name()` | `coordinator.py` | Short description |

**Events emitted / consumed** (if applicable):

| Event | Direction | Handler |
|---|---|---|
| `event_name` | emitted / consumed | `_handler_method` |

## Data Structures

Key types, fields, and storage format owned by this module.

```python
@dataclass
class KeyType:
    field_name: type  # what it stores; units if numeric
```

**Persistence** (if any): file path, format, write pattern.

## Invariants

Properties that must always be true while this module is running:

1. (e.g., "List X must never exceed N entries — enforced by cap at write time")
2. (e.g., "Value Y must be negative — rejected at commit if positive")
3. (e.g., "File Z is always written atomically — .tmp + os.replace — never truncated in place")

## Disclosure Path

← Tier 1 parent: [link to the Tier 1 doc that references this module]
→ Tier 3 specs: [list each Tier 3 spec that covers a subsystem of this module]
↔ Siblings: [other Tier 2 docs at the same level, if any]
