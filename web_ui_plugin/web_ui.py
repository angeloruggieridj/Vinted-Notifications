from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    session,
    g,
    has_request_context,
    Response,
    send_from_directory,
)
from werkzeug.security import check_password_hash, generate_password_hash
import db
import core
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import zoneinfo
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from logger import get_logger
from web_ui_plugin import i18n

# Get logger for this module
logger = get_logger(__name__)

# ------------------------------------------------------------------ translations
def current_language():
    """Language for this request: Configuration setting, else the browser's, else English."""
    if not has_request_context():
        return i18n.DEFAULT_LANGUAGE
    if "lang" not in g:
        g.lang = i18n.resolve_language(
            db.get_parameter("language"), request.accept_languages
        )
    return g.lang


def tr(msgid, **kwargs):
    return i18n.translate(current_language(), msgid, **kwargs)


def tr_n(singular, plural, n):
    return tr(singular if n == 1 else plural, n=n)


# Create Flask app
app = Flask(
    __name__,
    template_folder=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "templates"
    ),
    static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
)

# The secret key is stored so that logins survive restarts
_secret = db.get_parameter("web_secret_key")
if not _secret:
    _secret = secrets.token_hex(32)
    db.set_parameter("web_secret_key", _secret)
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_NAME="vn_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)

app.jinja_env.globals.update(_=tr, _n=tr_n)

# ------------------------------------------------------------------ authentication
# Optional password for the whole web UI. Set it from Configuration > Security, or with the
# VN_WEB_PASSWORD environment variable (which takes precedence, handy if the password is lost).
MAX_LOGIN_FAILURES = 5
LOGIN_LOCK_SECONDS = 60
MIN_PASSWORD_LENGTH = 8
_login_failures = {}  # ip -> (failures, locked_until)
# Reachable without logging in: the login page, static files and what a PWA needs to install
PUBLIC_ENDPOINTS = ("static", "login", "manifest", "service_worker", "offline")


def _env_password():
    return os.environ.get("VN_WEB_PASSWORD", "")


def _password_hash():
    return db.get_parameter("web_password_hash") or ""


def auth_enabled():
    return bool(_env_password() or _password_hash())


def _auth_token():
    """Changes whenever the password does, which logs out every other session."""
    secret = _env_password() or _password_hash()
    return hashlib.sha256(secret.encode()).hexdigest()[:24]


def _check_password(candidate):
    env = _env_password()
    if env:
        return hmac.compare_digest(env.encode(), candidate.encode())
    stored = _password_hash()
    return bool(stored) and check_password_hash(stored, candidate)


def _safe_next(target):
    """Only follow same-site relative redirects after login."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("index")


@app.before_request
def require_login():
    if not auth_enabled() or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if session.get("auth") == _auth_token():
        return None
    if request.headers.get("X-Requested-With") == "fetch" or request.path.startswith(
        "/control"
    ):
        return jsonify({"status": "error", "message": tr("Login required")}), 401
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth_enabled():
        return redirect(url_for("index"))
    ip = request.remote_addr or "?"
    failures, locked_until = _login_failures.get(ip, (0, 0))
    error = None
    if request.method == "POST":
        if locked_until > time.time():
            error = tr("Too many attempts. Try again in %(n)s seconds.", n=int(locked_until - time.time()) + 1)
        elif _check_password(request.form.get("password", "")):
            _login_failures.pop(ip, None)
            session.clear()
            session["auth"] = _auth_token()
            session.permanent = True
            return redirect(_safe_next(request.form.get("next")))
        else:
            failures += 1
            locked_until = time.time() + LOGIN_LOCK_SECONDS if failures >= MAX_LOGIN_FAILURES else 0
            _login_failures[ip] = (0 if locked_until else failures, locked_until)
            error = tr("Wrong password.")
    return render_template(
        "login.html", error=error, next=request.values.get("next", "")
    ), (429 if error and locked_until > time.time() else 200)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/security/password", methods=["POST"])
def set_password():
    if _env_password():
        flash(tr("The password is managed by the VN_WEB_PASSWORD environment variable."), "warning")
        return redirect(url_for("config", _anchor="security"))
    if _password_hash() and not _check_password(request.form.get("current_password", "")):
        flash(tr("Current password is wrong."), "danger")
        return redirect(url_for("config", _anchor="security"))
    new = request.form.get("new_password", "")
    if len(new) < MIN_PASSWORD_LENGTH:
        flash(tr("Use at least %(n)s characters.", n=MIN_PASSWORD_LENGTH), "danger")
    elif new != request.form.get("confirm_password", ""):
        flash(tr("The two passwords do not match."), "danger")
    else:
        db.set_parameter("web_password_hash", generate_password_hash(new))
        session["auth"] = _auth_token()  # keep this browser logged in
        session.permanent = True
        flash(tr("Password saved. Other browsers have been logged out."), "success")
    return redirect(url_for("config", _anchor="security"))


@app.route("/security/password/remove", methods=["POST"])
def remove_password():
    if _env_password():
        flash(tr("The password is managed by the VN_WEB_PASSWORD environment variable."), "warning")
    elif not _check_password(request.form.get("current_password", "")):
        flash(tr("Current password is wrong."), "danger")
    else:
        db.set_parameter("web_password_hash", "")
        flash(tr("Password removed. The web interface is open to anyone who can reach it."), "warning")
    return redirect(url_for("config", _anchor="security"))


_version_cache = {"at": 0, "value": None}


def _cached_check_version(ttl=3600):
    """check_version() hits GitHub, so don't do it on every page/fragment request."""
    if _version_cache["value"] is None or time.time() - _version_cache["at"] > ttl:
        _version_cache["value"] = core.check_version()
        _version_cache["at"] = time.time()
    return _version_cache["value"]


