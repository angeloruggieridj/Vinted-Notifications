"""
Is a collected listing still for sale?

Vinted has no public "is this item still there" endpoint, so the listing's own web page is asked:
  * a removed listing answers 404 (checked with a HEAD request, which costs next to nothing);
  * a sold one is still served, with the purchase state in its embedded data (can_buy / is_reserved / is_hidden).
Anything unclear (rate limits, bot checks, a page without that data) is "unknown" and never deletes anything.
A "sold" verdict has to be seen twice, on two different checks, before the item is dropped.

Checks are paced (a pause between requests, a small batch per run, and each item is looked at no more often than
the account's chosen interval), so the cleanup stays light on Vinted. Opening an item in the app checks that one
item once (cached for ten minutes), which is the closest thing to real time without polling.
"""

import random
import re
import threading
import time
from urllib.parse import urlparse

import db
import proxies
from logger import get_logger
from pyVintedVN.requester import Requester

logger = get_logger(__name__)

AVAILABLE, REMOVED, SOLD, UNKNOWN = "available", "removed", "sold", "unknown"
HOST = re.compile(r"^(?:[a-z0-9-]+\.)*vinted\.[a-z]{2,}(?:\.[a-z]{2})?$", re.IGNORECASE)
RECENT = 600  # seconds during which an item that was just checked is not asked about again
MAX_BYTES = 2_000_000
INTERVALS = ("0", "1", "6", "24")  # hours between checks of the same item; 0 = never

_requesters = {}
_lock = threading.Lock()
_manual = {}  # account -> progress of the "check now" button


def _requester(host):
    """A session of its own (the scraper's session and locale are left alone), one per Vinted domain."""
    with _lock:
        req = _requesters.get(host)
        if req is None:
            req = Requester()
            req.set_locale(host)
            _requesters[host] = req
        return req


def _marker(item_id):
    """Pattern for the purchase state of the listing inside the page's embedded JSON."""
    i = re.escape(str(item_id))
    flags = r'"can_buy":(true|false)(?:,"[a-z_]+":[^,{}]{1,40}){0,4}'
    return re.compile(
        rf'{flags},"item_id":"{i}"|"item_id":"{i}"[^{{}}]{{0,300}}?"can_buy":(true|false)'
        rf'|"can_buy":(true|false)[^{{}}]{{0,200}}?"id":"{i}"|"id":"{i}"[^{{}}]{{0,200}}?"can_buy":(true|false)'
    )


def _state_from_block(block):
    can_buy = re.search(r'"can_buy":(true|false)', block)
    reserved = re.search(r'"is_reserved":(true|false)', block)
    hidden = re.search(r'"is_hidden":(true|false)', block)
    if not can_buy:
        return UNKNOWN
    if can_buy.group(1) == "true":
        return AVAILABLE
    if reserved and reserved.group(1) == "true":
        return AVAILABLE  # reserved for someone, still listed
    if hidden and hidden.group(1) == "true":
        return UNKNOWN  # hidden by its seller: may come back
    return SOLD


def check(item_id, base_url):
    """Ask Vinted about one listing. `base_url` is any URL on the Vinted domain the item belongs to."""
    host = urlparse(base_url or "").netloc
    if not HOST.match(host):
        return UNKNOWN
    page = f"https://{host}/items/{int(item_id)}"
    try:
        req = _requester(host)
        proxies.configure_proxy(req.session)
        head = req.session.head(page, headers={"Accept": "text/html"}, timeout=15, allow_redirects=True)
        if head.status_code in (404, 410):
            return REMOVED
        if head.status_code != 200:
            return UNKNOWN
        response = req.session.get(page, headers={"Accept": "text/html"}, timeout=20, stream=True)
        try:
            if response.status_code in (404, 410):
                return REMOVED
            if response.status_code != 200:
                return UNKNOWN
            pattern = _marker(item_id)
            text, total = "", 0
            for chunk in response.iter_content(65536):
                total += len(chunk)
                text += chunk.decode("utf-8", "ignore").replace('\\"', '"')
                match = pattern.search(text)
                if match:
                    return _state_from_block(text[max(0, match.start() - 250): match.end() + 250])
                text = text[-1000:]  # the marker never spans more than that
                if total > MAX_BYTES:
                    break
            return UNKNOWN
        finally:
            response.close()
    except Exception as e:
        logger.debug(f"Availability check of {item_id} failed: {e}")
        return UNKNOWN


