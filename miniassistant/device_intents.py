"""Deterministischer Fast-Path für Geräte-Steuerbefehle (Echo Show etc.).

Der 35B-Orchestrator ruft device_action unzuverlässig auf (greift reflexartig zu
exec/curl/adb). Für die häufigsten, eindeutigen Sprachbefehle matchen wir hier
regex-basiert und liefern die device_action direkt — modellunabhängig. Alles
andere (komplexe/mehrdeutige Wünsche) geht weiter an den Agenten.

match() gibt (actions, reply) zurück oder None, wenn kein Fast-Path greift.
"""
from __future__ import annotations

import re

# App-Label → Android-Paketname. Erweiterbar; unbekannte Namen gehen als Label
# durch (die App matcht dann selbst per installierter Label-Liste).
_APP_PACKAGES = {
    "newpipe": "org.schabi.newpipe",
    "youtube": "org.schabi.newpipe",       # auf LineageOS ohne GApps: NewPipe
    "musik": "org.schabi.newpipe",
    "music": "org.schabi.newpipe",
    "spotify": "com.spotify.music",
    "vlc": "org.videolan.vlc",
    "browser": "org.lineageos.jelly",
    "firefox": "org.mozilla.firefox",
    "kamera": "org.lineageos.aperture",
    "camera": "org.lineageos.aperture",
    "einstellungen": "com.android.settings",
    "settings": "com.android.settings",
}


def _action(action: str, arg: str = "") -> dict:
    return {"action": action, "arg": arg}


def match(text: str) -> tuple[list[dict], str] | None:
    """Erkennt eindeutige Gerätebefehle. Sprache: DE + etwas EN."""
    t = text.lower().strip()
    t = re.sub(r"^\[voice\]\s*", "", t)
    t = t.strip(" .!?,")

    # --- Lautstärke ---
    if re.search(r"\b(lauter|louder|volume up|mach.*laut)\b", t):
        return [_action("volume", "up")], "Mache lauter."
    if re.search(r"\b(leiser|quieter|volume down|mach.*leise)\b", t):
        return [_action("volume", "down")], "Mache leiser."
    m = re.search(r"(?:lautst[äa]rke|volume)\D*(\d{1,3})\s*(?:%|prozent)?", t)
    if m:
        pct = min(100, int(m.group(1)))
        return [_action("volume", str(pct))], f"Lautstärke auf {pct} Prozent."

    # --- Media-Transport ---
    if re.search(r"\b(pause|pausier|anhalten|stopp? die musik|stop music)\b", t):
        return [_action("media", "pause")], "Pausiert."
    if re.search(r"\b(weiter(spielen)?|fortsetzen|resume|play again)\b", t):
        return [_action("media", "play")], "Spiele weiter."
    if re.search(r"\b(n[äa]chste(r|s)? (lied|song|titel|track)|skip|weiter zum n[äa]chsten|next)\b", t):
        return [_action("media", "next")], "Nächster Titel."
    if re.search(r"\b(vorherige(r|s)?|zur[üu]ck zum|previous|letzter titel)\b", t):
        return [_action("media", "prev")], "Vorheriger Titel."

    # --- Stream stoppen ---
    if re.search(r"\b(stopp?|beende|halt|stop)\b.*\b(radio|stream|wiedergabe|playback|musik)\b", t) \
            or re.search(r"\b(radio|stream) (aus|stopp?|beenden)\b", t):
        return [_action("stop_playback")], "Wiedergabe gestoppt."

    # --- App öffnen ---
    m = re.search(r"\b(?:[öo]ffne|starte|open|start|mach.*auf)\s+(?:die\s+|das\s+|den\s+|the\s+|app\s+)*([a-zäöü0-9 .\-]+)", t)
    if m:
        name = m.group(1).strip().strip(" .")
        name = re.sub(r"\b(app|auf|bitte|mal|jetzt|die|das|den|der|the)\b", "", name)
        name = re.sub(r"\s+", " ", name).strip()
        if name:
            key = name.replace(" ", "")
            pkg = _APP_PACKAGES.get(key) or _APP_PACKAGES.get(name)
            arg = pkg or name
            label = name.capitalize()
            return [_action("open_app", arg)], f"Öffne {label}."

    return None
