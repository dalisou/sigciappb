"""Migrate the local CIAP SQLite database and documents to PostgreSQL/S3."""
import argparse
import os
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", default="data/ciap.db", type=Path)
    parser.add_argument("--documents", default="documentos", type=Path)
    parser.add_argument("--upload-documents", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL", "").startswith(("postgres://", "postgresql://", "postgresql+psycopg2://")):
        raise SystemExit("Defina DATABASE_URL com uma URL PostgreSQL antes da migração.")
    os.environ["CIAP_AUTO_IMPORT"] = "0"

    import app

    source = sqlite3.connect(args.sqlite)
    source.row_factory = sqlite3.Row
    target = app.db()
    tables = ["users", "pessoas", "atendimentos", "agendamentos", "alertas_agendamento", "disponibilidades", "frequencias", "auditoria"]

    try:
        for table in tables[1:]:
            if target.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise SystemExit(f"O banco PostgreSQL não está vazio: {table} já possui registros.")
        target.execute("DELETE FROM users")

        target_columns = {
            table: {row["name"] for row in target.execute(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ?",
                (table,),
            ).fetchall()}
            for table in tables
        }
        for table in tables:
            columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
            columns = [column for column in columns if column in target_columns[table]]
            rows = source.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
            if not rows:
                continue
            placeholders = ", ".join("?" for _ in columns)
            query = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
            for row in rows:
                target.execute(query, tuple(row[column] for column in columns))
            print(f"{table}: {len(rows)} registros")

        for table in tables:
            target.execute(
                "SELECT setval(pg_get_serial_sequence(?, 'id'), COALESCE((SELECT MAX(id) FROM "
                + table
                + "), 1), (SELECT COUNT(*) > 0 FROM " + table + "))",
                (table,),
            )
        target.commit()
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()

    if args.upload_documents:
        if not os.environ.get("CIAP_S3_BUCKET"):
            raise SystemExit("CIAP_S3_BUCKET é obrigatório para enviar documentos.")
        storage = app.object_storage()
        count = 0
        for document in args.documents.rglob("*"):
            if document.is_file() and document.name != ".gitkeep":
                with document.open("rb") as stream:
                    extra_args = {}
                    encryption = os.environ.get("CIAP_S3_SERVER_SIDE_ENCRYPTION", "")
                    if encryption:
                        extra_args["ServerSideEncryption"] = encryption
                    storage.upload_fileobj(
                        stream, os.environ["CIAP_S3_BUCKET"], f"documentos/{document.name}", ExtraArgs=extra_args
                    )
                count += 1
        print(f"documentos: {count} arquivos enviados")


if __name__ == "__main__":
    main()