@app.context_processor
def inject_version_info():
    is_up_to_date, current_ver, latest_version, github_url = _cached_check_version()
    return {
        "github_url": github_url,
        "current_version": current_ver,
        "latest_version": latest_version,
        "is_up_to_date": is_up_to_date,
    }


@app.context_processor
def inject_current_year():
    return {
        "current_year": datetime.now().year,
        "display_tz": db.get_parameter("timezone") or "",
        "auth_enabled": auth_enabled(),
        "lang": current_language(),
        "languages": i18n.available_languages(),
        "js_i18n": i18n.js_catalog(current_language()),
    }


def _display_tz():
    """The timezone chosen in Config, or None to use the server's local time."""
    name = db.get_parameter("timezone")
    if not name:
        return None
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:
        return None


def format_ts(epoch):
    """Format an epoch timestamp in the configured timezone."""
    return datetime.fromtimestamp(epoch, _display_tz()).strftime("%Y-%m-%d %H:%M:%S")


def _clean_int(field, default, minimum=1):
    """Read a positive integer setting from the form, falling back to `default`."""
    try:
        value = int(request.form.get(field, ""))
        if value >= minimum:
            return str(value)
    except ValueError:
        pass
    flash(tr("Invalid value for %(field)s, using %(default)s", field=field, default=default), "warning")
    return str(default)


def _setting_int(name, default):
    try:
        return int(db.get_parameter(name) or default)
    except ValueError:
        return default


def parse_max_price(value):
    """Parse the optional maximum-price form value; None if empty/invalid/<=0."""
    try:
        return max(0.0, float(str(value).replace(",", "."))) or None
    except (TypeError, ValueError):
        return None


def parse_min_reviews(value):
    """Parse the optional minimum-reviews form value; None if empty/invalid/<=0."""
    try:
        return max(0, int(value)) or None
    except (TypeError, ValueError):
        return None


def parse_min_stars(value):
    """Parse the optional minimum-stars form value; clamp to 0-5, None if empty/invalid."""
    try:
        stars = float(value)
    except (TypeError, ValueError):
        return None
    return min(5.0, max(0.0, stars)) or None


def _query_display(url, name):
    return name or parse_qs(urlparse(url).query).get("search_text", [None])[0] or url


def _format_item(item):
    """Template-ready dict from a db.search_items row."""
    search_text = parse_qs(urlparse(item[5]).query).get("search_text", [None])[0]
    avg, history = item[11], item[12]
    deal_pct = None
    try:
        if avg and history >= db.DEAL_MIN_HISTORY and float(item[2]) <= avg * db.DEAL_RATIO:
            deal_pct = round((1 - float(item[2]) / avg) * 100)
    except (TypeError, ValueError):
        pass
    return {
        "id": item[0],
        "title": item[1],
        "price": item[2],
        "currency": item[3],
        # Epoch is formatted in the browser; the string is the no-JavaScript fallback.
        "ts": item[4],
        "timestamp": format_ts(item[4]),
        "query": item[7] or search_text or item[5],
        "url": f"{urlparse(item[5]).scheme}://{urlparse(item[5]).netloc}/items/{item[0]}",
        "photo_url": item[6],
        "seller_login": item[8],
        "seller_stars": item[9],
        "seller_reviews": item[10],
        "avg_price": round(avg, 2) if avg else None,
        "deal_pct": deal_pct,
    }


