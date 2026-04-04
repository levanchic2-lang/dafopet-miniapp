from __future__ import annotations

import hmac
import hashlib
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = ""  # 可填入与你 .env 里 SMS_GATEWAY_SECRET 相同的值做验签


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""

        sig = self.headers.get("X-Signature", "")
        if SECRET:
            calc = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
            sig_ok = (sig.lower() == calc.lower())
        else:
            sig_ok = True

        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            data = {"_raw": body.decode("utf-8", errors="replace")}

        print("\n=== sms gateway received ===")
        print("path:", self.path)
        print("x-signature:", sig, "ok" if sig_ok else "BAD")
        print("json:", json.dumps(data, ensure_ascii=False, indent=2))

        if not sig_ok:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"{\"ok\":false,\"error\":\"bad signature\"}")
            return

        # 这里模拟“已下发短信”
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"{\"ok\":true,\"provider\":\"mock\"}")


if __name__ == "__main__":
    host = "127.0.0.1"
    port = 9878
    print(f"listening on http://{host}:{port}/sms")
    HTTPServer((host, port), Handler).serve_forever()

