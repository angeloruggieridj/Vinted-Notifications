from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import db
import core
import os
import re
import time
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)

# Create Flask app
app = Flask(
    __name__,
    template_folder=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "templates"
    ),
    static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
)

# Secret key for session management
app.secret_key = os.urandom(24)


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
    return {"current_year": datetime.now().year}


@app.route("/")
def index():
    # Get parameters
    params = db.get_all_parameters()

    # Get queries
    queries = db.get_queries()
    formatted_queries = []
    for i, query in enumerate(queries):
        parsed_query = urlparse(query[1])
        query_params = parse_qs(parsed_query.query)
        query_name = (
            query[3]
            if query[3] is not None
            else query_params.get("search_text", [None])[0]
        )

        # Get the last timestamp for this query
        try:
            last_timestamp = db.get_last_timestamp(query[0])
            last_found_item = datetime.fromtimestamp(last_timestamp).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except Exception as e:
            logger.debug(f"Error getting last timestamp for query {query[0]}: {e}")
            last_found_item = "Never"

        formatted_queries.append(
            {
                "id": i + 1,
                "query_id": query[0],
                "query": query[1],
                "display": query_name if query_name else query[1],
                "last_found_item": last_found_item,
                "min_stars": query[4] or "",
                "min_reviews": query[5] or "",
            }
        )

    # Get recent items
    items = db.get_items(limit=10)
    formatted_items = []
    for item in items:
        formatted_items.append(
            {
                "title": item[1],
                "price": item[2],
                "currency": item[3],
                "timestamp": datetime.fromtimestamp(item[4]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "query": item[5],
                "photo_url": item[6],
                "url": f"{urlparse(item[5]).scheme}://{urlparse(item[5]).netloc}/items/{item[0]}",
            }
        )

    # Get process status from the database
    telegram_running = db.get_parameter("telegram_process_running") == "True"
    rss_running = db.get_parameter("rss_process_running") == "True"

    # Get statistics for the dashboard
    stats = {
        "total_items": db.get_total_items_count(),
        "total_queries": db.get_total_queries_count(),
        "items_per_day": db.get_items_per_day(),
    }

    # Get the last found item
    last_item = db.get_last_found_item()
    if last_item:
        stats["last_item"] = {
            "title": last_item[1],
            "price": last_item[2],
            "currency": last_item[3],
            "timestamp": datetime.fromtimestamp(last_item[4]).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "query": last_item[5],
            "photo_url": last_item[6],
            "url": f"{urlparse(last_item[5]).scheme}://{urlparse(last_item[5]).netloc}/items/{last_item[0]}"
        }
    else:
        stats["last_item"] = None

    return render_template(
        "index.html",
        params=params,
        queries=formatted_queries,
        items=formatted_items,
        telegram_running=telegram_running,
        rss_running=rss_running,
        stats=stats,
    )


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


@app.route("/queries")
def queries():
    # Get queries
    all_queries = db.get_queries()
    formatted_queries = []
    for i, query in enumerate(all_queries):
        parsed_query = urlparse(query[1])
        query_params = parse_qs(parsed_query.query)
        query_name = (
            query[3]
            if query[3] is not None
            else query_params.get("search_text", [None])[0]
        )

        # Get the last timestamp for this query
        try:
            last_timestamp = db.get_last_timestamp(query[0])
            last_found_item = datetime.fromtimestamp(last_timestamp).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except Exception as e:
            logger.debug(f"Error getting last timestamp for query {query[0]}: {e}")
            last_found_item = "Never"

        formatted_queries.append(
            {
                "id": i + 1,
                "query_id": query[0],
                "query": query[1],
                "display": query_name if query_name else query[1],
                "last_found_item": last_found_item,
                "min_stars": query[4] or "",
                "min_reviews": query[5] or "",
            }
        )

    return render_template("queries.html", queries=formatted_queries)


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
        )
        if is_new_query:
            flash(f"Query added: {query}", "success")
        else:
            flash(message, "warning")
    else:
        flash("No query provided", "error")

    return redirect(url_for("queries"))


