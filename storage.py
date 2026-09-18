import json
import sqlite3
from pathlib import Path
from datetime import datetime


BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "data" / "meter-reader.db"


def connect():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row

    return db


def init_database():
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS meters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meter_uid TEXT NOT NULL UNIQUE,
                name TEXT,
                protocol TEXT NOT NULL,
                identification TEXT,
                created_at TEXT NOT NULL,
                ha_enabled INTEGER NOT NULL DEFAULT 0
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS measurement_series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meter_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                requested_count INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL,
                successful_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running',

                FOREIGN KEY (meter_id)
                    REFERENCES meters(id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meter_id INTEGER NOT NULL,
                read_at TEXT NOT NULL,
                meter_date TEXT,
                meter_time TEXT,
                bcc_valid INTEGER NOT NULL,
                values_json TEXT NOT NULL,
                ha_synced INTEGER NOT NULL DEFAULT 0,

                FOREIGN KEY (meter_id)
                    REFERENCES meters(id)
            )
        """)

        # Identitaetsmodell v0.1: technische Wiedererkennung bleibt in
        # meter_uid. Fabrik-/EVU-Nummern und die bevorzugte Anzeige werden
        # getrennt gespeichert. Bestehende Datenbanken werden in-place erweitert.
        meter_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(meters)").fetchall()
        }
        for column, definition in (
            ("technical_source", "TEXT"),
            ("factory_number", "TEXT"),
            ("evu_number", "TEXT"),
            ("preferred_number", "TEXT NOT NULL DEFAULT 'technical'"),
        ):
            if column not in meter_columns:
                db.execute(f"ALTER TABLE meters ADD COLUMN {column} {definition}")

        # Bestehende Zaehler behalten ihre bisherige UID als technische ID.
        db.execute("""
            UPDATE meters
            SET technical_source = COALESCE(technical_source, 'legacy'),
                preferred_number = COALESCE(preferred_number, 'technical')
        """)

        # Bestehende Datenbanken werden in-place erweitert. Alte Messungen
        # bleiben erhalten; fuer sie wird das bisher am Zaehler gespeicherte
        # Protokoll uebernommen.
        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(readings)").fetchall()
        }
        if "protocol" not in columns:
            db.execute("ALTER TABLE readings ADD COLUMN protocol TEXT")
            db.execute("""
                UPDATE readings
                SET protocol = (
                    SELECT m.protocol
                    FROM meters m
                    WHERE m.id = readings.meter_id
                )
                WHERE protocol IS NULL
            """)

        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(readings)").fetchall()
        }
        if "series_id" not in columns:
            db.execute("ALTER TABLE readings ADD COLUMN series_id INTEGER")

        db.execute("""
            CREATE INDEX IF NOT EXISTS
                idx_readings_series
            ON readings(series_id)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS
                idx_readings_meter_time
            ON readings(meter_id, read_at)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS
                idx_readings_sync
            ON readings(ha_synced)
        """)


def get_meter_by_uid(meter_uid):
    with connect() as db:
        return db.execute(
            """
            SELECT *
            FROM meters
            WHERE meter_uid = ?
            """,
            (meter_uid,),
        ).fetchone()


def get_meter_by_id(meter_id):
    with connect() as db:
        return db.execute(
            """
            SELECT *
            FROM meters
            WHERE id = ?
            """,
            (meter_id,),
        ).fetchone()


def create_meter(
    meter_uid,
    protocol,
    identification=None,
    name=None,
    technical_source=None,
    factory_number=None,
    evu_number=None,
    preferred_number="technical",
):
    created_at = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO meters (
                meter_uid,
                name,
                protocol,
                identification,
                created_at,
                technical_source,
                factory_number,
                evu_number,
                preferred_number
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meter_uid,
                name,
                protocol,
                identification,
                created_at,
                technical_source,
                factory_number,
                evu_number,
                preferred_number,
            ),
        )

        return cursor.lastrowid


def note_meter_protocol(meter_id, protocol):
    """Legacy meters.protocol behalten, bei mehreren Protokollen 'mixed' setzen."""
    with connect() as db:
        row = db.execute(
            "SELECT protocol FROM meters WHERE id = ?", (meter_id,)
        ).fetchone()
        if row is None:
            return
        old = row["protocol"]
        if old == protocol or old == "mixed":
            return
        db.execute(
            "UPDATE meters SET protocol = 'mixed' WHERE id = ?",
            (meter_id,),
        )


def set_meter_name(meter_id, name):
    with connect() as db:
        db.execute(
            """
            UPDATE meters
            SET name = ?
            WHERE id = ?
            """,
            (name, meter_id),
        )


def rekey_meter(meter_id, new_meter_uid):
    """Technischen Wiedererkennungsschluessel eines bestehenden Zaehler aendern."""
    with connect() as db:
        db.execute(
            "UPDATE meters SET meter_uid = ? WHERE id = ?",
            (new_meter_uid, meter_id),
        )


def set_meter_identity(
    meter_id,
    factory_number=None,
    evu_number=None,
    preferred_number=None,
    technical_source=None,
):
    """Metadaten zur Zaehleridentitaet aktualisieren, ohne meter_uid zu aendern."""
    fields = []
    params = []
    for column, value in (
        ("factory_number", factory_number),
        ("evu_number", evu_number),
        ("preferred_number", preferred_number),
        ("technical_source", technical_source),
    ):
        if value is not None:
            fields.append(f"{column} = ?")
            params.append(value)
    if not fields:
        return
    params.append(meter_id)
    with connect() as db:
        db.execute(
            f"UPDATE meters SET {', '.join(fields)} WHERE id = ?",
            params,
        )


def set_ha_meter(meter_id):
    """
    Genau ein Zaehler wird als HA-Quelle markiert.
    """

    with connect() as db:
        db.execute(
            "UPDATE meters SET ha_enabled = 0"
        )

        db.execute(
            """
            UPDATE meters
            SET ha_enabled = 1
            WHERE id = ?
            """,
            (meter_id,),
        )


def save_reading(
    meter_id,
    read_at,
    meter_date,
    meter_time,
    bcc_valid,
    values,
    protocol=None,
    series_id=None,
):
    values_json = json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO readings (
                meter_id,
                read_at,
                meter_date,
                meter_time,
                bcc_valid,
                values_json,
                ha_synced,
                protocol,
                series_id
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                meter_id,
                read_at,
                meter_date,
                meter_time,
                int(bcc_valid),
                values_json,
                protocol,
                series_id,
            ),
        )

        return cursor.lastrowid


def create_measurement_series(
    meter_id,
    started_at,
    requested_count,
    interval_seconds,
):
    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO measurement_series (
                meter_id, started_at, requested_count, interval_seconds, status
            )
            VALUES (?, ?, ?, ?, 'running')
            """,
            (meter_id, started_at, requested_count, interval_seconds),
        )
        return cursor.lastrowid


