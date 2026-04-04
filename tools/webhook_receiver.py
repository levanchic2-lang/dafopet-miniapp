from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            data = {"_raw": body.decode("utf-8", errors="replace")}

        print("\n=== webhook received ===")
        print("path:", self.path)
        print("headers:", dict(self.headers))
        print("json:", json.dumps(data, ensure_ascii=False, indent=2))

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"{\"ok\":true}")


if __name__ == "__main__":
    host = "127.0.0.1"
    port = 9877
    print(f"listening on http://{host}:{port}/webhook")
    HTTPServer((host, port), Handler).serve_forever()

