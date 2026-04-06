# Prompt Engineering Guide

**What this means:** Writing instructions, rules, or system prompts for AI models/LLMs — text that controls an assistant's behavior (basic_rules, docs, system context). Not: shell prompts, database queries.

When the user says "let's engineer a prompt for X", "write instructions for Y", "design a system prompt", "how do I tell the agent to do Z":

## Process

1. **Clarify the goal** (1-2 questions max): What should the agent do? What mistakes to avoid? Example scenario?
2. **Create draft** in `{workspace}/prompt-TOPIC-draft.md`
3. **Iterate with user** until satisfied
4. **Save** by type:
   - Behavior rule → `basic_rules/` (always in context — keep short!)
   - Reference / howto → `docs/` (loaded on demand)
   - User-specific → `prefs/` (personal data, preferences)
   - Template for the user → leave in workspace

## Writing guidelines

**For local LLMs: short, concrete, with examples.**

- **Name the trigger:** When does the rule apply? ("When the user says X...", "For voice messages...")
- **Examples > descriptions:** `"10-20" → "10 bis 20"` beats "replace range hyphens with the word bis"
- **Negative examples help:** "NOT: ... BUT: ..."
- **No redundancy:** Check if the rule exists already (`grep -r "keyword" basic_rules/ docs/`)
- **No vague phrasing:** "usually", "normally", "when appropriate" — LLMs ignore these
- **One sentence = one rule** — multiple rules in one sentence get partially forgotten

## Typical structures

**Behavior rule** (basic_rules):
```
**[Title].** [When it applies.] [What to do.] [Example.] [What NOT to do.]
```

**Howto guide** (docs):
```
# Title
## When to read
[Trigger sentence]
## Steps
1. ...
## Example
```

## Quality check before saving

- Would a local 7B model understand this?
- Is it clear when the rule applies and when not?
- Is there a concrete example?
- Is it as short as possible?
