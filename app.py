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

Ricerca (nessun uso di Claude, nessun costo in token):
  1. Doppioni: le decisioni con oggetto uguale o quasi uguale (contenzioso
     seriale) vengono raggruppate; ogni risultato porta con se' l'elenco
     "simili".
  2. Filtri: sede, sezione, tipo di ricorso, esito, date.
  3. Sintassi di ricerca: "frase esatta", parola1 or parola2, -parola
     (websearch_to_tsquery di Postgres).
  4. Errori di battitura: se non si trova nulla, le parole sconosciute
     vengono corrette con la parola piu' simile del vocabolario (pg_trgm).
     Il vocabolario si prepara da solo all'avvio dell'app (vedi
     prepara_vocabolario) e si aggiorna quando cambia il numero di decisioni.
     Lo stato si controlla all'indirizzo /stato.
"""

import os
import re
import threading
import unicodedata
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
import anthropic

from db import get_conn



# ---------- preparazione automatica del vocabolario (per la correzione degli errori) ----------
# Stato visibile all'indirizzo /stato, utile per capire se la correzione e' attiva.
STATO_VOCABOLARIO = {"stato": "non ancora avviato", "parole": 0}


def _scrivi(msg):
    # print con flush: compare subito nei log di Render
    print(f"[vocabolario] {msg}", flush=True)


def prepara_vocabolario():
    """Prepara il vocabolario delle parole presenti negli oggetti dei ricorsi.
    - Gira in sottofondo: la ricerca funziona anche mentre lavora.
    - Se il numero di decisioni non e' cambiato dall'ultima volta, non rifa' nulla.
    - Costruisce la versione nuova in una tabella a parte e la scambia con la
      vecchia all'ultimo istante: le ricerche non restano mai bloccate.
    - Se qualcosa va storto, la correzione degli errori resta disattivata e il
      motivo compare nei log e in /stato."""
    conn = None
    try:
        STATO_VOCABOLARIO["stato"] = "in preparazione"
        _scrivi("avvio preparazione")
        conn = get_conn()
        # Durante un deploy Render tiene acceso per qualche secondo anche il
        # processo vecchio: aspettiamo che abbia finito invece di rinunciare.
        conn.run("SELECT pg_advisory_lock(7310001);")
        conn.run("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
        conn.run("CREATE TABLE IF NOT EXISTS vocabolario_info (chiave text PRIMARY KEY, valore bigint NOT NULL);")

        decisioni = conn.run("SELECT COUNT(*) FROM decisioni;")[0][0]
        esiste = conn.run("SELECT to_regclass('public.vocabolario') IS NOT NULL;")[0][0]
        fatto = conn.run("SELECT valore FROM vocabolario_info WHERE chiave = 'decisioni';")
        if esiste and fatto and fatto[0][0] == decisioni:
            n = conn.run("SELECT COUNT(*) FROM vocabolario;")[0][0]
            if n > 0:
                STATO_VOCABOLARIO.update(stato="pronto", parole=n)
                _scrivi(f"gia' pronto ({n} parole, {decisioni} decisioni): nessun aggiornamento necessario")
                return

        _scrivi(f"costruzione in corso su {decisioni} decisioni...")
        conn.run("DROP TABLE IF EXISTS vocabolario_nuovo;")
        conn.run("""
            CREATE TABLE vocabolario_nuovo AS
            SELECT parola, COUNT(*)::integer AS frequenza
            FROM (
                SELECT regexp_split_to_table(lower(oggetto_ricorso), '[^[:alpha:]]+') AS parola
                FROM decisioni
            ) t
            WHERE length(parola) >= 4
            GROUP BY parola;
        """)
        conn.run("ALTER TABLE vocabolario_nuovo ADD PRIMARY KEY (parola);")
        conn.run("CREATE INDEX vocabolario_nuovo_trgm ON vocabolario_nuovo USING gin (parola gin_trgm_ops);")

        # Scambio istantaneo tra vecchio e nuovo
        conn.run("START TRANSACTION;")
        conn.run("DROP TABLE IF EXISTS vocabolario;")
        conn.run("ALTER TABLE vocabolario_nuovo RENAME TO vocabolario;")
        conn.run("ALTER INDEX vocabolario_nuovo_trgm RENAME TO vocabolario_trgm;")
        conn.run("""
            INSERT INTO vocabolario_info (chiave, valore) VALUES ('decisioni', :d)
            ON CONFLICT (chiave) DO UPDATE SET valore = EXCLUDED.valore;
        """, d=decisioni)
        conn.run("COMMIT;")

        n = conn.run("SELECT COUNT(*) FROM vocabolario;")[0][0]
        STATO_VOCABOLARIO.update(stato="pronto", parole=n)
        _scrivi(f"pronto: {n} parole")
    except Exception as e:
        STATO_VOCABOLARIO.update(stato=f"errore: {e}", parole=0)
        _scrivi(f"NON preparato, la correzione degli errori resta disattivata. Motivo: {e}")
        try:
            if conn:
                conn.run("ROLLBACK;")
        except Exception:
            pass
    finally:
        if conn:
            try:
                conn.run("SELECT pg_advisory_unlock_all();")
                conn.close()
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=prepara_vocabolario, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)
claude_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

RICERCA_PORTALE_URL = "https://www.giustizia-amministrativa.it/web/guest/dcsnprr"

ORD = {"PRIMA": "I", "SECONDA": "II", "TERZA": "III", "QUARTA": "IV", "QUINTA": "V",
       "SESTA": "VI", "SETTIMA": "VII", "OTTAVA": "VIII", "NONA": "IX", "DECIMA": "X"}

MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]

MAX_CHARS = 50000

# Quante righe chiedere al database prima di raggruppare i doppioni.
CANDIDATI = 300
MAX_RISULTATI = 50
MAX_SIMILI = 50

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

# Filtro "tipo di ricorso": condizione SQL sul campo tipo_ricorso.
TIPI = {
    "appalti": "tipo_ricorso ILIKE '%rito appalti%'",
    "accesso": "tipo_ricorso ILIKE '%accesso%'",
    "silenzio": "tipo_ricorso ILIKE '%silenzio%'",
    "ottemperanza": "(tipo_ricorso ILIKE '%ottemperanza%' OR tipo_ricorso ILIKE '%inottemp%')",
}

# Filtro "esito": stessi gruppi usati per colorare l'esito nella pagina.
_PARZIALE = "(esito ~* 'PARZIAL|PARTE RIGETTA E PARTE ACCOGLIE')"
ESITI = {
    "parziale": _PARZIALE,
    "accoglie": f"(esito ~* '^(ACCOGLI|ACCOLT)' AND NOT {_PARZIALE})",
    "respinge": f"(esito ~* '^(RESPING|RESPINT|RIGETT)' AND NOT {_PARZIALE})",
    "nomerito": "(esito ~* 'INAMMISSIB|IMPROCEDIB|IRRICEVIB|ESTINT|CESSATA MATERIA|PERENZ|RINUNC|RINUNZ|DIFETTO DI GIURISDIZIONE|INCOMPETENZ')",
}

ROMANI = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}

COLONNE = """sede, sezione, numero_provvedimento, numero_ricorso, data_pubblicazione,
             esito, tipo_ricorso, oggetto_ricorso, tipo_provvedimento"""


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


def pretty_sez(z: str) -> str:
    if (z or "").upper() == "PLENARIA":
        return "Adunanza plenaria"
    m = re.match(r"^SEZIONE\s+(.+)$", z or "", re.IGNORECASE)
    if not m:
        return z or ""
    v = m.group(1).upper()
    return "Sezione " + ORD.get(v, v if re.match(r"^[IVX]+$", v) else m.group(1).lower())


def citation(row: dict) -> str:
    court = "Cons. Stato" if row["sede"] == "CDS" else pretty_sede(row["sede"]).replace("TAR", "T.A.R.").replace("TRGA", "T.R.G.A.")
    d = row["data_pubblicazione"]
    date_it = f"{d.day} {MESI[d.month - 1]} {d.year}" if d else "data sconosciuta"
    numero = int(row["numero_provvedimento"][4:]) if row["numero_provvedimento"] else "?"
    return f"{court}, {sez_short(row['sezione'])}, {date_it}, n. {numero}"


def _data(s: str):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def filtri_sql(sede="", sezione="", tipo="", esito="", dal="", al="", solo_sede=False):
    """Restituisce (lista di condizioni SQL, parametri) per i filtri scelti."""
    cond, par = [], {}
    if sede:
        cond.append("sede = :f_sede"); par["f_sede"] = sede
    if solo_sede:
        return cond, par
    if sezione:
        cond.append("sezione = :f_sez"); par["f_sez"] = sezione
    if tipo in TIPI:
        cond.append(TIPI[tipo])
    if esito in ESITI:
        cond.append(ESITI[esito])
    if _data(dal):
        cond.append("data_pubblicazione >= :f_dal"); par["f_dal"] = _data(dal)
    if _data(al):
        cond.append("data_pubblicazione <= :f_al"); par["f_al"] = _data(al)
    return cond, par


def _righe(conn, sql, **par):
    rows = conn.run(sql, **par)
    columns = [c["name"] for c in conn.columns]
    return [dict(zip(columns, r)) for r in rows]


def cerca_testo(conn, testo: str, filtri: dict) -> list[dict]:
    """Ricerca testuale con la sintassi di websearch_to_tsquery:
    "frase esatta", parola1 or parola2, -parola da escludere."""
    cond, par = filtri_sql(**filtri)
    where = " AND ".join(["search_vector @@ websearch_to_tsquery('italian', :q)"] + cond)
    return _righe(conn, f"""
        SELECT {COLONNE},
               ts_rank(search_vector, websearch_to_tsquery('italian', :q)) AS rank
        FROM decisioni
        WHERE {where}
        ORDER BY rank DESC, data_pubblicazione DESC
        LIMIT :lim;
    """, q=testo, lim=CANDIDATI, **par)


def cerca_numero(conn, numero_anno: str, filtri: dict) -> list[dict]:
    numero, anno = numero_anno.split("/")
    provv = anno + numero.zfill(5)
    cond, par = filtri_sql(solo_sede=True, **{k: v for k, v in filtri.items() if k == "sede"})
    extra = (" AND " + " AND ".join(cond)) if cond else ""
    return _righe(conn, f"""
        SELECT {COLONNE}, 1.0 AS rank
        FROM decisioni
        WHERE (numero_provvedimento = :provv OR numero_ricorso LIKE :ricorso_pattern){extra}
        ORDER BY data_pubblicazione DESC
        LIMIT :lim;
    """, provv=provv, ricorso_pattern=f"{anno}%{numero.zfill(5)}", lim=CANDIDATI, **par)


# ---------- 4. errori di battitura ----------
OPERATORI = {"or"}


def correggi(conn, testo: str) -> dict:
    """Per ogni parola che non esiste nel vocabolario cerca la piu' simile.
    Restituisce {parola_scritta: parola_corretta}. Se la tabella vocabolario
    non c'e' (migrazione non eseguita) non corregge nulla."""
    correzioni = {}
    parole = {p.lower() for p in re.findall(r"[^\W\d_]{4,}", testo)} - OPERATORI
    try:
        for p in parole:
            if conn.run("SELECT 1 FROM vocabolario WHERE parola = :p", p=p):
                continue
            r = conn.run("""
                SELECT parola FROM vocabolario
                WHERE parola % :p
                ORDER BY similarity(parola, :p) DESC, frequenza DESC
                LIMIT 1;
            """, p=p)
            if r and r[0][0] != p:
                correzioni[p] = r[0][0]
    except Exception:
        return {}
    return correzioni


def applica(testo: str, correzioni: dict) -> str:
    for sbagliata, giusta in correzioni.items():
        testo = re.sub(rf"(?<![^\W\d_]){re.escape(sbagliata)}(?![^\W\d_])", giusta, testo, flags=re.IGNORECASE)
    return testo


# ---------- 1. doppioni: raggruppamento degli oggetti quasi identici ----------
STOP = set("""della delle dello degli dalla dalle dallo alla alle allo nella nelle nello sulla sulle sullo
questo questa quale quali sono stato stata essere anche come oltre ogni relativo relativa ricorso ricorsi
sentenza sentenze appello avverso annullamento riforma consiglio sezione ottemperanza esecuzione giudicato
parte parti""".split())


def _parole_chiave(oggetto: str) -> set:
    return {w[:6] for w in re.findall(r"[a-z]{4,}", norm(oggetto)) if w not in STOP}


def raggruppa(righe: list[dict], soglia: float = 0.6) -> list[dict]:
    """Raggruppa le decisioni con oggetto uguale o quasi uguale. Il primo della
    lista (il piu' pertinente) rappresenta il gruppo; gli altri vanno in 'simili'."""
    gruppi = []
    for r in righe:
        chiave = norm(r["oggetto_ricorso"] or "").strip()
        s = _parole_chiave(r["oggetto_ricorso"] or "")
        trovato = None
        for g in gruppi:
            if chiave and chiave == g["chiave"]:
                trovato = g
                break
            if len(s) >= 4 and len(g["s"]) >= 4:
                inter = len(s & g["s"])
                if inter >= 4 and inter / len(s | g["s"]) >= soglia:
                    trovato = g
                    break
        if trovato:
            trovato["simili"].append(r)
        else:
            gruppi.append({"r": r, "s": s, "chiave": chiave, "simili": []})
    return gruppi


def formatta(row: dict) -> dict:
    numero = int(row["numero_provvedimento"][4:]) if row["numero_provvedimento"] else None
    anno = row["numero_provvedimento"][:4] if row["numero_provvedimento"] else None
    return {
        "citazione": citation(row),
        "oggetto": (row["oggetto_ricorso"] or "").capitalize(),
        "esito": (row["esito"] or "").capitalize(),
        "tipo_ricorso": (row["tipo_ricorso"] or "").capitalize(),
        "sede_da_cercare": pretty_sede(row["sede"]),
        "numero_da_cercare": f"{numero}/{anno}" if numero else None,
        "ricerca_portale_url": RICERCA_PORTALE_URL,
    }


@app.get("/search")
def search(q: str, n: int = 5, sede: str = "", sezione: str = "", tipo: str = "",
           esito: str = "", dal: str = "", al: str = ""):
    n = max(1, min(n, MAX_RISULTATI))
    filtri = dict(sede=sede, sezione=sezione, tipo=tipo, esito=esito, dal=dal, al=al)

    # Numeri come 523/2026 cercano la decisione o il ricorso; il resto e' testo.
    num_matches = re.findall(r"\b\d+/\d{4}\b", q)
    testo = re.sub(r"\b\d+/\d{4}\b", " ", q)
    testo = re.sub(r"(?i)(?<!\w)(n|nr|n\.|nr\.|n°|numero)(?!\w)", " ", testo)
    testo = re.sub(r"\s+", " ", testo).strip()

    correzioni = {}
    conn = get_conn()
    try:
        if num_matches:
            righe = cerca_numero(conn, num_matches[0], filtri)
        elif testo:
            righe = cerca_testo(conn, testo, filtri)
            if not righe:
                correzioni = correggi(conn, testo)
                if correzioni:
                    testo = applica(testo, correzioni)
                    righe = cerca_testo(conn, testo, filtri)
        else:
            righe = []
    finally:
        conn.close()

    gruppi = raggruppa(righe)[:n]
    risultati = []
    for g in gruppi:
        item = formatta(g["r"])
        simili = sorted(g["simili"], key=lambda r: r["data_pubblicazione"] or date.min, reverse=True)
        item["simili"] = [formatta(r) for r in simili[:MAX_SIMILI]]
        item["simili_totali"] = len(simili)
        risultati.append(item)

    return JSONResponse({
        "query": q,
        "query_usata": testo if correzioni else None,
        "correzioni": correzioni,
        "candidati": len(righe),
        "candidati_limite": len(righe) >= CANDIDATI,
        "risultati": risultati,
    })


@app.get("/filtri")
def filtri(sede: str = ""):
    """Valori per i menu dei filtri: sedi presenti nel database e sezioni della sede scelta."""
    conn = get_conn()
    try:
        sedi = conn.run("SELECT sede, COUNT(*) FROM decisioni GROUP BY sede ORDER BY COUNT(*) DESC;")
        if sede:
            sez = conn.run("SELECT sezione, COUNT(*) FROM decisioni WHERE sede = :s AND sezione IS NOT NULL GROUP BY sezione;", s=sede)
        else:
            sez = conn.run("SELECT sezione, COUNT(*) FROM decisioni WHERE sezione IS NOT NULL GROUP BY sezione;")
    finally:
        conn.close()

    def ordine(z):
        m = re.match(r"^SEZIONE\s+(.+)$", z or "", re.IGNORECASE)
        v = m.group(1).upper() if m else ""
        v = ORD.get(v, v)
        return (0 if z.upper() == "PLENARIA" else 1, ROMANI.get(v, 99), z)

    return JSONResponse({
        "sedi": [{"valore": s, "etichetta": pretty_sede(s), "n": c} for s, c in sedi],
        "sezioni": [{"valore": z, "etichetta": pretty_sez(z), "n": c} for z, c in sorted(sez, key=lambda x: ordine(x[0]))],
    })


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


@app.get("/stato")
def stato():
    """Controllo rapido dal browser: la correzione degli errori e' attiva?"""
    return JSONResponse({"vocabolario": STATO_VOCABOLARIO})


@app.api_route("/", methods=["GET", "HEAD"])
def home():
    with open("static/index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())
