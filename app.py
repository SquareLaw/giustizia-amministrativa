"""
app.py - Motore di ricerca su indice OpenGA (database Postgres) con link
alla sentenza vera e massima scritta automaticamente da Claude.

Rispetto alla prima versione (data.json bundle), ora legge da un database
Postgres popolato da ingest_openga.py - copre tutte le sedi TAR + CDS,
non solo le due caricate a mano all'inizio.

ATTENZIONE - da leggere prima di usarlo su scala:
Le regole di accesso della Giustizia amministrativa vietano l'accesso
massivo ai singoli provvedimenti per fini commerciali, e un altro indirizzo
del sito vieta esplicitamente gli accessi automatici. Non verificato con un
parere legale.
"""

import os
import re
import unicodedata

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse
import anthropic

from db import get_conn

app = FastAPI()
claude_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Schema dell'indirizzo per sede, usato per costruire il link alla sentenza.
# "confermato" = trovato su un indirizzo vero durante le ricerche di oggi.
# "dedotto" = segue la stessa convenzione (sigla provincia italiana) delle
# sedi confermate, MAI verificato su un indirizzo vero - puo' sbagliare.
SEDE_SCHEMA = {
    "CDS": "cds",                                    # confermato
    "TAR-ABRUZZO-L-AQUILA": "tar_aq",                 # confermato
    "TAR-ABRUZZO-PESCARA": "tar_pe",                  # dedotto
    "TAR-BASILICATA": "tar_pz",                       # dedotto (Potenza)
    "TAR-CALABRIA-CATANZARO": "tar_cz",               # dedotto
    "TAR-CALABRIA-REGGIO-CALABRIA": "tar_rc",         # dedotto
    "TAR-CAMPANIA-NAPOLI": "tar_na",                  # confermato
    "TAR-CAMPANIA-SALERNO": "tar_sa",                 # dedotto
    "TAR-EMILIA-ROMAGNA-BOLOGNA": "tar_bo",           # dedotto
    "TAR-EMILIA-ROMAGNA-PARMA": "tar_pr",             # dedotto
    "TAR-FRIULI-VENEZIA-GIULIA": "tar_ts",            # dedotto (Trieste)
    "TAR-LAZIO-LATINA": "tar_lt",                     # dedotto
    "TAR-LAZIO-ROMA": "tar_rm",                       # confermato
    "TAR-LIGURIA": "tar_ge",                          # dedotto (Genova)
    "TAR-LOMBARDIA-BRESCIA": "tar_bs",                # dedotto
    "TAR-LOMBARDIA-MILANO": "tar_mi",                 # dedotto
    "TAR-MARCHE": "tar_an",                           # dedotto (Ancona)
    "TAR-MOLISE": "tar_cb",                           # dedotto (Campobasso)
    "TAR-PIEMONTE": "tar_to",                         # dedotto (Torino)
    "TAR-PUGLIA-BARI": "tar_ba",                      # dedotto
    "TAR-PUGLIA-LECCE": "tar_le",                     # dedotto (visto in un indirizzo, non confermato con certezza)
    "TAR-SARDEGNA": "tar_ca",                         # dedotto (Cagliari)
    "TAR-SICILIA-PALERMO": "tar_pa",                  # confermato
    "TAR-SICILIA-CATANIA": "tar_ct",                  # dedotto
    "TAR-TOSCANA": "tar_fi",                          # dedotto (Firenze)
    "TRGA-TRENTO": "tar_tn",                          # dedotto
    "TRGA-BOLZANO": "tar_bz",                         # confermato
    "TAR-UMBRIA": "tar_pg",                           # dedotto (Perugia)
    "TAR-VALLE-D-AOSTA": "tar_ao",                    # dedotto
    "TAR-VENETO": "tar_ve",                           # dedotto (Venezia)
    # CGA-SICILIA: schema sconosciuto, non e' un TAR - nessun link finche' non si trova un esempio vero
}

ORD = {"PRIMA": "I", "SECONDA": "II", "TERZA": "III", "QUARTA": "IV", "QUINTA": "V",
       "SESTA": "VI", "SETTIMA": "VII", "OTTAVA": "VIII", "NONA": "IX", "DECIMA": "X"}

MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]

MAX_CHARS = 50000


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower()


def pretty_sede(sede: str) -> str:
    return PRETTY_SEDE.get(sede, sede)


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


def portal_links(row: dict):
    schema = SEDE_SCHEMA.get(row["sede"])
    if not schema or (row.get("tipo_provvedimento") or "").upper() != "SENTENZA":
        return None
    code = "11" if schema == "cds" else "01"
    n = row["numero_provvedimento"]
    r = row["numero_ricorso"] or ""
    base = f"https://mdp.giustizia-amministrativa.it/visualizza/?nodeRef=&schema={schema}&nrg={r}&nomeFile={n}_{code}"
    return {"html": base + ".html&subDir=Provvedimenti", "xml": base + ".xml&subDir=Provvedimenti"}


