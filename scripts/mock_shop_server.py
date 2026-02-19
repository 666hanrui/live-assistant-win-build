#!/usr/bin/env python3
import argparse
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
MOCK_HTML = ROOT / "stress" / "mock_shop" / "mock_tiktok_shop.html"


class MockHandler(BaseHTTPRequestHandler):
    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            html = f"""
            <!doctype html><html><head><meta charset="utf-8"><title>Mock Shop Entry</title></head>
            <body style="font-family:Segoe UI,Microsoft YaHei,sans-serif;padding:24px;">
              <h2>Mock TikTok Shop 助播页面</h2>
              <ul>
                <li><a href="/streamer/live/product/dashboard?mock_tiktok_shop=1&view=dashboard_idle">控制台(未开播)</a></li>
                <li><a href="/streamer/live/product/dashboard?mock_tiktok_shop=1&view=dashboard_live">控制台(开播)</a></li>
                <li><a href="/workbench/live/overview?mock_tiktok_shop=1">直播大屏</a></li>
              </ul>
              <p>当前文件：{MOCK_HTML}</p>
            </body></html>
            """
            return self._send_html(html)

        if path.startswith("/streamer/live/product/dashboard") or path.startswith("/workbench/live/overview"):
            if not MOCK_HTML.exists():
                return self._send_html(f"<h1>404</h1><p>Mock file not found: {MOCK_HTML}</p>", status=404)
            return self._send_html(MOCK_HTML.read_text(encoding="utf-8"))

        self._send_html("<h1>404 Not Found</h1>", status=404)


def main():
    parser = argparse.ArgumentParser(description="Serve mock TikTok Shop assistant pages for local testing.")
    parser.add_argument("--host", default=os.getenv("MOCK_SHOP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MOCK_SHOP_PORT", "9100")))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    print(f"Mock server started: http://{args.host}:{args.port}")
    print(f"Dashboard idle: http://{args.host}:{args.port}/streamer/live/product/dashboard?mock_tiktok_shop=1&view=dashboard_idle")
    print(f"Dashboard live: http://{args.host}:{args.port}/streamer/live/product/dashboard?mock_tiktok_shop=1&view=dashboard_live")
    print(f"Overview:      http://{args.host}:{args.port}/workbench/live/overview?mock_tiktok_shop=1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
