# Prompt Engineering – Anleitung

**Was ist gemeint:** Das Schreiben von Anweisungen, Regeln oder System-Prompts für KI-Modelle/LLMs — also Texte die das Verhalten eines Assistenten steuern (basic_rules, docs, Systemkontext). Nicht gemeint: Shell-Prompts, Datenbankabfragen o.ä.

Wenn der User sagt "engineeren wir einen Prompt für X", "schreib mir Anweisungen für Y", "lass uns einen System-Prompt entwerfen", "wie sage ich dem Agenten dass er Z tun soll":

## Ablauf

1. **Ziel klären** (1-2 Fragen, nicht mehr): Was soll der Agent tun? Welche Fehler soll er vermeiden? Gibt es ein konkretes Beispiel-Szenario?
2. **Draft erstellen** in `{workspace}/prompt-THEMA-draft.md`
3. **Mit User iterieren** bis er zufrieden ist
4. **Ablegen** je nach Typ:
   - Verhaltensregel → `basic_rules/` (immer im Kontext, kurz halten!)
   - Referenzwissen / Howto → `docs/` (wird bei Bedarf geladen)
   - Nutzerspezifisch → `prefs/` (persönliche Daten, Vorlieben)
   - Template für den User → im Workspace lassen

## Worauf achten beim Schreiben

**Für lokale LLMs: kurz, konkret, mit Beispielen.**

- **Trigger explizit nennen:** Wann gilt die Regel? ("Wenn der User X sagt…", "Bei Voice-Nachrichten…")
- **Beispiele > Beschreibungen:** `"10-20" → "10 bis 20"` schlägt "ersetze Bereichs-Bindestriche durch das Wort bis"
- **Negativ-Beispiele** helfen: "NICHT so: …  SONDERN so: …"
- **Keine Redundanz:** Prüfe ob die Regel schon woanders steht (`grep -r "Stichwort" basic_rules/ docs/`)
- **Keine vagen Formulierungen:** "meistens", "normalerweise", "wenn sinnvoll" → LLMs ignorieren das
- **Ein Satz = eine Regel** — mehrere Regeln in einem Satz werden teils vergessen

## Typische Prompt-Strukturen

**Verhaltensregel** (basic_rules):
```
**[Titel].** [Wann gilt es.] [Was tun.] [Beispiel.] [Was NICHT tun.]
```

**Howto-Guide** (docs):
```
# Titel
## Wann lesen?
[Trigger-Satz]
## Ablauf
1. ...
## Beispiel
```

## Qualitätscheck vor dem Speichern

- Würde ein lokales 7B-Modell das verstehen?
- Ist klar wann die Regel greift und wann nicht?
- Gibt es ein konkretes Beispiel?
- Ist sie so kurz wie möglich?
