import sqlite3
from pathlib import Path
import threading


class DataStore:
    def __init__(self, path, max_bytes=256 * 1024 * 1024):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=5000')
        if self.db.execute('PRAGMA user_version').fetchone()[0] not in (0, 1):
            raise ValueError('Unsupported database version; refusing destructive migration')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS devices(device_id TEXT PRIMARY KEY,last_seen REAL NOT NULL,metadata TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS models(model_id TEXT,version TEXT,metadata TEXT NOT NULL,PRIMARY KEY(model_id,version));
        CREATE TABLE IF NOT EXISTS runs(device_id TEXT,run_id TEXT,model_id TEXT,model_version TEXT,
          state TEXT NOT NULL,metadata TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,
          confirmed_through_seq INTEGER NOT NULL DEFAULT -1,PRIMARY KEY(device_id,run_id));
        CREATE TABLE IF NOT EXISTS samples(device_id TEXT,run_id TEXT,seq INTEGER,sample_time_s REAL,
          voltage_v REAL,current_a REAL,temperature_c REAL,digest TEXT NOT NULL,received_at REAL NOT NULL,
          PRIMARY KEY(device_id,run_id,seq),FOREIGN KEY(device_id,run_id) REFERENCES runs(device_id,run_id));
        CREATE TABLE IF NOT EXISTS predictions(device_id TEXT,run_id TEXT,seq INTEGER,model_id TEXT,model_version TEXT,
          task TEXT,result TEXT NOT NULL,created_at REAL NOT NULL,
          PRIMARY KEY(device_id,run_id,model_id,model_version,task,seq),
          FOREIGN KEY(device_id,run_id) REFERENCES runs(device_id,run_id));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,device_id TEXT,run_id TEXT,
          created_at REAL NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL);
        PRAGMA user_version=1;
        ''')
        self.db.commit()

    def bytes_used(self):
        return sum(p.stat().st_size for p in [self.path, Path(str(self.path) + '-wal')]
                   if p.exists())

    def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            target = sqlite3.connect(destination)
            try: self.db.backup(target)
            finally: target.close()
        return destination

    def close(self):
        with self.lock: self.db.close()
