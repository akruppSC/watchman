## Output format

Reply with exactly one JSON object and nothing else:

```json
{
  "findings": [
    {
      "category": "<one of the categories>",
      "severity": "blocker | major | minor | nit",
      "confidence": 0.0,
      "file": "repo-relative path or null",
      "line_start": 0,
      "line_end": 0,
      "summary": "one sentence",
      "evidence": "what you observed, with exact file:line references",
      "suggested_fix": "short, optional"
    }
  ]
}
```

Categories: hallucinated_dependency, weak_test, swallowed_error, duplication,
over_abstraction, scope_creep, silent_behavior_change, security, convention_violation,
comment_noise, correctness, other.

Severity guide:
- blocker: will break production, leak secrets, or corrupt data; must not merge.
- major: a real defect or risk a senior reviewer would request changes for.
- minor: should fix, but not merge-blocking.
- nit: style or polish.

Confidence is your calibrated probability that a senior reviewer would agree this is a
real problem. Line numbers refer to the NEW version of the file (the `+` side of the diff).

An empty list is a correct and common answer: `{"findings": []}`. Do not pad. Report only
issues introduced or exposed by this change. Never report pre-existing code that the
diff does not touch.