def finish_measurement_series(
    series_id,
    completed_at,
    successful_count,
    failed_count,
    status,
):
    with connect() as db:
        db.execute(
            """
            UPDATE measurement_series
            SET completed_at = ?,
                successful_count = ?,
                failed_count = ?,
                status = ?
            WHERE id = ?
            """,
            (completed_at, successful_count, failed_count, status, series_id),
        )


def list_meters():
    with connect() as db:
        return db.execute(
            """
            SELECT
                m.*,
                COUNT(r.id) AS reading_count,
                MAX(r.read_at) AS last_reading
            FROM meters m
            LEFT JOIN readings r
                ON r.meter_id = m.id
            GROUP BY m.id
            ORDER BY m.id
            """
        ).fetchall()


def list_measurement_series(meter_id=None, limit=100):
    """Messreihen mit aktuellem Zaehlernamen und tatsaechlicher Messungszahl."""
    with connect() as db:
        params = []
        where = ""
        if meter_id is not None:
            where = "WHERE ms.meter_id = ?"
            params.append(meter_id)
        params.append(limit)
        return db.execute(
            f"""
            SELECT
                ms.*,
                m.meter_uid,
                m.name,
                COUNT(r.id) AS actual_count
            FROM measurement_series ms
            JOIN meters m ON m.id = ms.meter_id
            LEFT JOIN readings r ON r.series_id = ms.id
            {where}
            GROUP BY ms.id
            ORDER BY ms.started_at DESC, ms.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()


def get_measurement_series(series_id):
    with connect() as db:
        return db.execute(
            """
            SELECT
                ms.*,
                m.meter_uid,
                m.name,
                COUNT(r.id) AS actual_count
            FROM measurement_series ms
            JOIN meters m ON m.id = ms.meter_id
            LEFT JOIN readings r ON r.series_id = ms.id
            WHERE ms.id = ?
            GROUP BY ms.id
            """,
            (series_id,),
        ).fetchone()


def get_series_readings(series_id):
    with connect() as db:
        return db.execute(
            """
            SELECT
                r.*,
                m.meter_uid,
                m.name,
                m.protocol
            FROM readings r
            JOIN meters m ON m.id = r.meter_id
            WHERE r.series_id = ?
            ORDER BY r.read_at, r.id
            """,
            (series_id,),
        ).fetchall()


def _refresh_series_counts(db, series_id):
    if series_id is None:
        return
    count = db.execute(
        "SELECT COUNT(*) FROM readings WHERE series_id = ?",
        (series_id,),
    ).fetchone()[0]
    db.execute(
        "UPDATE measurement_series SET successful_count = ? WHERE id = ?",
        (count, series_id),
    )


def get_last_reading(meter_id=None):
    with connect() as db:
        if meter_id is None:
            return db.execute(
                """
                SELECT
                    r.*,
                    m.meter_uid,
                    m.name,
                    m.protocol
                FROM readings r
                JOIN meters m
                    ON m.id = r.meter_id
                ORDER BY r.read_at DESC, r.id DESC
                LIMIT 1
                """
            ).fetchone()

        return db.execute(
            """
            SELECT
                r.*,
                m.meter_uid,
                m.name,
                m.protocol
            FROM readings r
            JOIN meters m
                ON m.id = r.meter_id
            WHERE r.meter_id = ?
            ORDER BY r.read_at DESC, r.id DESC
            LIMIT 1
            """,
            (meter_id,),
        ).fetchone()


def get_reading(reading_id):
    with connect() as db:
        return db.execute(
            """
            SELECT
                r.*,
                m.meter_uid,
                m.name,
                m.protocol
            FROM readings r
            JOIN meters m
                ON m.id = r.meter_id
            WHERE r.id = ?
            """,
            (reading_id,),
        ).fetchone()


def get_history(meter_id=None, limit=50):
    with connect() as db:
        if meter_id is None:
            return db.execute(
                """
                SELECT
                    r.*,
                    m.meter_uid,
                    m.name,
                    m.protocol
                FROM readings r
                JOIN meters m
                    ON m.id = r.meter_id
                ORDER BY r.read_at DESC, r.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return db.execute(
            """
            SELECT
                r.*,
                m.meter_uid,
                m.name,
                m.protocol
            FROM readings r
            JOIN meters m
                ON m.id = r.meter_id
            WHERE r.meter_id = ?
            ORDER BY r.read_at DESC, r.id DESC
            LIMIT ?
            """,
            (meter_id, limit),
        ).fetchall()


def decode_values(reading):
    if reading is None:
        return {}

    try:
        return json.loads(reading["values_json"])
    except (json.JSONDecodeError, TypeError):
        return {}


def pending_ha_readings():
    with connect() as db:
        return db.execute(
            """
            SELECT
                r.*,
                m.meter_uid,
                m.name
            FROM readings r
            JOIN meters m
                ON m.id = r.meter_id
            WHERE
                m.ha_enabled = 1
                AND r.ha_synced = 0
                AND r.bcc_valid = 1
            ORDER BY r.read_at
            """
        ).fetchall()


def delete_reading(reading_id):
    """Loescht eine Auslesung und haelt Messreihen-Zaehler konsistent."""
    with connect() as db:
        row = db.execute(
            "SELECT series_id FROM readings WHERE id = ?",
            (reading_id,),
        ).fetchone()
        if row is None:
            return False
        series_id = row["series_id"]
        cursor = db.execute(
            "DELETE FROM readings WHERE id = ?",
            (reading_id,),
        )
        if cursor.rowcount == 1:
            _refresh_series_counts(db, series_id)
            return True
        return False


def delete_all_readings_for_meter(meter_id):
    """Loescht alle Auslesungen und Messreihen eines Zaehler; Zaehler bleibt."""
    with connect() as db:
        cursor = db.execute(
            "DELETE FROM readings WHERE meter_id = ?",
            (meter_id,),
        )
        deleted = cursor.rowcount
        db.execute(
            "DELETE FROM measurement_series WHERE meter_id = ?",
            (meter_id,),
        )
        return deleted


def delete_meter_with_readings(meter_id):
    """
    Loescht einen Zaehler und alle zugeordneten Auslesungen atomar.
    Liefert (deleted_meter, deleted_readings).
    """
    with connect() as db:
        meter = db.execute(
            "SELECT id FROM meters WHERE id = ?",
            (meter_id,),
        ).fetchone()
        if meter is None:
            return False, 0

        readings_cursor = db.execute(
            "DELETE FROM readings WHERE meter_id = ?",
            (meter_id,),
        )
        deleted_readings = readings_cursor.rowcount

        db.execute(
            "DELETE FROM measurement_series WHERE meter_id = ?",
            (meter_id,),
        )

        meter_cursor = db.execute(
            "DELETE FROM meters WHERE id = ?",
            (meter_id,),
        )
        return meter_cursor.rowcount == 1, deleted_readings