@app.route("/remove_query/<int:query_id>", methods=["POST"])
def remove_query(query_id):
    message, success = core.process_remove_query(str(query_id))
    if success:
        flash("Query removed", "success")
    else:
        flash(message, "error")

    return redirect(url_for("queries"))


@app.route("/remove_query/all", methods=["POST"])
def remove_all_queries():
    message, success = core.process_remove_query("all")
    if success:
        flash("All queries removed", "success")
    else:
        flash(message, "error")

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
        )
        if success:
            flash("Query updated", "success")
        else:
            flash(message, "error")
    else:
        flash("No query provided", "error")

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


def _items_context():
    """Read the Items filters from the query string and build the template context."""
    filters = {
        "query": request.args.get("query", ""),
        "limit": min(max(_int_arg("limit", 50), 1), 500),
        "q": request.args.get("q", "").strip(),
        "seller": request.args.get("seller", "").strip(),
        "min_price": request.args.get("min_price", ""),
        "max_price": request.args.get("max_price", ""),
        "min_stars": request.args.get("min_stars", ""),
        "min_reviews": request.args.get("min_reviews", ""),
        "period": request.args.get("period", ""),
        "sort": request.args.get("sort", "newest"),
    }
    since = (
        int(time.time()) - PERIODS[filters["period"]]
        if filters["period"] in PERIODS
        else None
    )
    items_data, total = db.search_items(
        limit=filters["limit"],
        query_id=_int_arg("query"),
        text=filters["q"] or None,
        seller=filters["seller"] or None,
        min_price=_float_arg("min_price"),
        max_price=_float_arg("max_price"),
        min_stars=_float_arg("min_stars"),
        min_reviews=_int_arg("min_reviews"),
        since=since,
        sort=filters["sort"],
    )

    formatted_items = []
    for item in items_data:
        formatted_items.append(
            {
                "title": item[1],
                "price": item[2],
                "currency": item[3],
                # Epoch is formatted in the browser (viewer's timezone); the string is
                # only the no-JavaScript fallback.
                "ts": item[4],
                "timestamp": datetime.fromtimestamp(item[4]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                # Ugly Ugly Ugly very Ugly eeew but I have to do a proper migration of existing db later else it'll break
                # Eeew bad me >:c
                "query": (
                    item[7] if item[7] else parse_qs(urlparse(item[5]).query).get("search_text", [None])[0]
                    if parse_qs(urlparse(item[5]).query).get("search_text", [None])[0]
                    else item[5]
                ),
                "url": f"{urlparse(item[5]).scheme}://{urlparse(item[5]).netloc}/items/{item[0]}",
                "photo_url": item[6],
                "seller_login": item[8],
                "seller_stars": item[9],
                "seller_reviews": item[10],
            }
        )

    # Get queries for filter dropdown
    queries = db.get_queries()
    formatted_queries = []
    selected_query_display = None
    for i, q in enumerate(queries):
        parsed_query = urlparse(q[1])
        query_params = parse_qs(parsed_query.query)
        query_name = (
            q[3] if q[3] is not None else query_params.get("search_text", [None])[0]
        )
        display_name = query_name if query_name else q[0]
        # Store display name for selected query
        if filters["query"] == str(q[0]):
            selected_query_display = display_name
        formatted_queries.append(
            {"id": i + 1, "query_id": q[0], "query": q[1], "display": display_name}
        )

    return dict(
        items=formatted_items,
        total=total,
        queries=formatted_queries,
        f=filters,
        selected_query=filters["query"],
        selected_query_display=selected_query_display,
        autorefresh_enabled=db.get_parameter("items_autorefresh") == "True",
        autorefresh_interval=max(
            5, int(db.get_parameter("query_refresh_delay") or 60)
        ),
    )


@app.route("/items")
def items():
    return render_template("items.html", **_items_context())


@app.route("/items/fragment")
def items_fragment():
    """Results block only, used by the Items page to update without a full reload."""
    return render_template("_items_results.html", **_items_context())


@app.route("/config")
def config():
    params = db.get_all_parameters()
    return render_template("config.html", params=params)


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
    db.set_parameter("rss_port", request.form.get("rss_port", "8080"))
    db.set_parameter("rss_max_items", request.form.get("rss_max_items", "100"))

    # Update System parameters
    db.set_parameter("items_per_query", request.form.get("items_per_query", "20"))
    db.set_parameter(
        "query_refresh_delay", request.form.get("query_refresh_delay", "60")
    )
    db.set_parameter("banwords", request.form.get("banwords", ""))
    items_autorefresh = "items_autorefresh" in request.form
    db.set_parameter("items_autorefresh", str(items_autorefresh))

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

    flash("Configuration updated", "success")
    return redirect(url_for("config"))


@app.route("/control/<process_name>/<action>", methods=["POST"])
def control_process(process_name, action):
    if process_name not in ["telegram", "rss"]:
        return jsonify({"status": "error", "message": "Invalid process name"})

    if action == "start":
        if process_name == "telegram":
            # Check current status
            if db.get_parameter("telegram_process_running") == "True":
                return jsonify(
                    {"status": "warning", "message": "Telegram bot already running"}
                )

            # Check if telegram_token and telegram_chat_id are set
            telegram_token = db.get_parameter("telegram_token")
            telegram_chat_id = db.get_parameter("telegram_chat_id")
            if not telegram_token or not telegram_chat_id:
                return jsonify(
                    {
                        "status": "error",
                        "message": "Please set Telegram token and chat ID in the configuration panel before starting the Telegram process",
                    }
                )

            # Update process status in the database
            # The manager process will detect this and start the process
            db.set_parameter("telegram_process_running", "True")
            logger.info("Telegram bot process start requested")
            return jsonify(
                {"status": "success", "message": "Telegram bot start requested"}
            )

        elif process_name == "rss":
            # Check current status
            if db.get_parameter("rss_process_running") == "True":
                return jsonify(
                    {"status": "warning", "message": "RSS feed already running"}
                )

            # Update process status in the database
            # The manager process will detect this and start the process
            db.set_parameter("rss_process_running", "True")
            logger.info("RSS feed process start requested")
            return jsonify({"status": "success", "message": "RSS feed start requested"})

    elif action == "stop":
        if process_name == "telegram":
            # Check current status
            if db.get_parameter("telegram_process_running") != "True":
                return jsonify(
                    {"status": "warning", "message": "Telegram bot not running"}
                )

            # Update process status in the database
            # The manager process will detect this and stop the process
            db.set_parameter("telegram_process_running", "False")
            logger.info("Telegram bot process stop requested")
            return jsonify(
                {"status": "success", "message": "Telegram bot stop requested"}
            )

        elif process_name == "rss":
            # Check current status
            if db.get_parameter("rss_process_running") != "True":
                return jsonify({"status": "warning", "message": "RSS feed not running"})

            # Update process status in the database
            # The manager process will detect this and stop the process
            db.set_parameter("rss_process_running", "False")
            logger.info("RSS feed process stop requested")
            return jsonify({"status": "success", "message": "RSS feed stop requested"})

    return jsonify({"status": "error", "message": "Invalid action"})


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
        flash(message, "success" if "added" in message else "warning")
    else:
        flash("No country provided", "error")

    return redirect(url_for("allowlist"))


@app.route("/remove_country/<country>", methods=["POST"])
def remove_country(country):
    message, country_list = core.process_remove_country(country)
    flash(message, "success")

    return redirect(url_for("allowlist"))


@app.route("/clear_allowlist", methods=["POST"])
def clear_allowlist():
    db.clear_allowlist()
    flash("Allowlist cleared", "success")

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
