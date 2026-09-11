import os
import re
import html
import json
import threading
import time

import requests
from flask import Flask, request, Response, jsonify, render_template

from metrics import MetricsStore


app = Flask(__name__)

FLARESOLVERR_URL = os.getenv(
    "FLARESOLVERR_URL",
    "http://flaresolverr:8191/v1",
)

UPSTREAM = "https://stripchat.com"
FLARESOLVERR_SESSION = "stripchat-adapter"
SESSION_TTL_MINUTES = 60
MIN_REQUEST_INTERVAL_SECONDS = 0.5
BLOCK_RETRY_DELAY_SECONDS = 5
BLOCK_COOLDOWN_SECONDS = 15
BROADCAST_CACHE_TTL_SECONDS = 15 * 60
BROADCAST_STALE_TTL_SECONDS = 24 * 60 * 60

solver_http = requests.Session()
solver_lock = threading.Lock()
cache_lock = threading.Lock()
queue_lock = threading.Lock()
broadcast_cache = {}
model_to_username = {}
session_ready = False
next_request_at = 0.0
cooldown_until = 0.0
waiting_requests = 0
active_request = False
metrics = MetricsStore(os.getenv("METRICS_DB_PATH", "/data/metrics.sqlite3"))


def wait_for_request_slot():
    delay = max(next_request_at, cooldown_until) - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def post_solver(payload):
    response = solver_http.post(
        FLARESOLVERR_URL,
        json=payload,
        timeout=70,
    )
    return response.json()


def ensure_session():
    global session_ready

    if session_ready:
        return

    data = post_solver({
        "cmd": "sessions.create",
        "session": FLARESOLVERR_SESSION,
    })

    if data.get("status") != "ok":
        raise RuntimeError(data.get("message") or "Unable to create FlareSolverr session")

    session_ready = True
    print(f"FlareSolverr session ready: {FLARESOLVERR_SESSION}", flush=True)


def rotate_session(reason):
    global session_ready

    try:
        post_solver({
            "cmd": "sessions.destroy",
            "session": FLARESOLVERR_SESSION,
        })
    except (requests.RequestException, ValueError, RuntimeError):
        pass

    session_ready = False
    ensure_session()
    metrics.record_event("session_rotation", reason)


def request_with_browser_session(url):
    global active_request, cooldown_until, next_request_at, waiting_requests

    with queue_lock:
        waiting_requests += 1

    with solver_lock:
        with queue_lock:
            waiting_requests -= 1
            active_request = True

        try:
            blocked_seen = False

            for attempt in range(2):
                wait_for_request_slot()
                ensure_session()

                data = post_solver({
                    "cmd": "request.get",
                    "url": url,
                    "session": FLARESOLVERR_SESSION,
                    "session_ttl_minutes": SESSION_TTL_MINUTES,
                    "maxTimeout": 60000,
                })
                next_request_at = time.monotonic() + MIN_REQUEST_INTERVAL_SECONDS

                message = data.get("message") or ""
                blocked = "Cloudflare has blocked this request" in message
                blocked_seen = blocked_seen or blocked

                if data.get("status") == "ok" or not blocked:
                    return data, attempt, blocked_seen

                if attempt == 0:
                    print("Cloudflare block detected; rotating browser session", flush=True)
                    rotate_session("cloudflare_block")
                    cooldown_until = time.monotonic() + BLOCK_RETRY_DELAY_SECONDS
                    continue

                cooldown_until = time.monotonic() + BLOCK_COOLDOWN_SECONDS
                return data, attempt, blocked_seen
        finally:
            with queue_lock:
                active_request = False


def broadcast_username(path):
    match = re.fullmatch(r"api/front/v1/broadcasts/([^/]+)", path)
    return match.group(1).lower() if match else None


def cam_model_id(path):
    match = re.fullmatch(r"api/front/v2/models/([^/]+)/cam", path)
    return match.group(1) if match else None