def apply(user_id, row, status, delete_sold=True):
    """Record the verdict for an item row (dict with item, available). Returns True if the item was removed."""
    item_id = row["item"]
    if status == REMOVED:
        db.delete_item(item_id, user_id)
        return True
    if status == SOLD:
        if row.get("available") == 0 and delete_sold:  # second sighting
            db.delete_item(item_id, user_id)
            return True
        db.mark_item_checked(item_id, user_id, 0)
        return False
    db.mark_item_checked(item_id, user_id, 1 if status == AVAILABLE else None)
    return False


def cleanup_user(user_id, min_age, batch):
    """Check up to `batch` of the account's items that were last checked more than `min_age` seconds ago."""
    checked = removed = 0
    for row in db.items_due_for_check(user_id, int(time.time()) - min_age, batch):
        status = check(row["item"], row["base_url"])
        if apply(user_id, row, status):
            removed += 1
        checked += 1
        time.sleep(random.uniform(1.0, 2.2))
    return checked, removed


def sweep(batch=20):
    """Scheduled job: every account that chose an interval gets a small batch of due items checked."""
    for user_id in db.users_with_cleanup():
        try:
            hours = int(db.get_user_setting(user_id, "cleanup_interval") or 0)
            if hours <= 0:
                continue
            checked, removed = cleanup_user(user_id, hours * 3600, batch)
            if checked:
                logger.info(f"Cleanup for account {user_id}: checked {checked}, removed {removed}")
        except Exception as e:
            logger.error(f"Cleanup for account {user_id} failed: {e}", exc_info=True)


def check_one(user_id, item_id):
    """Real-time check when an item is opened in the app. Returns (status, removed)."""
    row = db.get_item_for_user(item_id, user_id)
    if not row:
        return REMOVED, True
    row = {"item": row["item"], "available": row["available"], "base_url": row["base_url"], "checked_at": row["checked_at"]}
    if row["checked_at"] and time.time() - row["checked_at"] < RECENT:
        return (SOLD if row["available"] == 0 else AVAILABLE if row["available"] == 1 else UNKNOWN), False
    status = check(row["item"], row["base_url"])
    auto = int(db.get_user_setting(user_id, "cleanup_interval") or 0) > 0
    # Opening an item never deletes it on a first "sold" sighting; a missing listing goes only if cleanup is on
    removed = False
    if status == REMOVED and auto:
        removed = apply(user_id, row, status)
    elif status != REMOVED:
        apply(user_id, row, status, delete_sold=False)
    return status, removed


def start_manual(user_id, batch=60):
    """"Check now": runs in the background; progress is read with manual_state()."""
    state = _manual.get(user_id)
    if state and state["running"]:
        return False
    _manual[user_id] = {"running": True, "checked": 0, "removed": 0, "started": time.time()}

    def run():
        state = _manual[user_id]
        try:
            for row in db.items_due_for_check(user_id, int(time.time()) - RECENT, batch):
                status = check(row["item"], row["base_url"])
                if apply(user_id, row, status):
                    state["removed"] += 1
                state["checked"] += 1
                time.sleep(random.uniform(0.8, 1.6))
        except Exception as e:
            logger.error(f"Manual cleanup failed: {e}", exc_info=True)
        finally:
            state["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return True


def manual_state(user_id):
    return _manual.get(user_id) or {"running": False, "checked": 0, "removed": 0}
