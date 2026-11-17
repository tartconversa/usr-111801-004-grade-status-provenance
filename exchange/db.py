import sqlite3


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, jurisdiction TEXT NOT NULL,
    classification TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS fields (
    source_id TEXT NOT NULL REFERENCES sources(id), name TEXT NOT NULL,
    meaning TEXT NOT NULL, kind TEXT NOT NULL, sensitive INTEGER NOT NULL,
    PRIMARY KEY(source_id,name)
);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
    schema_version INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
    received_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
    recipient TEXT NOT NULL, purpose TEXT NOT NULL, valid_until TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS policies (
    jurisdiction TEXT NOT NULL, classification TEXT NOT NULL, purpose TEXT NOT NULL,
    decision TEXT NOT NULL, revision INTEGER NOT NULL,
    PRIMARY KEY(jurisdiction,classification,purpose)
);
CREATE TABLE IF NOT EXISTS products (
    id TEXT NOT NULL, revision INTEGER NOT NULL, name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL,
    PRIMARY KEY(id,revision)
);
CREATE TABLE IF NOT EXISTS product_inputs (
    product_id TEXT NOT NULL, product_revision INTEGER NOT NULL,
    asset_id TEXT NOT NULL REFERENCES assets(id),
    PRIMARY KEY(product_id,product_revision,asset_id),
    FOREIGN KEY(product_id,product_revision) REFERENCES products(id,revision)
);
CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY, product_id TEXT NOT NULL, product_revision INTEGER NOT NULL,
    recipient TEXT NOT NULL, purpose TEXT NOT NULL, created_at TEXT NOT NULL,
    input_digest TEXT NOT NULL, policy_revisions TEXT NOT NULL,
    FOREIGN KEY(product_id,product_revision) REFERENCES products(id,revision)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, subject TEXT NOT NULL, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spots (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    jurisdiction TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spot_jurisdiction_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT, spot_id TEXT NOT NULL REFERENCES spots(id),
    from_jurisdiction TEXT NOT NULL, to_jurisdiction TEXT NOT NULL,
    effective_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    UNIQUE(spot_id,effective_at)
);
CREATE TABLE IF NOT EXISTS rating_documents (
    id TEXT PRIMARY KEY, spot_id TEXT NOT NULL REFERENCES spots(id),
    doc_type TEXT NOT NULL, issuer TEXT NOT NULL, jurisdiction TEXT NOT NULL,
    issued_at TEXT NOT NULL, effective_at TEXT NOT NULL,
    target_level TEXT, source_id TEXT NOT NULL REFERENCES sources(id),
    asset_id TEXT NOT NULL REFERENCES assets(id), digest TEXT NOT NULL,
    corrects_document_id TEXT REFERENCES rating_documents(id),
    governing INTEGER NOT NULL DEFAULT 1, received_at TEXT NOT NULL,
    received_seq INTEGER NOT NULL DEFAULT 0,
    UNIQUE(spot_id,digest)
);
CREATE TABLE IF NOT EXISTS rating_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, spot_id TEXT NOT NULL REFERENCES spots(id),
    document_id TEXT NOT NULL UNIQUE REFERENCES rating_documents(id),
    kind TEXT NOT NULL, status TEXT NOT NULL,
    level TEXT, issued_at TEXT NOT NULL, effective_at TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES rating_decisions(id),
    sequence INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rating_publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL REFERENCES rating_documents(id),
    channel TEXT NOT NULL, published_at TEXT NOT NULL,
    UNIQUE(document_id,channel)
);
CREATE TABLE IF NOT EXISTS rating_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, spot_id TEXT NOT NULL REFERENCES spots(id),
    reason TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES rating_documents(id),
    other_document_id TEXT REFERENCES rating_documents(id),
    active INTEGER NOT NULL DEFAULT 1, opened_at TEXT NOT NULL, resolved_at TEXT
);
"""


def connect(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection
