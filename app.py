"""
app.py - Motore di ricerca su indice OpenGA (database Postgres).

NIENTE PIU' link diretto indovinato alla sentenza - si e' rivelato
inaffidabile (funziona per pochi casi verificati per caso, fallisce sulla
maggior parte). Al suo posto:
  - Il sistema mostra citazione, oggetto e esito (dati sempre affidabili,
    vengono dal database).
  - Un link alla RICERCA del portale (non alla sentenza diretta), con
    sede e numero mostrati chiaramente da copiare nella ricerca.
  - Un pulsante "Scrivi la massima": incolli tu il testo della sentenza
    (trovata sul portale), il sistema scrive la bozza di massima.

Questo NON genera piu' massime in automatico per ogni risultato - serve
il testo, che ora va incollato a mano.
"""

import os
import re
import unicodedata

from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
import anthropic

from db import get_conn

app = FastAPI()
claude_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

RICERCA_PORTALE_URL = "https://www.giustizia-amministrativa.it/web/guest/dcsnprr"

ORD = {"PRIMA": "I", "SECONDA": "II", "TERZA": "III", "QUARTA": "IV", "QUINTA": "V",
       "SESTA": "VI", "SETTIMA": "VII", "OTTAVA": "VIII", "NONA": "IX", "DECIMA": "X"}

MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]

MAX_CHARS = 50000

