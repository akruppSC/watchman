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

An empty list is a correct and common answer: `{"findings": []}`. Do not pad. Report only
issues introduced or exposed by this change. Never report pre-existing code that the
diff does not touch.
