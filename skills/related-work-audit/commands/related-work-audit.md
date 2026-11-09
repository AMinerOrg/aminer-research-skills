---
description: Audit a manuscript for potentially missing related work
argument-hint: "[PDF path or AMiner pub_id] [review year: YYYY]"
allowed-tools: Read, Write, Bash
---

# `/related-work-audit`

Use the `related-work-audit` Skill for `$ARGUMENTS`.

- Require exactly one local PDF or 24-character hexadecimal AMiner `pub_id` and a review year; request missing input rather than inventing it.
- Follow the Skill's year window, bibliography exclusion, evidence, credential, and cost rules. Never ask the user to paste API keys into chat.
- Keep related-work coverage separate from direct experimental baseline checks and acceptance prediction. Report candidates for author review rather than mandatory citations.