def fetch_text(url: str) -> str:
    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (compatible; RicercaOpenGA/0.1)"})
    resp.raise_for_status()
    is_xml = url.split("nomeFile=")[1].split("&")[0].endswith(".xml")
    soup = BeautifulSoup(resp.content, "xml" if is_xml else "html.parser")
    if soup.find(string=re.compile("pagina non trovata", re.IGNORECASE)):
        raise ValueError("Documento non trovato a questo indirizzo")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text(separator="\n")).strip()
    if len(text) < 300:
        raise ValueError("Testo troppo corto o pagina non riconosciuta")
    return text


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
            query_text = " & ".join(text_words) if text_words else q
            rows = conn.run("""
                SELECT sede, sezione, numero_provvedimento, numero_ricorso, data_pubblicazione,
                       esito, tipo_ricorso, oggetto_ricorso, tipo_provvedimento
                FROM decisioni
                WHERE search_vector @@ plainto_tsquery('italian', :q)
                ORDER BY data_pubblicazione DESC LIMIT :n;
            """, q=" ".join(text_words) or q, n=n)
        columns = [c["name"] for c in conn.columns]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        conn.close()


def build_massima_prompt(row: dict, text: str, cut: bool) -> str:
    return (
        "Sei un assistente che prepara bozze di massime giurisprudenziali per uno studio legale. "
        "Lavori solo sul testo della decisione fornito qui sotto.\n\n"
        f"DATI DELLA DECISIONE (dall'indice OpenGA):\n{citation(row)}\n"
        f"Tipo di ricorso: {(row['tipo_ricorso'] or '').capitalize()}\n"
        f"Esito indicato: {(row['esito'] or '').capitalize()}\n"
        f"Oggetto del ricorso: {(row['oggetto_ricorso'] or '').capitalize()}\n\n"
        "COMPITO:\nScrivi la massima nello stile del massimario: 2-4 frasi che enunciano il principio di "
        "diritto affermato dal collegio, in forma astratta, senza nomi di parti. Usa solo ciò che è scritto "
        "nel testo: non aggiungere norme, sentenze o principi che nel testo non compaiono.\n\n"
        "FORMATO: testo semplice, senza Markdown e senza asterischi. Quattro sezioni, ciascuna con il "
        "titolo su una riga:\n"
        "Massima:\n(la massima)\n\n"
        "Riferimenti:\n(norme e precedenti citati nel testo; se non ce ne sono scrivi \u00abnessuno indicato\u00bb)\n\n"
        "Dove verificarla:\n(il punto del testo, con una frase breve copiata tra virgolette, max 25 parole)\n\n"
        "Avvertenze:\n(segnala incongruenze coi dati, se e' una decisione di rito, se il testo e' accorciato; "
        "altrimenti scrivi \u00abnessuna\u00bb)\n\n"
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


@app.get("/search")
def search(q: str, n: int = 5):
    matches = search_index(q, n)
    risultati = []

    for row in matches:
        entry = {
            "citazione": citation(row),
            "oggetto": (row["oggetto_ricorso"] or "").capitalize(),
            "esito": (row["esito"] or "").capitalize(),
            "tipo_ricorso": (row["tipo_ricorso"] or "").capitalize(),
        }
        links = portal_links(row)
        if not links:
            entry["link"] = None
            entry["errore"] = "Indirizzo non disponibile (sentenza breve, sede non ancora mappata, o schema dedotto non confermato)."
            risultati.append(entry)
            continue

        entry["link"] = links["html"]
        entry["link_xml"] = links["xml"]

        text = None
        for url in (links["xml"], links["html"]):
            try:
                text = fetch_text(url)
                break
            except Exception as e:
                entry["errore_lettura"] = str(e)

        if not text:
            entry["massima"] = None
            risultati.append(entry)
            continue

        fitted, cut = fit(text)
        try:
            response = claude_client.messages.create(
                model="claude-sonnet-4-6", max_tokens=700,
                messages=[{"role": "user", "content": build_massima_prompt(row, fitted, cut)}],
            )
            entry["massima"] = "".join(b.text for b in response.content if b.type == "text")
            entry.pop("errore_lettura", None)
        except Exception as e:
            entry["massima"] = None
            entry["errore_massima"] = str(e)

        risultati.append(entry)

    return JSONResponse({"query": q, "risultati": risultati})


@app.get("/")
def home():
    with open("static/index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())
