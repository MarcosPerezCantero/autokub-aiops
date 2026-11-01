#!/usr/bin/env python3
"""Vista del flujo: muestra en el navegador los eventos de cada incidente.

Solo lectura: no tiene credenciales del clúster ni forma de actuar sobre él,
y el resto del sistema funciona igual sin ella (docs/arquitectura.md).
"""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

EVENTS_FILE = os.environ.get("AUTOKUB_EVENTS", ".local/events.jsonl")
ADDRESS = ("127.0.0.1", int(os.environ.get("AUTOKUB_VIEWER_PORT", "8091")))
PAGE = Path(__file__).with_name("index.html")
MAX_EVENTS = 500
# Los eventos pueden contener texto de logs: la página nunca carga nada externo
SECURITY_POLICY = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'"


def read_events(events_file, after):
    """Devuelve los eventos posteriores a `after` y el total. Ignora una línea a medio escribir."""
    try:
        with open(events_file, encoding="utf-8") as events:
            lines = [line for line in events if line.strip()]
    except FileNotFoundError:
        return [], 0
    total = len(lines)
    if after > total:  # El fichero se ha reiniciado
        after = 0
    records = []
    for line in lines[max(after, total - MAX_EVENTS):]:
        try:
            records.append(json.loads(line))
        except ValueError:
            total -= 1
            break
    return records, total


class Handler(BaseHTTPRequestHandler):
    def send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.send(200, "text/html; charset=utf-8", PAGE.read_bytes())
        elif url.path == "/api/events":
            try:
                after = max(int(parse_qs(url.query).get("after", ["0"])[0]), 0)
            except ValueError:
                after = 0
            records, total = read_events(EVENTS_FILE, after)
            body = json.dumps({"events": records, "total": total}, ensure_ascii=False).encode()
            self.send(200, "application/json; charset=utf-8", body)
        else:
            self.send(404, "text/plain; charset=utf-8", b"no encontrado")

    def log_message(self, *args):
        pass


def main():
    print(f"Vista del flujo en http://localhost:{ADDRESS[1]}", flush=True)
    HTTPServer(ADDRESS, Handler).serve_forever()


if __name__ == "__main__":
    main()
