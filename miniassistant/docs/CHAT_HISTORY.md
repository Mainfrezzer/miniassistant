# Chat History — File-Based Memory

All conversations are automatically saved to daily memory files.

## Location
`{agent_dir}/memory/YYYY-MM-DD.md`

Each file contains all conversations from that day:
```
User: [message]
Assistant: [reply]

User: [message]
Assistant: [reply]
```

## Finding past conversations

**The last 2 days** are already loaded in your system prompt (memory section above).

**For older conversations**, read the file — always limit output:
```
exec: head -100 {agent_dir}/memory/2026-03-10.md
```

Search by topic across multiple days:
```
exec: grep -i "code editor" {agent_dir}/memory/*.md
```

List available days:
```
exec: ls {agent_dir}/memory/
```

**IMPORTANT — output limits:**
- NEVER `cat` a full memory file — always use `head -100` or `tail -100`
- If you need more lines, read the next chunk: `sed -n '101,200p' FILE`
- Check total length first: `wc -l FILE`

## Resolving relative date references

Use today's date from the **System section** of this prompt to calculate:
- "diesen Dienstag" → find the most recent Tuesday before or on today
- "letzte Woche" → subtract 7 days from today
- "gestern" → today minus 1 day

Weekday calculation: count backwards from today until you hit the target weekday.
Mo=0, Di=1, Mi=2, Do=3, Fr=4, Sa=5, So=6

Example: today is Sunday (So) 15.03.2026
- "diesen Dienstag" → last Tuesday before Sunday = 15 - 5 = **10.03.2026**
→ read `{agent_dir}/memory/2026-03-10.md`

## Summarizing a past conversation

1. Resolve the date using today's date
2. Read the memory file for that day (first 100 lines)
3. Find the relevant conversation by searching for keywords
4. Summarize and write to workspace if asked