PRETTY_SEDE = {
    "CDS": "Consiglio di Stato",
    "TAR-ABRUZZO-L-AQUILA": "TAR Abruzzo - L'Aquila",
    "TAR-ABRUZZO-PESCARA": "TAR Abruzzo - Pescara",
    "TAR-BASILICATA": "TAR Basilicata",
    "TAR-CALABRIA-CATANZARO": "TAR Calabria - Catanzaro",
    "TAR-CALABRIA-REGGIO-CALABRIA": "TAR Calabria - Reggio Calabria",
    "TAR-CAMPANIA-NAPOLI": "TAR Campania - Napoli",
    "TAR-CAMPANIA-SALERNO": "TAR Campania - Salerno",
    "TAR-EMILIA-ROMAGNA-BOLOGNA": "TAR Emilia Romagna - Bologna",
    "TAR-EMILIA-ROMAGNA-PARMA": "TAR Emilia Romagna - Parma",
    "TAR-FRIULI-VENEZIA-GIULIA": "TAR Friuli Venezia Giulia",
    "TAR-LAZIO-LATINA": "TAR Lazio - Latina",
    "TAR-LAZIO-ROMA": "TAR Lazio - Roma",
    "TAR-LIGURIA": "TAR Liguria",
    "TAR-LOMBARDIA-BRESCIA": "TAR Lombardia - Brescia",
    "TAR-LOMBARDIA-MILANO": "TAR Lombardia - Milano",
    "TAR-MARCHE": "TAR Marche",
    "TAR-MOLISE": "TAR Molise",
    "TAR-PIEMONTE": "TAR Piemonte",
    "TAR-PUGLIA-BARI": "TAR Puglia - Bari",
    "TAR-PUGLIA-LECCE": "TAR Puglia - Lecce",
    "TAR-SARDEGNA": "TAR Sardegna",
    "TAR-SICILIA-PALERMO": "TAR Sicilia - Palermo",
    "TAR-SICILIA-CATANIA": "TAR Sicilia - Catania",
    "TAR-TOSCANA": "TAR Toscana",
    "TRGA-TRENTO": "TRGA - Trento",
    "TRGA-BOLZANO": "TRGA - Bolzano",
    "TAR-UMBRIA": "TAR Umbria",
    "TAR-VALLE-D-AOSTA": "TAR Valle d'Aosta",
    "TAR-VENETO": "TAR Veneto",
    "CGA-SICILIA": "CGA Sicilia",
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower()


def pretty_sede(sede: str) -> str:
    return PRETTY_SEDE.get(sede, sede)


def sez_short(z: str) -> str:
    if (z or "").upper() == "PLENARIA":
        return "Ad. plen."
    m = re.match(r"^SEZIONE\s+(.+)$", z or "", re.IGNORECASE)
    if not m:
        return z or ""
    v = m.group(1).upper()
    return "sez. " + ORD.get(v, v if re.match(r"^[IVX]+$", v) else m.group(1).lower())


def citation(row: dict) -> str:
    court = "Cons. Stato" if row["sede"] == "CDS" else pretty_sede(row["sede"]).replace("TAR", "T.A.R.").replace("TRGA", "T.R.G.A.")
    d = row["data_pubblicazione"]
    date_it = f"{d.day} {MESI[d.month - 1]} {d.year}" if d else "data sconosciuta"
    numero = int(row["numero_provvedimento"][4:]) if row["numero_provvedimento"] else "?"
    return f"{court}, {sez_short(row['sezione'])}, {date_it}, n. {numero}"


def search_index(q: str, n: int) -> list[dict]:
    words = [w for w in re.split(r"\s+", norm(q)) if w and w not in ("n", "nr", "n.", "nr.", "numero")]
    num_matches = [w for w in words if re.match(r"^\d+/\d{4}$", w)]
    text_words = [w for w in words if w not in num_matches]

    conn = get_conn()
    try:
        if num_matches:
            numero, anno = num_matches[0].split("/")
            provv = anno + numero.zfill(5)
            rows = conn.run("""
                SELECT sede, sezione, numero_provvedimento, numero_ricorso, data_pubblicazione,
                       esito, tipo_ricorso, oggetto_ricorso, tipo_provvedimento
                FROM decisioni
                WHERE numero_provvedimento = :provv
                   OR numero_ricorso LIKE :ricorso_pattern
                ORDER BY data_pubblicazione DESC LIMIT :n;
            """, provv=provv, ricorso_pattern=f"{anno}%{numero.zfill(5)}", n=n)
        else:
            rows = conn.run("""
                SELECT sede, sezione, numero_provvedimento, numero_ricorso, data_pubblicazione,
                       esito, tipo_ricorso, oggetto_ricorso, tipo_provvedimento
                FROM decisioni
                WHERE search_vector @@ plainto_tsquery('italian', :q)
                ORDER BY ts_rank(search_vector, plainto_tsquery('italian', :q)) DESC,
                         data_pubblicazione DESC
                LIMIT :n;
            """, q=" ".join(text_words) or q, n=n)
        columns = [c["name"] for c in conn.columns]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        conn.close()


@app.get("/search")
def search(q: str, n: int = 5):
    matches = search_index(q, n)
    risultati = []
    for row in matches:
        numero = int(row["numero_provvedimento"][4:]) if row["numero_provvedimento"] else None
        anno = row["numero_provvedimento"][:4] if row["numero_provvedimento"] else None
        risultati.append({
            "citazione": citation(row),
            "oggetto": (row["oggetto_ricorso"] or "").capitalize(),
            "esito": (row["esito"] or "").capitalize(),
            "tipo_ricorso": (row["tipo_ricorso"] or "").capitalize(),
            "sede_da_cercare": pretty_sede(row["sede"]),
            "numero_da_cercare": f"{numero}/{anno}" if numero else None,
            "ricerca_portale_url": RICERCA_PORTALE_URL,
        })
    return JSONResponse({"query": q, "risultati": risultati})


class MassimaRequest(BaseModel):
    citazione: str
    oggetto: str = ""
    tipo_ricorso: str = ""
    esito: str = ""
    testo: str


def build_massima_prompt(req: MassimaRequest, text: str, cut: bool) -> str:
    return (
        "Sei un assistente che prepara bozze di massime giurisprudenziali per uno studio legale. "
        "Lavori solo sul testo della decisione fornito qui sotto.\n\n"
        f"DATI DELLA DECISIONE (dall'indice OpenGA):\n{req.citazione}\n"
        f"Tipo di ricorso: {req.tipo_ricorso}\nEsito indicato: {req.esito}\nOggetto del ricorso: {req.oggetto}\n\n"
        "COMPITO:\nScrivi la massima nello stile del massimario: 2-4 frasi che enunciano il principio di "
        "diritto affermato dal collegio, in forma astratta, senza nomi di parti. Usa solo ciò che è scritto "
        "nel testo: non aggiungere norme, sentenze o principi che nel testo non compaiono.\n\n"
        "FORMATO: testo semplice, senza Markdown. Quattro sezioni:\n"
        "Massima:\n(la massima)\n\nRiferimenti:\n(norme/precedenti citati, o \u00abnessuno indicato\u00bb)\n\n"
        "Dove verificarla:\n(punto del testo, frase breve tra virgolette, max 25 parole)\n\n"
        "Avvertenze:\n(incongruenze coi dati forniti, se e' una decisione di rito, se il testo e' incompleto; "
        "altrimenti \u00abnessuna\u00bb)\n\n"
        + ("NOTA: il testo e' stato accorciato per lunghezza.\n\n" if cut else "")
        + f"TESTO DELLA DECISIONE:\n{text}"
    )


def fit(text: str):
    text = re.sub(r"[ \t]+", " ", text.replace("\r", ""))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= MAX_CHARS:
        return text, False
    head = 6000
    return text[:head] + "\n[... parte omessa per lunghezza ...]\n" + text[-(MAX_CHARS - head):], True


@app.post("/massima")
def massima(req: MassimaRequest):
    if len(req.testo.strip()) < 300:
        return JSONResponse({"errore": "Testo troppo corto - incolla il testo completo della decisione."}, status_code=400)
    fitted, cut = fit(req.testo)
    try:
        response = claude_client.messages.create(
            model="claude-sonnet-4-6", max_tokens=700,
            messages=[{"role": "user", "content": build_massima_prompt(req, fitted, cut)}],
        )
        testo_massima = "".join(b.text for b in response.content if b.type == "text")
        return JSONResponse({"massima": testo_massima})
    except Exception as e:
        return JSONResponse({"errore": str(e)}, status_code=500)


@app.get("/")
def home():
    with open("static/index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())