def get_cached_broadcast(username, allow_stale=False):
    with cache_lock:
        entry = broadcast_cache.get(username)
        if not entry:
            return None

        stored_at, body = entry
        age = time.monotonic() - stored_at

        if age <= BROADCAST_CACHE_TTL_SECONDS:
            return body

        if allow_stale and age <= BROADCAST_STALE_TTL_SECONDS:
            return body

        if age <= BROADCAST_STALE_TTL_SECONDS:
            return None

        model_id = json.loads(body)["item"]["streamName"]
        broadcast_cache.pop(username, None)
        model_to_username.pop(str(model_id), None)
        return None


def store_broadcast(username, body):
    try:
        model_id = str(json.loads(body)["item"]["streamName"])
    except (KeyError, TypeError, ValueError):
        return

    with cache_lock:
        previous = broadcast_cache.get(username)
        if previous:
            try:
                previous_model = str(json.loads(previous[1])["item"]["streamName"])
                model_to_username.pop(previous_model, None)
            except (KeyError, TypeError, ValueError):
                pass

        broadcast_cache[username] = (time.monotonic(), body)
        model_to_username[model_id] = username

    metrics.associate_model(username, model_id)


def invalidate_broadcast(username):
    with cache_lock:
        entry = broadcast_cache.pop(username, None)
        if not entry:
            return

        try:
            model_id = str(json.loads(entry[1])["item"]["streamName"])
            model_to_username.pop(model_id, None)
        except (KeyError, TypeError, ValueError):
            pass


def invalidate_model(model_id):
    with cache_lock:
        username = model_to_username.pop(model_id, None)
        if username:
            broadcast_cache.pop(username, None)


def username_for_model(model_id):
    if not model_id:
        return None

    with cache_lock:
        return model_to_username.get(model_id)


def flush_cache():
    with cache_lock:
        entry_count = len(broadcast_cache)
        broadcast_cache.clear()
        model_to_username.clear()
        return entry_count


def current_state():
    with cache_lock:
        cache_entries = len(broadcast_cache)

    with queue_lock:
        queue_depth = waiting_requests
        request_active = active_request

    return {
        "cache_entries": cache_entries,
        "session_ready": session_ready,
        "session_name": FLARESOLVERR_SESSION,
        "queue_depth": queue_depth,
        "request_active": request_active,
        "cooldown_seconds": max(0, round(cooldown_until - time.monotonic(), 1)),
    }


def cached_response(username, allow_stale=False):
    body = get_cached_broadcast(username, allow_stale=allow_stale)
    if body is None:
        return None

    cache_state = "stale" if allow_stale else "hit"
    print(f"GET broadcasts/{username} -> cache {cache_state} -> 200", flush=True)
    return Response(body, status=200, content_type="application/json")


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.get("/api/dashboard")
def dashboard_data():
    data = metrics.dashboard(request.args.get("window", "1h"))
    data["state"] = current_state()
    return jsonify(data)


@app.post("/api/admin/cache/flush")
def flush_cache_route():
    entry_count = flush_cache()
    metrics.record_event("cache_flush", f"entries={entry_count}")
    return jsonify(status="ok", cleared=entry_count, state=current_state())


