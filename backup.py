from datetime import datetime
from pathlib import Path
import os
import shutil
import sqlite3


BASE = Path(__file__).parent
DB = Path(os.environ.get("CIAP_DB_PATH", BASE / "data" / "ciap.db")).resolve()
DOCUMENTS = Path(os.environ.get("CIAP_DOCUMENTS_DIR", BASE / "documentos")).resolve()
BACKUPS = Path(os.environ.get("CIAP_BACKUP_DIR", BASE / "backups")).resolve()
RETENTION = int(os.environ.get("CIAP_BACKUP_RETENTION", "30"))


def create_backup():
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = BACKUPS / timestamp
    destination.mkdir(parents=True, exist_ok=False)
    database_backup = destination / "ciap.db"
    with sqlite3.connect(DB) as source, sqlite3.connect(database_backup) as target:
        source.backup(target)
    if DOCUMENTS.exists():
        shutil.copytree(DOCUMENTS, destination / "documentos")
    else:
        (destination / "documentos").mkdir()
    prune_backups()
    print(destination)


def prune_backups():
    backups = sorted((path for path in BACKUPS.iterdir() if path.is_dir()), reverse=True)
    for old_backup in backups[RETENTION:]:
        shutil.rmtree(old_backup)


if __name__ == "__main__":
    BACKUPS.mkdir(parents=True, exist_ok=True)
    create_backup()