def _queries_overview():
    """Queries with filters, collected-item count, average price and last-found time."""
    overview = []
    for i, q in enumerate(db.get_queries_overview(), start=1):
        overview.append(
            {
                "id": i,
                "query_id": q[0],
                "query": q[1],
                "display": _query_display(q[1], q[2]),
                "name": q[2] or "",
                "last_ts": q[3],
                "last_found_item": format_ts(q[3]) if q[3] else "Never",
                "min_stars": q[4] or "",
                "min_reviews": q[5] or "",
                "max_price": ("%g" % q[6]) if q[6] else "",
                "item_count": q[7],
                "avg_price": round(q[8], 2) if q[8] else None,
            }
        )
    return overview


@app.route("/")
def index():
    params = db.get_all_parameters()
    latest, _ = db.search_items(limit=8)
    deals_today = db.search_items(
        limit=1, since=int(time.time()) - 86400, deals_only=True
    )[1]
    stats = {
        "total_items": db.get_total_items_count(),
        "total_queries": db.get_total_queries_count(),
        "items_per_day": db.get_items_per_day(),
        "deals_today": deals_today,
    }
    return render_template(
        "index.html",
        params=params,
        queries=_queries_overview(),
        items=[_format_item(row) for row in latest],
        telegram_running=params.get("telegram_process_running") == "True",
        rss_running=params.get("rss_process_running") == "True",
        stats=stats,
    )


@app.route("/queries")
def queries():
    return render_template("queries.html", queries=_queries_overview())


@app.route("/add_query", methods=["POST"])
def add_query():
    query = request.form.get("query")
    query_name = request.form.get("query_name", "").strip()
    if query:
        message, is_new_query = core.process_query(
            query,
            name=query_name if query_name != "" else None,
            min_stars=parse_min_stars(request.form.get("min_stars")),
            min_reviews=parse_min_reviews(request.form.get("min_reviews")),
            max_price=parse_max_price(request.form.get("max_price")),
        )
        if is_new_query:
            flash(tr("Query added: %(query)s", query=query), "success")
        else:
            flash(tr(message), "warning")
    else:
        flash(tr("No query provided"), "error")

    return redirect(url_for("queries"))


@app.route("/remove_query/<int:query_id>", methods=["POST"])
def remove_query(query_id):
    message, success = core.process_remove_query(str(query_id))
    if success:
        flash(tr("Query removed"), "success")
    else:
        flash(tr(message), "error")

    return redirect(url_for("queries"))


@app.route("/remove_query/all", methods=["POST"])
def remove_all_queries():
    message, success = core.process_remove_query("all")
    if success:
        flash(tr("All queries removed"), "success")
    else:
        flash(tr(message), "error")

    return redirect(url_for("queries"))


@app.route("/update_query/<int:query_id>", methods=["POST"])
def update_query(query_id):
    query = request.form.get("query")
    query_name = request.form.get("query_name", "").strip()

    if query:
        message, success = core.process_update_query(
            query_id,
            query,
            name=query_name if query_name != "" else None,
            min_stars=parse_min_stars(request.form.get("min_stars")),
            min_reviews=parse_min_reviews(request.form.get("min_reviews")),
            max_price=parse_max_price(request.form.get("max_price")),
        )
        if success:
            flash(tr("Query updated"), "success")
        else:
            flash(tr(message), "error")
    else:
        flash(tr("No query provided"), "error")

    return redirect(url_for("queries"))


def _float_arg(name):
    try:
        return float(request.args.get(name, ""))
    except ValueError:
        return None


def _int_arg(name, default=None):
    try:
        return int(request.args.get(name, ""))
    except ValueError:
        return default


PERIODS = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
PAGE_SIZES = (12, 24, 48, 96, 192)
DEFAULT_PAGE_SIZE = 24


def _page_window(page, pages):
    """Page numbers to show in the pager, with None marking a gap."""
    wanted = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    window = []
    for number in wanted:
        if window and number - window[-1] > 1:
            window.append(None)
        window.append(number)
    return window


