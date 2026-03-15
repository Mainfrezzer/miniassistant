## Long-running tracking & recurring data

When the user wants to track something over time (calories, expenses, weight, habits, journal, …):

**Always use a dedicated subfolder:**
```
workspace/kalorien/      ← one folder per topic
workspace/fitness/
workspace/ausgaben/
```
Never mix tracking files with other workspace content.

**File structure — split by month from day one:**
```
workspace/kalorien/2026-03.md
workspace/kalorien/2026-04.md
workspace/kalorien/_index.md   ← monthly totals/summaries only
```
Never put everything in one flat file. One file per month keeps files small forever.

**Setup on first use:**
1. Create the folder and first monthly file + `_index.md`
2. **Save to prefs immediately:** tracking topic, folder path, what is tracked, unit (e.g. kcal, €, kg), active monthly file
3. Offer to set up a daily `schedule` reminder if it makes sense

**Resuming tracking (new session):**
1. Read prefs to know folder and topic
2. `ls -1t workspace/{folder}/` — shows all files, newest first — to find the current month's file
3. `grep "$(date +%d.%m.%Y)" workspace/{folder}/YYYY-MM.md` to check if today already has entries

**Appending entries — always append, never rewrite:**
```bash
echo "- 14.03.2026 Schnitzel mit Pommes: 650 kcal" >> workspace/kalorien/2026-03.md
```

**Reading entries — grep, never cat:**
```bash
grep "14.03.2026" workspace/kalorien/2026-03.md   # specific day
grep -h "" workspace/kalorien/2026-03.md           # whole month if needed (small file)
```

**Index file** (`_index.md`): append one line per month with the total/summary. Never recalculate the whole history — only the current month.

**Calorie lookups:** use your own knowledge for common foods. Only `web_search` for unusual items or when the user asks for exact values.
