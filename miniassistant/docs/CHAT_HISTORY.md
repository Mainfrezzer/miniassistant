# Chat History / Conversation Memory

All conversations are automatically saved to daily memory files.

## Location
`/root/.config/miniassistant/agent/memory/YYYY-MM-DD.md`

Each file contains all conversations from that day, in the format:
```
User: [message]
Assistant: [reply]

User: [message]
Assistant: [reply]
```

## Finding past conversations

**The last 2 days** are already loaded in your system prompt (memory section above).

**For older conversations**, read the file directly:
```
exec: cat /root/.config/miniassistant/agent/memory/2026-03-10.md
```

Search by topic across multiple days:
```
exec: grep -i "code editor" /root/.config/miniassistant/agent/memory/*.md
```

List available days:
```
exec: ls /root/.config/miniassistant/agent/memory/
```

## Resolving relative date references

Use today's date from the **System section** of this prompt to calculate:
- "diesen Dienstag" → find the most recent Tuesday before or on today
- "letzte Woche" → subtract 7 days from today
- "gestern" → today minus 1 day

Weekday calculation: count backwards from today until you hit the target weekday.
Mo=0, Di=1, Mi=2, Do=3, Fr=4, Sa=5, So=6

Example: today is Sunday (So) 15.03.2026
- "diesen Dienstag" → last Tuesday before Sunday = 15 - 5 = **10.03.2026**
→ read `/root/.config/miniassistant/agent/memory/2026-03-10.md`

## Summarizing a past conversation

1. Resolve the date using today's date
2. Read the memory file for that day
3. Find the relevant conversation by searching for keywords
4. Summarize and write to workspace if asked
