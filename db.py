"""
db.py - Schema per l'indice OpenGA (solo metadati, niente testo).
"""

import os
from urllib.parse import urlparse
from pg8000.native import Connection


def get_conn() -> Connection:
    parsed = urlparse(os.environ["DATABASE_URL"])
    return Connection(
        user=parsed.username, password=parsed.password, host=parsed.hostname,
        port=parsed.port or 5432, database=parsed.path.lstrip("/"), ssl_context=True,
    )


def create_schema():
    conn = get_conn()
    try:
        conn.run("""
            CREATE TABLE IF NOT EXISTS decisioni (
                id TEXT PRIMARY KEY,          -- sede|numero_provvedimento
                sede TEXT NOT NULL,
                sezione TEXT,
                numero_provvedimento TEXT NOT NULL,
                numero_ricorso TEXT,
                data_pubblicazione DATE,
                esito TEXT,
                tipo_ricorso TEXT,
                oggetto_ricorso TEXT,
                tipo_provvedimento TEXT,
                anno_dataset TEXT,             -- da quale file OpenGA proviene (es. "2026" o "2017-2024")
                search_vector TSVECTOR GENERATED ALWAYS AS (
                    to_tsvector('italian', coalesce(oggetto_ricorso,'') || ' ' || coalesce(tipo_ricorso,''))
                ) STORED
            );
        """)
        conn.run("CREATE INDEX IF NOT EXISTS idx_search ON decisioni USING GIN(search_vector);")
        conn.run("CREATE INDEX IF NOT EXISTS idx_sede ON decisioni (sede);")
        conn.run("CREATE INDEX IF NOT EXISTS idx_numero ON decisioni (numero_provvedimento);")
        conn.run("CREATE INDEX IF NOT EXISTS idx_ricorso ON decisioni (numero_ricorso);")
    finally:
        conn.close()