def _items_context():
    """Read the Items filters from the query string and build the template context."""
    per_page = _int_arg("per_page", _int_arg("limit", DEFAULT_PAGE_SIZE))
    if per_page not in PAGE_SIZES:
        per_page = DEFAULT_PAGE_SIZE
    filters = {
        "query": request.args.get("query", ""),
        "per_page": per_page,
        "q": request.args.get("q", "").strip(),
        "seller": request.args.get("seller", "").strip(),
        "min_price": request.args.get("min_price", ""),
        "max_price": request.args.get("max_price", ""),
        "min_stars": request.args.get("min_stars", ""),
        "min_reviews": request.args.get("min_reviews", ""),
        "period": request.args.get("period", ""),
        "sort": request.args.get("sort", "newest"),
        "deals": "1" if request.args.get("deals") else "",
    }
    since = (
        int(time.time()) - PERIODS[filters["period"]]
        if filters["period"] in PERIODS
        else None
    )
    search = dict(
        query_id=_int_arg("query"),
        text=filters["q"] or None,
        seller=filters["seller"] or None,
        min_price=_float_arg("min_price"),
        max_price=_float_arg("max_price"),
        min_stars=_float_arg("min_stars"),
        min_reviews=_int_arg("min_reviews"),
        since=since,
        sort=filters["sort"],
        deals_only=bool(filters["deals"]),
    )
    page = max(1, _int_arg("page", 1))
    items_data, total = db.search_items(
        limit=per_page, offset=(page - 1) * per_page, **search
    )
    pages = max(1, -(-total // per_page))
    if page > pages:  # e.g. the last page emptied after a clear or a stricter filter
        page = pages
        items_data, total = db.search_items(
            limit=per_page, offset=(page - 1) * per_page, **search
        )

    formatted_items = [_format_item(row) for row in items_data]

    # Queries for the filter dropdown
    formatted_queries = []
    selected_query_display = None
    for q in db.get_queries():
        display_name = _query_display(q[1], q[3])
        if filters["query"] == str(q[0]):
            selected_query_display = display_name
        formatted_queries.append({"query_id": q[0], "display": display_name})

    active_filters = sum(
        1
        for key in ("query", "q", "seller", "min_price", "max_price", "min_stars", "min_reviews", "period", "deals")
        if filters[key]
    )
    return dict(
        items=formatted_items,
        total=total,
        page=page,
        pages=pages,
        page_window=_page_window(page, pages),
        page_sizes=PAGE_SIZES,
        first_shown=(page - 1) * per_page + 1 if formatted_items else 0,
        last_shown=(page - 1) * per_page + len(formatted_items),
        queries=formatted_queries,
        f=filters,
        active_filters=active_filters,
        selected_query=filters["query"],
        selected_query_display=selected_query_display,
        autorefresh_enabled=db.get_parameter("items_autorefresh") == "True",
        autorefresh_interval=max(5, _setting_int("query_refresh_delay", 60)),
        deal_percent=round((1 - db.DEAL_RATIO) * 100),
        new_items_notice=db.get_parameter("new_items_notice") != "False",
    )


@app.route("/items")
def items():
    return render_template("items.html", **_items_context())


@app.route("/items/fragment")
def items_fragment():
    """Results block only, used by the Items page to update without a full reload."""
    return render_template("_items_results.html", **_items_context())


@app.route("/items/clear", methods=["POST"])
def clear_items():
    query_id = request.form.get("query_id", type=int)
    deleted = db.clear_items(query_id)
    flash(
        tr_n("Deleted %(n)s item. ", "Deleted %(n)s items. ", deleted)
        + tr("Existing listings will be recorded silently on the next check."),
        "success",
    )
    return redirect(url_for("items"))


# ------------------------------------------------------------------ progressive web app
@app.route("/manifest.webmanifest")
def manifest():
    icon = lambda name, size, purpose="any": {
        "src": url_for("static", filename=f"icons/{name}"),
        "sizes": size,
        "type": "image/png",
        "purpose": purpose,
    }
    data = {
        "name": "Vinted Notifications",
        "short_name": "Vinted",
        "description": tr("New Vinted listings for your searches, as they appear."),
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "lang": current_language(),
        "background_color": "#F2F5F4",
        "theme_color": "#0F2B30",
        "icons": [
            icon("icon-192.png", "192x192"),
            icon("icon-512.png", "512x512"),
            icon("icon-maskable-512.png", "512x512", "maskable"),
        ],
        "shortcuts": [
            {"name": tr("Items"), "url": "/items"},
            {"name": tr("Queries"), "url": "/queries"},
        ],
    }
    return Response(json.dumps(data), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    """Served from the root so the worker controls the whole site."""
    response = send_from_directory(app.static_folder, "sw.js", mimetype="text/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/offline")
def offline():
    return render_template("offline.html")


@app.route("/config")
def config():
    params = db.get_all_parameters()
    return render_template(
        "config.html",
        params=params,
        timezones=sorted(zoneinfo.available_timezones()),
        languages=i18n.available_languages(),
        password_set=bool(_password_hash()),
        password_env=bool(_env_password()),
        min_password_length=MIN_PASSWORD_LENGTH,
    )


@app.route("/update_config", methods=["POST"])
def update_config():
    # Update Telegram parameters
    telegram_enabled = "telegram_enabled" in request.form
    db.set_parameter("telegram_enabled", str(telegram_enabled))
    db.set_parameter("telegram_token", request.form.get("telegram_token", ""))
    db.set_parameter("telegram_chat_id", request.form.get("telegram_chat_id", ""))

    # Update RSS parameters
    rss_enabled = "rss_enabled" in request.form
    db.set_parameter("rss_enabled", str(rss_enabled))
    db.set_parameter("rss_port", _clean_int("rss_port", 8080))
    db.set_parameter("rss_max_items", _clean_int("rss_max_items", 100))

    # Update System parameters
    db.set_parameter("items_per_query", _clean_int("items_per_query", 20))
    db.set_parameter("query_refresh_delay", _clean_int("query_refresh_delay", 60))
    timezone = request.form.get("timezone", "").strip()
    try:
        if timezone:
            zoneinfo.ZoneInfo(timezone)
        db.set_parameter("timezone", timezone)
    except Exception:
        flash(tr('Unknown timezone "%(tz)s", keeping the previous one', tz=timezone), "warning")
    db.set_parameter("banwords", request.form.get("banwords", ""))
    items_autorefresh = "items_autorefresh" in request.form
    db.set_parameter("items_autorefresh", str(items_autorefresh))
    db.set_parameter("new_items_notice", str("new_items_notice" in request.form))
    language = request.form.get("language", "")
    if language == "" or language in dict(i18n.available_languages()):
        db.set_parameter("language", language)

    # Update Proxy parameters
    check_proxies = "check_proxies" in request.form
    db.set_parameter("check_proxies", str(check_proxies))
    db.set_parameter("proxy_list", request.form.get("proxy_list", ""))
    db.set_parameter("proxy_list_link", request.form.get("proxy_list_link", ""))

    # Update Advanced parameters
    db.set_parameter("message_template", request.form.get("message_template", ""))
    db.set_parameter("user_agents", request.form.get("user_agents", "[]"))
    db.set_parameter("default_headers", request.form.get("default_headers", "{}"))

    # Reset proxy cache to force refresh on next use
    db.set_parameter("last_proxy_check_time", "1")
    logger.info("Proxy settings updated, cache reset")

    flash(tr("Configuration updated"), "success")
    return redirect(url_for("config"))


@app.route("/control/<process_name>/<action>", methods=["POST"])
def control_process(process_name, action):
    if process_name not in ["telegram", "rss"]:
        return jsonify({"status": "error", "message": tr("Invalid process name")})

    if action == "start":
        if process_name == "telegram":
            # Check current status
            if db.get_parameter("telegram_process_running") == "True":
                return jsonify(
                    {"status": "warning", "message": tr("Telegram bot already running")}
                )

            # Check if telegram_token and telegram_chat_id are set
            telegram_token = db.get_parameter("telegram_token")
            telegram_chat_id = db.get_parameter("telegram_chat_id")
            if not telegram_token or not telegram_chat_id:
                return jsonify(
                    {
                        "status": "error",
                        "message": tr("Please set Telegram token and chat ID in the configuration panel before starting the Telegram process"),
                    }
                )

            # Update process status in the database
            # The manager process will detect this and start the process
            db.set_parameter("telegram_process_running", "True")
            logger.info("Telegram bot process start requested")
            return jsonify(
                {"status": "success", "message": tr("Telegram bot start requested")}
            )

        elif process_name == "rss":
            # Check current status
            if db.get_parameter("rss_process_running") == "True":
                return jsonify(
                    {"status": "warning", "message": tr("RSS feed already running")}
                )

            # Update process status in the database
            # The manager process will detect this and start the process
            db.set_parameter("rss_process_running", "True")
            logger.info("RSS feed process start requested")
            return jsonify({"status": "success", "message": tr("RSS feed start requested")})

    elif action == "stop":
        if process_name == "telegram":
            # Check current status
            if db.get_parameter("telegram_process_running") != "True":
                return jsonify(
                    {"status": "warning", "message": tr("Telegram bot not running")}
                )

            # Update process status in the database
            # The manager process will detect this and stop the process
            db.set_parameter("telegram_process_running", "False")
            logger.info("Telegram bot process stop requested")
            return jsonify(
                {"status": "success", "message": tr("Telegram bot stop requested")}
            )

        elif process_name == "rss":
            # Check current status
            if db.get_parameter("rss_process_running") != "True":
                return jsonify({"status": "warning", "message": tr("RSS feed not running")})

            # Update process status in the database
            # The manager process will detect this and stop the process
            db.set_parameter("rss_process_running", "False")
            logger.info("RSS feed process stop requested")
            return jsonify({"status": "success", "message": tr("RSS feed stop requested")})

    return jsonify({"status": "error", "message": tr("Invalid action")})


@app.route("/control/status", methods=["GET"])
def process_status():
    # Get process status from the database
    telegram_running = db.get_parameter("telegram_process_running") == "True"
    rss_running = db.get_parameter("rss_process_running") == "True"

    return jsonify({"telegram": telegram_running, "rss": rss_running})


@app.route("/allowlist")
def allowlist():
    countries = db.get_allowlist()
    if countries == 0:
        countries = []

    return render_template("allowlist.html", countries=countries)


@app.route("/add_country", methods=["POST"])
def add_country():
    country = request.form.get("country", "").strip()
    if country:
        message, country_list = core.process_add_country(country)
        if message.startswith('Country "') and message.endswith("already in allowlist."):
            shown = tr('Country "%(c)s" already in allowlist.', c=country.replace(" ", "").upper())
        else:
            shown = tr(message)
        flash(shown, "success" if "added" in message else "warning")
    else:
        flash(tr("No country provided"), "error")

    return redirect(url_for("allowlist"))


@app.route("/remove_country/<country>", methods=["POST"])
def remove_country(country):
    message, country_list = core.process_remove_country(country)
    flash(tr(message), "success")

    return redirect(url_for("allowlist"))


@app.route("/clear_allowlist", methods=["POST"])
def clear_allowlist():
    db.clear_allowlist()
    flash(tr("Allowlist cleared"), "success")

    return redirect(url_for("allowlist"))


@app.route("/logs")
def logs():
    return render_template("logs.html")


@app.route("/api/logs")
def api_logs():
    offset = int(request.args.get("offset", 0))
    limit = int(request.args.get("limit", 100))
    level_filter = request.args.get("level", "all")

    log_file_path = os.path.join("logs", "vinted.log")

    if not os.path.exists(log_file_path):
        return jsonify({"logs": [], "total": 0})

    # Parse log file
    log_entries = []
    total_matching_entries = 0

    try:
        with open(log_file_path, "r", encoding="utf-8") as file:
            # Read all lines from the file
            all_lines = file.readlines()

            # Process lines in reverse order (newest first)
            all_lines.reverse()

            # Regular expression to parse log lines
            # Format: 2023-09-15 12:34:56,789 - module_name - LEVEL - Message
            log_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - ([^-]+) - ([A-Z]+) - (.+)"

            current_entry = 0

            for line in all_lines:
                match = re.match(log_pattern, line.strip())
                if match:
                    timestamp, module, level, message = match.groups()

                    # Apply level filter if specified
                    if level_filter != "all" and level != level_filter:
                        continue

                    total_matching_entries += 1

                    # Skip entries before offset
                    if total_matching_entries <= offset:
                        continue

                    # Add entry if within limit
                    if current_entry < limit:
                        log_entries.append(
                            {
                                "timestamp": timestamp,
                                "module": module.strip(),
                                "level": level,
                                "message": message,
                            }
                        )
                        current_entry += 1

                    # Stop if we've reached the limit
                    if current_entry >= limit:
                        break
    except Exception as e:
        logger.error(f"Error reading log file: {e}")
        return jsonify({"logs": [], "total": 0, "error": str(e)})

    return jsonify({"logs": log_entries, "total": total_matching_entries})


def web_ui_process():
    logger.info("Web UI process started")
    try:
        app.run(host="0.0.0.0", port=8000, debug=False)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Web UI process stopped")
    except Exception as e:
        logger.error(f"Error in web UI process: {e}", exc_info=True)
