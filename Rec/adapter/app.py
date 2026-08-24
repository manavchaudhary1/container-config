import os
import re
import html
import json

import requests
from flask import Flask, request, Response, jsonify


app = Flask(__name__)

FLARESOLVERR_URL = os.getenv(
    "FLARESOLVERR_URL",
    "http://flaresolverr:8191/v1",
)

UPSTREAM = "https://stripchat.com"


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def proxy(path):
    url = f"{UPSTREAM}/{path}"

    if request.query_string:
        url += "?" + request.query_string.decode()

    try:
        response = requests.post(
            FLARESOLVERR_URL,
            json={
                "cmd": "request.get",
                "url": url,
                "maxTimeout": 60000,
            },
            timeout=70,
        )

    except requests.Timeout:
        return jsonify(error="FlareSolverr timeout"), 504

    except requests.RequestException as exc:
        print(f"FlareSolverr connection error: {exc}", flush=True)
        return jsonify(error="FlareSolverr connection error"), 502

    try:
        data = response.json()
    except ValueError:
        return jsonify(error="Invalid FlareSolverr response"), 502

    if data.get("status") != "ok":
        print(
            f"GET /{path} -> FlareSolverr failed: {data.get('message')}",
            flush=True,
        )
        return jsonify(error="FlareSolverr failed"), 502

    solution = data.get("solution")

    if not solution:
        return jsonify(error="Missing FlareSolverr solution"), 502

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

    print(
        f"GET /{path} -> {url} -> {status}",
        flush=True,
    )

    return Response(
        body,
        status=status,
        content_type=content_type,
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