@app.post("/api/admin/session/recreate")
def recreate_session_route():
    global cooldown_until, next_request_at

    try:
        with solver_lock:
            rotate_session("manual")
            cooldown_until = 0.0
            next_request_at = time.monotonic() + MIN_REQUEST_INTERVAL_SECONDS
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        print(f"Manual session recreation failed: {exc}", flush=True)
        return jsonify(status="error", error=str(exc)), 502

    return jsonify(status="ok", state=current_state())


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def proxy(path):
    started_at = time.monotonic()
    url = f"{UPSTREAM}/{path}"
    username = broadcast_username(path)
    model_id = cam_model_id(path)
    endpoint = "broadcasts" if username else "cam" if model_id else "other"

    if not username:
        username = username_for_model(model_id)

    def finish(
        response,
        adapter_status,
        *,
        cache_state="none",
        upstream_status=None,
        blocked=False,
        retries=0,
    ):
        metrics.record_request(
            endpoint=endpoint,
            username=username,
            model_id=model_id,
            cache_state=cache_state,
            adapter_status=adapter_status,
            upstream_status=upstream_status,
            latency_ms=round((time.monotonic() - started_at) * 1000),
            cloudflare_block=blocked,
            retry_count=retries,
        )
        return response

    if endpoint == "broadcasts":
        response = cached_response(username)
        if response is not None:
            return finish(response, 200, cache_state="hit")

    if request.query_string:
        url += "?" + request.query_string.decode()

    retries = 0
    blocked = False

    try:
        data, retries, blocked = request_with_browser_session(url)

    except requests.Timeout:
        if endpoint == "broadcasts":
            response = cached_response(username, allow_stale=True)
            if response is not None:
                return finish(response, 200, cache_state="stale")
        response = jsonify(error="FlareSolverr timeout")
        response.status_code = 504
        return finish(response, 504, cache_state="miss" if endpoint == "broadcasts" else "none")

    except (requests.RequestException, ValueError, RuntimeError) as exc:
        print(f"FlareSolverr connection error: {exc}", flush=True)
        if endpoint == "broadcasts":
            response = cached_response(username, allow_stale=True)
            if response is not None:
                return finish(response, 200, cache_state="stale")
        response = jsonify(error="FlareSolverr connection error")
        response.status_code = 502
        return finish(response, 502, cache_state="miss" if endpoint == "broadcasts" else "none")

    if data.get("status") != "ok":
        print(
            f"GET /{path} -> FlareSolverr failed: {data.get('message')}",
            flush=True,
        )
        if endpoint == "broadcasts":
            response = cached_response(username, allow_stale=True)
            if response is not None:
                return finish(
                    response,
                    200,
                    cache_state="stale",
                    blocked=blocked,
                    retries=retries,
                )
        response = jsonify(error="FlareSolverr failed")
        response.status_code = 502
        return finish(
            response,
            502,
            cache_state="miss" if endpoint == "broadcasts" else "none",
            blocked=blocked,
            retries=retries,
        )

    solution = data.get("solution")

    if not solution:
        response = jsonify(error="Missing FlareSolverr solution")
        response.status_code = 502
        return finish(
            response,
            502,
            cache_state="miss" if endpoint == "broadcasts" else "none",
            blocked=blocked,
            retries=retries,
        )

    status = solution.get("status", 502)
    body = solution.get("response", "")

    # Chrome sometimes wraps JSON in:
    # <html><body><pre>...</pre></body></html>
    match = re.search(
        r"<pre[^>]*>(.*?)</pre>",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if match:
        body = html.unescape(match.group(1))

    content_type = "text/html; charset=utf-8"

    try:
        json.loads(body)
        content_type = "application/json"
    except (ValueError, TypeError):
        pass

    if status == 200 and endpoint == "broadcasts":
        store_broadcast(username, body)
    elif status in (404, 410):
        if endpoint == "broadcasts":
            invalidate_broadcast(username)
        elif model_id:
            invalidate_model(model_id)
    elif endpoint == "broadcasts" and (status == 403 or status == 429 or status >= 500):
        response = cached_response(username, allow_stale=True)
        if response is not None:
            return finish(
                response,
                200,
                cache_state="stale",
                upstream_status=status,
                blocked=blocked,
                retries=retries,
            )

    print(
        f"GET /{path} -> {url} -> {status}",
        flush=True,
    )

    response = Response(
        body,
        status=status,
        content_type=content_type,
    )
    return finish(
        response,
        status,
        cache_state="miss" if endpoint == "broadcasts" else "none",
        upstream_status=status,
        blocked=blocked,
        retries=retries,
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080,
        ssl_context=(
            "/certs/server.crt",
            "/certs/server.key",
        ),
    )
