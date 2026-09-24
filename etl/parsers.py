"""Parser für die unstrukturierten Felder der BAG-Dateien.

Die Versichererliste des BAG ist für den Druck gestaltet: Namen, Adressen und
Tätigkeitsgebiete stehen als Freitext mit Zeilenumbrüchen in einer Zelle, bei
einigen Versicherern (Groupe Mutuel, curaulta) sogar über mehrere Excel-Zeilen
verteilt. Die Funktionen hier machen daraus strukturierte Felder.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

KANTONE = [
    "AG", "AI", "AR", "BE", "BL", "BS", "FR", "GE", "GL", "GR", "JU", "LU", "NE",
    "NW", "OW", "SG", "SH", "SO", "SZ", "TG", "TI", "UR", "VD", "VS", "ZG", "ZH",
]

# Worttrennung beim Zeilenumbruch: «Gesundheits-\nversicherung» -> «Gesundheitsversicherung».
# Folgt ein Grossbuchstabe («Kranken-\nVersicherung»), bleibt der Bindestrich erhalten.
_LOWER = "a-zäöüéèàçâêô"
_SOFT_HYPHEN = re.compile(rf"([{_LOWER}])-\s*\n\s*([{_LOWER}])")
_HARD_HYPHEN = re.compile(rf"([{_LOWER}A-ZÄÖÜ])-\s*\n\s*([A-ZÄÖÜ])")
# Trennstrich aus dem Drucklayout innerhalb einer Zeile: «Unfallver-sicherung», «Genossen-schaft».
# Echte Bindestrich-Wörter wie «Assurance-maladie» bleiben erhalten.
_INLINE_HYPHEN = re.compile(rf"([{_LOWER}]{{2}})-(?!maladie|malattia)([{_LOWER}]{{2}})")


def clean_text(value) -> str:
    """Normalisiert Whitespace und entfernt Drucklayout-Trennungen."""
    if value is None:
        return ""
    text = str(value).replace("\r", "").replace("\u00a0", " ")
    text = _SOFT_HYPHEN.sub(r"\1\2", text)
    text = _HARD_HYPHEN.sub(r"\1-\2", text)
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if "@" not in line and "www" not in line.lower():
            line = _INLINE_HYPHEN.sub(r"\1\2", line)
        lines.append(line)
    return "\n".join(lines).strip()


def lines_of(value) -> list[str]:
    return [line for line in clean_text(value).split("\n") if line]


# --------------------------------------------------------------------------
# Versicherernamen DE / FR / IT
# --------------------------------------------------------------------------

_FR_WORDS = ("assurance", "caisse", "société", "maladie", "mutuelle", "fondation", "de la")
_IT_WORDS = ("assicurazion", "cassa", "malattia", "malattie", "assicurazioni", "società")
_DE_WORDS = ("versicherung", "kasse", "genossenschaft", "stiftung", " ag", "verein", "gesundheit")


def _language(segment: str) -> str | None:
    low = f" {segment.lower()} "
    if any(w in low for w in _IT_WORDS):
        return "it"
    if any(w in low for w in _FR_WORDS):
        return "fr"
    if any(w in low for w in _DE_WORDS) or low.rstrip().endswith(" ag"):
        return "de"
    return None


def split_names(raw) -> dict[str, str]:
    """Trennt den mehrsprachigen Namen in DE/FR/IT.

    Heuristik: Jede Sprachvariante beginnt mit dem Markennamen (erstes Wort der
    ersten Zeile); Zeilen ohne Markennamen gehören zur vorangehenden Variante.
    Fehlt eine Sprache, wird auf DE zurückgegriffen.
    """
    lines = lines_of(raw)
    if not lines:
        return {"de": "", "fr": "", "it": ""}
    brand = lines[0].split(" ")[0]
    segments: list[str] = []
    for line in lines:
        if segments and not line.startswith(brand):
            segments[-1] = f"{segments[-1]} {line}".strip()
        else:
            segments.append(line)
    # Duplikate (Folgezeilen aus mehrzeiligen Excel-Einträgen) entfernen, Reihenfolge behalten
    segments = list(dict.fromkeys(segments))

    names: dict[str, str] = {}
    for seg in segments:
        lang = _language(seg)
        if lang and lang not in names:
            names[lang] = seg
    fallback = names.get("de") or segments[0]
    return {
        "de": names.get("de", fallback),
        "fr": names.get("fr", fallback),
        "it": names.get("it", names.get("fr", fallback) if "fr" in names and "de" not in names else fallback),
    }


# --------------------------------------------------------------------------
# Adresse / Kontakt
# --------------------------------------------------------------------------

_PLZ_ORT = re.compile(r"^(\d{4})\s+(.+)$")
_STREET_WORDS = re.compile(
    r"(strasse|str\.|weg|gasse|platz|allee|quai|rain|rue|route|avenue|chemin|place|via)\b",
    re.IGNORECASE,
)


@dataclass
class Kontakt:
    strasse: str | None = None
    postfach: str | None = None
    plz: str | None = None
    ort: str | None = None
    tel: str | None = None
    fax: str | None = None
    email: str | None = None
    web: str | None = None
    zusatz: list[str] = field(default_factory=list)


def parse_kontakt(raw) -> Kontakt:
    k = Kontakt()
    for line in lines_of(raw):
        low = line.lower()
        if low.startswith("tel"):
            k.tel = re.sub(r"^tel\.?\s*", "", line, flags=re.IGNORECASE)
        elif low.startswith("fax"):
            k.fax = re.sub(r"^fax\.?\s*", "", line, flags=re.IGNORECASE)
        elif "@" in line and " " not in line:
            k.email = line
        elif low.startswith("www.") or low.startswith("http"):
            k.web = line if low.startswith("http") else f"https://{line}"
        elif low.startswith("postfach") or low.startswith("case postale"):
            k.postfach = line
        elif low.startswith("kontakt /"):
            continue  # Platzhalter «Kontakt / Contact / Contatto» (Kontaktformular auf der Website)
        elif (m := _PLZ_ORT.match(line)) and k.plz is None:
            k.plz, k.ort = m.group(1), m.group(2)
        elif k.plz is None and (_STREET_WORDS.search(line) or re.search(r"\s\d+[a-zA-Z]?$", line)):
            k.strasse = line
        else:
            k.zusatz.append(line)
    return k


# --------------------------------------------------------------------------
# Tätigkeitsgebiet
# --------------------------------------------------------------------------

@dataclass
class Taetigkeit:
    ganze_schweiz: bool = False
    kantone: set[str] = field(default_factory=set)
    # Kanton -> erlaubte Regionsnummern (nur wo auf Regionen eingeschränkt)
    regionen: dict[str, set[int]] = field(default_factory=dict)
    nur_beruf: bool = False  # reine Berufs-/Taggeldkassen
    ausland: list[str] = field(default_factory=list)

    def eintraege(self) -> list[tuple[str, int | None]]:
        """(Kanton, Regionsnummer|None) – None bedeutet ganzer Kanton."""
        rows: list[tuple[str, int | None]] = []
        kantone = set(KANTONE) if self.ganze_schweiz else set(self.kantone)
        for kt in sorted(kantone | set(self.regionen)):
            if kt in self.regionen and kt not in kantone:
                rows += [(kt, nr) for nr in sorted(self.regionen[kt])]
            else:
                rows.append((kt, None))
        return rows


def parse_taetigkeit(raw) -> Taetigkeit:
    t = Taetigkeit()
    inland = set(KANTONE) | {"CH", "FL"}
    for line in clean_text(raw).split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith(("beruf", "profession")):
            t.nur_beruf = True
            continue
        m = re.match(r"^([A-Z]{2}):\s*Region\s*(\d)", line)
        if m:
            t.regionen.setdefault(m.group(1), set()).add(int(m.group(2)))
            continue
        tokens = [tok.strip() for tok in re.split(r"[,;]", line) if tok.strip()]
        if not all(tok in inland for tok in tokens):
            t.ausland.append(line)  # EU/EFTA/UK oder einzelne Staaten (z.B. «AT, DE, FR»)
            continue
        for tok in tokens:
            if tok == "CH":
                t.ganze_schweiz = True
            elif tok == "FL":
                t.ausland.append(tok)
            else:
                t.kantone.add(tok)
    return t


# --------------------------------------------------------------------------
# Anpassungen (Fusionen / Bewilligungsentzüge)
# --------------------------------------------------------------------------

def parse_anpassung(text) -> dict:
    raw = clean_text(text)
    date = re.search(r"(\d{2}\.\d{2}\.\d{4})", raw)
    datum = None
    if date:
        d, m, y = date.group(1).split(".")
        datum = f"{y}-{m}-{d}"
    low = raw.lower()
    if "entzug" in low or "retrait" in low:
        return {"art": "entzug", "datum": datum, "ziel_bag_nr": None}
    if "zusammenschluss" in low or "fusion" in low:
        ziel = re.search(r"\b0*(\d{1,4})\s+\S", raw.split("\n")[-1])
        return {"art": "fusion", "datum": datum, "ziel_bag_nr": int(ziel.group(1)) if ziel else None}
    return {"art": "bisherige_angaben", "datum": datum, "ziel_bag_nr": None}
