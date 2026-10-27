import sqlite3
from traceback import print_exc

DB_PATH = "./data/vinted_notifications.db"


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_or_update_sqlite_db(db_path):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Using the sql script
        with open(db_path, "r", encoding="utf-8") as sql_file:
            sql_script = sql_file.read()
            cursor.executescript(sql_script)

        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def ensure_schema():
    """Idempotent schema upgrades that don't warrant a version migration."""
    conn = None
    try:
        conn = get_db_connection()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(queries)")]
        if "min_stars" not in cols:
            conn.execute("ALTER TABLE queries ADD COLUMN min_stars REAL")
        if "min_reviews" not in cols:
            conn.execute("ALTER TABLE queries ADD COLUMN min_reviews INTEGER")
        item_cols = [r[1] for r in conn.execute("PRAGMA table_info(items)")]
        for col, col_type in (
            ("seller_id", "INTEGER"),
            ("seller_login", "TEXT"),
            ("seller_stars", "REAL"),
            ("seller_reviews", "INTEGER"),
        ):
            if col not in item_cols:
                conn.execute(f"ALTER TABLE items ADD COLUMN {col} {col_type}")
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_query_seller_filters(query_id):
    """(min_stars, min_reviews) required for a query; each is None if not set."""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT min_stars, min_reviews FROM queries WHERE id=?", (query_id,)
        ).fetchone()
        if not row:
            return None, None
        return row[0] or None, row[1] or None
    except Exception:
        print_exc()
        return None, None
    finally:
        if conn:
            conn.close()


def is_item_in_db_by_id(id):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT() FROM items WHERE item=?", (id,))
        if cursor.fetchone()[0]:
            return True
        return False
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_last_timestamp(query_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT last_item FROM queries WHERE id=?", (query_id,))
        result = cursor.fetchone()
        if result:
            return result[0]
        return None
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def update_last_timestamp(query_id, timestamp):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE queries SET last_item=? WHERE id=?", (timestamp, query_id)
        )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def add_item_to_db(
    id,
    title,
    query_id,
    price,
    timestamp,
    photo_url,
    currency="EUR",
    seller_id=None,
    seller_login=None,
    seller_stars=None,
    seller_reviews=None,
):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Insert into db the id and the query_id related to the item
        cursor.execute(
            "INSERT INTO items (item, title, price, currency, timestamp, photo_url, query_id, "
            "seller_id, seller_login, seller_stars, seller_reviews) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                id,
                title,
                price,
                currency,
                timestamp,
                photo_url,
                query_id,
                seller_id,
                seller_login,
                seller_stars,
                seller_reviews,
            ),
        )
        # Update the last item for the query
        cursor.execute(
            "UPDATE queries SET last_item=? WHERE id=?", (timestamp, query_id)
        )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_queries():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, query, last_item, query_name, min_stars, min_reviews FROM queries")
        return cursor.fetchall()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def is_query_in_db(processed_query):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # replace spaces in searched_text by % to match any query containing the searched text

        cursor.execute(
            "SELECT COUNT() FROM queries WHERE query = ?", (processed_query,)
        )
        if cursor.fetchone()[0]:
            return True
        return False
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def add_query_to_db(query, name=None, min_stars=None, min_reviews=None):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO queries (query, last_item, query_name, min_stars, min_reviews) VALUES (?, NULL, ?, ?, ?)",
            (query, name or None, min_stars, min_reviews),
        )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_query_id_by_rowid(rowid):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        query = f"SELECT id FROM (SELECT id, ROW_NUMBER() OVER (ORDER BY ROWID) rn FROM queries) t WHERE rn={rowid}"
        cursor.execute(query)
        result = cursor.fetchone()
        if result:
            return result[0]
        return None
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def remove_query_from_db(query_number):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Delete items associated with this query using query_id
        cursor.execute("DELETE FROM items WHERE query_id=?", (query_number,))
        # Delete the query
        cursor.execute("DELETE FROM queries WHERE id=?", (query_number,))
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def remove_all_queries_from_db():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Delete all items first to maintain foreign key integrity
        cursor.execute("DELETE FROM items")
        # Then delete all queries
        cursor.execute("DELETE FROM queries")
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def update_query_in_db(query_id, query, name, min_stars=None, min_reviews=None):
    """
    Update an existing query in the database.

    Args:
        query_id (int): The ID of the query to update
        query (str): The new query URL
        name (str, optional): The new name for the query
        min_stars (float, optional): Minimum seller stars (0-5)
        min_reviews (int, optional): Minimum number of seller reviews

    Returns:
        bool: True if the query was updated successfully, False otherwise
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE queries SET query=?, query_name=?, min_stars=?, min_reviews=? WHERE id=?",
            (query, name, min_stars, min_reviews, query_id),
        )
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def add_to_allowlist(country):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO allowlist VALUES (?)", (country,))
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def remove_from_allowlist(country):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM allowlist WHERE country=?", (country,))
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_allowlist():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM allowlist")
        # Get list of countries
        countries = [country[0] for country in cursor.fetchall()]
        # Return 0 if there are no countries in the allowlist
        if not countries:
            return 0
        return countries
    finally:
        if conn:
            conn.close()


def clear_allowlist():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM allowlist")
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_parameter(key):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM parameters WHERE key=?", (key,))
        result = cursor.fetchone()
        return result[0] if result else None
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def set_parameter(key, value):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE parameters SET value=? WHERE key=?", (value, key))
        if cursor.rowcount == 0:
            # Parameter not present yet (e.g. added after the DB was created)
            cursor.execute(
                "INSERT INTO parameters (key, value) VALUES (?, ?)", (key, value)
            )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_all_parameters():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM parameters")
        return {row[0]: row[1] for row in cursor.fetchall()}
    except Exception:
        print_exc()
        return {}
    finally:
        if conn:
            conn.close()


def get_items(limit=50):
    """Most recent stored items (used by the dashboard)."""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT i.item, i.title, i.price, i.currency, i.timestamp, q.query, i.photo_url, q.query_name FROM items i JOIN queries q ON i.query_id = q.id ORDER BY i.timestamp DESC LIMIT ?",
            (limit,),
        )
        return cursor.fetchall()
    except Exception:
        print_exc()
        return []
    finally:
        if conn:
            conn.close()


ITEM_SORTS = {
    "newest": "i.timestamp DESC",
    "oldest": "i.timestamp ASC",
    "price_asc": "i.price ASC",
    "price_desc": "i.price DESC",
    "stars_desc": "i.seller_stars DESC",
    "reviews_desc": "i.seller_reviews DESC",
}


def _escape_like(value):
    """Escape LIKE wildcards so user input is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clear_items(query_id=None):
    """
    Delete stored items, either all of them or only those of one query.

    The affected queries also get their "last item" watermark reset, so the next scrape
    is treated as a first run (existing listings are recorded silently instead of
    flooding the notification channels).

    Returns:
        int: number of deleted items.
    """
    conn = None
    try:
        conn = get_db_connection()
        if query_id:
            deleted = conn.execute(
                "DELETE FROM items WHERE query_id=?", (query_id,)
            ).rowcount
            conn.execute("UPDATE queries SET last_item=NULL WHERE id=?", (query_id,))
        else:
            deleted = conn.execute("DELETE FROM items").rowcount
            conn.execute("UPDATE queries SET last_item=NULL")
        conn.commit()
        return deleted
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


