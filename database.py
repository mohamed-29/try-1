import sqlite3
import json
import os
import time

DB_NAME = "vmc_middleware.db"

class DatabaseManager:
    def __init__(self, db_path=DB_NAME):
        self.db_path = db_path
        self._init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self.get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            
            # 1. Command Queue
            conn.execute("""
                CREATE TABLE IF NOT EXISTS command_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_hex TEXT NOT NULL,
                    status TEXT DEFAULT 'PENDING',
                    retry_count INTEGER DEFAULT 0,
                    assigned_pack_no INTEGER,
                    response_payload TEXT,
                    completion_details TEXT,
                    created_at REAL DEFAULT (datetime('now', 'localtime')),
                    updated_at REAL DEFAULT (datetime('now', 'localtime'))
                );
            """)

            # 2. VMC Status
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vmc_status (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    raw_hex TEXT,
                    updated_at REAL DEFAULT (datetime('now', 'localtime'))
                );
            """)

            # 3. Event Log
            conn.execute("""
                CREATE TABLE IF NOT EXISTS event_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT,
                    raw_data TEXT,
                    parsed_data TEXT,
                    created_at REAL DEFAULT (datetime('now', 'localtime'))
                );
            """)

            # 4. Products Table (NEW for 0x11)
            # Stores the latest known state of every slot in the machine
            conn.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    selection_id INTEGER PRIMARY KEY, -- e.g., 10, 11, 20
                    price INTEGER,                    -- In cents/lowest unit
                    inventory INTEGER,
                    capacity INTEGER,
                    product_id INTEGER,               -- Internal VMC PID
                    status INTEGER,                   -- 0=Normal, 1=Paused
                    updated_at REAL DEFAULT (datetime('now', 'localtime'))
                );
            """)
            conn.commit()

    # --- Command Management ---

    def get_next_command(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM command_queue 
                WHERE status IN ('PENDING', 'SENDING')
                ORDER BY CASE WHEN status = 'SENDING' THEN 1 ELSE 2 END, id ASC
                LIMIT 1
            """)
            row = cursor.fetchone()
            return dict(row) if row else None

    def mark_as_sending(self, cmd_id, pack_no):
        with self.get_connection() as conn:
            conn.execute("UPDATE command_queue SET status='SENDING', assigned_pack_no=?, updated_at=datetime('now') WHERE id=?", (pack_no, cmd_id))
            conn.commit()

    def update_command_result(self, cmd_id, status, response_hex=None, details_dict=None):
        details_json = json.dumps(details_dict) if details_dict else None
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE command_queue 
                SET status=?, response_payload=?, completion_details=?, updated_at=datetime('now')
                WHERE id=?
            """, (status, response_hex, details_json, cmd_id))
            conn.commit()

    def increment_retry(self, cmd_id, current_retries):
        new_count = current_retries + 1
        status = 'FAILED' if new_count >= 5 else 'SENDING'
        with self.get_connection() as conn:
            conn.execute("UPDATE command_queue SET retry_count=?, status=?, updated_at=datetime('now') WHERE id=?", (new_count, status, cmd_id))
            conn.commit()
        return status

    # --- Data & Products ---

    def upsert_product(self, data):
        """
        Updates a product slot from a 0x11 report.
        data: {selection, price, inventory, capacity, product_id, status}
        """
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO products (selection_id, price, inventory, capacity, product_id, status, updated_at)
                VALUES (:selection, :price, :inventory, :capacity, :product_id, :status, datetime('now'))
                ON CONFLICT(selection_id) DO UPDATE SET
                    price=excluded.price,
                    inventory=excluded.inventory,
                    capacity=excluded.capacity,
                    product_id=excluded.product_id,
                    status=excluded.status,
                    updated_at=excluded.updated_at
            """, data)
            conn.commit()

    def update_machine_status(self, key, value, raw_hex=None):
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO vmc_status (key, value, raw_hex, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, raw_hex=excluded.raw_hex, updated_at=excluded.updated_at
            """, (key, str(value), raw_hex))
            conn.commit()

    def log_event(self, event_type, raw_data, parsed_dict=None):
        parsed_json = json.dumps(parsed_dict) if parsed_dict else ""
        with self.get_connection() as conn:
            conn.execute("INSERT INTO event_log (event_type, raw_data, parsed_data) VALUES (?, ?, ?)", (event_type, raw_data, parsed_json))
            conn.commit()

if __name__ == "__main__":
    db = DatabaseManager()
    print("Database Updated with Products Table.")