def search_items(
    limit=50,
    query_id=None,
    text=None,
    seller=None,
    min_price=None,
    max_price=None,
    min_stars=None,
    min_reviews=None,
    since=None,
    sort="newest",
    offset=0,
):
    """
    Search stored items with optional filters.

    Returns:
        tuple: (rows, total_matching) where each row is
        (item, title, price, currency, timestamp, query_url, photo_url, query_name,
         seller_login, seller_stars, seller_reviews)
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        where, params = [], []
        if query_id:
            where.append("i.query_id = ?")
            params.append(query_id)
        if text:
            where.append("i.title LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(text)}%")
        if seller:
            where.append("i.seller_login LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(seller)}%")
        if min_price is not None:
            where.append("i.price >= ?")
            params.append(min_price)
        if max_price is not None:
            where.append("i.price <= ?")
            params.append(max_price)
        if min_stars is not None:
            where.append("i.seller_stars >= ?")
            params.append(min_stars)
        if min_reviews is not None:
            where.append("i.seller_reviews >= ?")
            params.append(min_reviews)
        if since is not None:
            where.append("i.timestamp >= ?")
            params.append(since)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        order_sql = ITEM_SORTS.get(sort, ITEM_SORTS["newest"])
        base = "FROM items i JOIN queries q ON i.query_id = q.id" + where_sql
        total = conn.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
        rows = conn.execute(
            "SELECT i.item, i.title, i.price, i.currency, i.timestamp, q.query, "
            "i.photo_url, q.query_name, i.seller_login, i.seller_stars, i.seller_reviews "
            + base
            + f" ORDER BY {order_sql}, i.timestamp DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return rows, total
    except Exception:
        print_exc()
        return [], 0
    finally:
        if conn:
            conn.close()


def get_total_items_count():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM items")
        return cursor.fetchone()[0]
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


def get_total_queries_count():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM queries")
        return cursor.fetchone()[0]
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


def get_last_found_item():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT i.item, i.title, i.price, i.currency, i.timestamp, q.query, i.photo_url FROM items i JOIN queries q ON i.query_id = q.id ORDER BY i.timestamp DESC LIMIT 1"
        )
        return cursor.fetchone()
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def get_items_per_day():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Get total items
        cursor.execute("SELECT COUNT(*) FROM items")
        total_items = cursor.fetchone()[0]

        if total_items == 0:
            return 0

        # Get earliest and latest timestamps
        cursor.execute("SELECT MIN(timestamp), MAX(timestamp) FROM items")
        min_timestamp, max_timestamp = cursor.fetchone()

        # Calculate number of days (add 1 to include both start and end days)
        import datetime

        min_date = datetime.datetime.fromtimestamp(min_timestamp).date()
        max_date = datetime.datetime.fromtimestamp(max_timestamp).date()
        days_diff = (max_date - min_date).days + 1

        # Ensure at least 1 day to avoid division by zero
        days_diff = max(1, days_diff)

        # Calculate items per day
        return round(total_items / days_diff, 1)
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()
