#!/usr/bin/env python3
"""Panel de operación: incidentes, razonamiento del agente, decisiones de OPA, aprobaciones y chat.

No tiene permisos sobre la aplicación. Lee los eventos (del fichero del laboratorio o de los logs
de los pods de autokub) y reenvía dos tipos de petición: decisiones sobre acciones pendientes al
ejecutor, que vuelve a consultar a OPA antes de actuar, y preguntas al chat de solo lectura del
agente. Diseño: docs/adr/0009-panel-de-operacion.md
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Origen de los eventos: un fichero (laboratorio) o los logs de los pods (clúster)
EVENTS_FILE = os.environ.get("AUTOKUB_EVENTS", ".local/events.jsonl")
SOURCE = os.environ.get("AUTOKUB_PANEL_SOURCE", "file")  # file | kubernetes
SYSTEM_NAMESPACE = os.environ.get("AUTOKUB_PANEL_NAMESPACE", "autokub-system")
KUBECONFIG = os.environ.get("AUTOKUB_PANEL_KUBECONFIG", "")
CONFIG_FILE = os.environ.get("AUTOKUB_CONFIG", "config/lab.json")
EXECUTOR_URL = os.environ.get("AUTOKUB_EXECUTOR_BASE", "http://127.0.0.1:8090")
CHAT_URL = os.environ.get("AUTOKUB_CHAT_URL", "http://127.0.0.1:8092/chat")
APPROVAL_TOKEN_FILE = os.environ.get("AUTOKUB_APPROVAL_TOKEN_FILE", ".local/approval.token")
# Por defecto solo escucha en localhost: en el clúster se llega con kubectl port-forward,
# así que quién puede operar lo decide el RBAC de Kubernetes (pods/portforward).
ADDRESS = (os.environ.get("AUTOKUB_PANEL_HOST", "127.0.0.1"), int(os.environ.get("AUTOKUB_PANEL_PORT", "8091")))
# Nombres con los que se puede abrir el panel, además de localhost (detrás de un proxy, por ejemplo)
EXTRA_HOSTS = {host for host in os.environ.get("AUTOKUB_PANEL_ALLOWED_HOSTS", "").split(",") if host}

PAGE = Path(__file__).with_name("index.html")
MAX_EVENTS = 3000
MAX_BODY_BYTES = 32768
REFRESH_SECONDS = 2
APPROVAL_ID = re.compile(r"^apr-[0-9a-f]{12}$")
# Los eventos pueden contener texto de logs: la página nunca carga nada externo
SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
# Cabecera que el navegador no envía desde otra web sin una comprobación previa que el panel no acepta
CSRF_HEADER = "X-Autokub-Panel"


def parse_lines(lines):
    """Eventos válidos de una lista de líneas. Ignora el resto (mensajes de arranque, líneas a medias)."""
    events = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("event"), str) and "timestamp" in record:
            events.append(record)
    return events


def merge(*streams):
    """Une los eventos de varios componentes por marca de tiempo; dentro de cada uno conserva el orden."""
    tagged = [(event.get("timestamp", ""), index, n, event) for index, stream in enumerate(streams) for n, event in enumerate(stream)]
    tagged.sort(key=lambda item: (str(item[0]), item[1], item[2]))
    return [event for *_, event in tagged][-MAX_EVENTS:]


def read_file(path):
    try:
        with open(path, encoding="utf-8") as events:
            return parse_lines(events)
    except FileNotFoundError:
        return []


def kubectl_logs(deployment, container):
    kubeconfig = ["--kubeconfig", KUBECONFIG] if KUBECONFIG else []
    command = [
        "kubectl", *kubeconfig, "-n", SYSTEM_NAMESPACE, "logs", f"deployment/{deployment}",
        "-c", container, f"--tail={MAX_EVENTS}",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise OSError(result.stderr.strip()[:200])
    return result.stdout.splitlines()


class EventStore:
    """Mantiene la lista de eventos actualizada en segundo plano y la versión de su contenido."""

    def __init__(self, read):
        self.read = read
        self.lock = threading.Lock()
        self.events, self.version, self.error = [], "", None

    def refresh(self):
        try:
            events, error = self.read(), None
        except (OSError, subprocess.SubprocessError) as failure:
            events, error = None, str(failure) or type(failure).__name__
        with self.lock:
            if events is not None:
                self.events = events
                self.version = hashlib.sha256(json.dumps(events, sort_keys=True).encode()).hexdigest()[:16]
            self.error = error

    def snapshot(self):
        with self.lock:
            return self.events, self.version, self.error

    def run(self):
        while True:
            self.refresh()
            time.sleep(REFRESH_SECONDS)


def tag(events, source):
    """Marca cada evento con el pod del que se leyó. Lo asigna el panel, no el contenido del evento:
    el campo component lo declara quien escribe, y el agente podría imprimir eventos del ejecutor."""
    for event in events:
        event["_source"] = source
    return events


def make_reader(source=SOURCE):
    if source == "kubernetes":
        return lambda: merge(
            tag(parse_lines(kubectl_logs("autokub-agent", "agent")), "agent"),
            tag(parse_lines(kubectl_logs("autokub-executor", "executor")), "executor"),
        )
    if source == "file":
        # Laboratorio: todos los procesos escriben en el mismo fichero y no se puede distinguir el origen
        return lambda: tag(read_file(EVENTS_FILE), "file")
    raise ValueError(f"origen de eventos no válido: {source!r}")


def load_info(path=CONFIG_FILE):
    """Lo que la página necesita saber de la configuración. Sin secretos."""
    try:
        with open(path, encoding="utf-8") as file:
            config = json.load(file)
    except (OSError, ValueError):
        config = {}
    return {"mode": config.get("mode", "propose"), "namespaces": sorted(config.get("namespaces", {}))}


def load_token(path=APPROVAL_TOKEN_FILE):
    try:
        with open(path, encoding="utf-8") as file:
            return file.read().strip() or None
    except OSError:
        return None


def forward(url, body, token=None, timeout=10):
    """Reenvía una petición JSON y devuelve (código, cuerpo)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        except ValueError:
            return error.code, {"error": f"HTTP {error.code}"}
    except (OSError, ValueError):
        return 502, {"error": "no responde"}


LOCAL_HOST = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?$")


def allowed_host(host, extra=frozenset(EXTRA_HOSTS)):
    """Evita el DNS rebinding: una web ajena que resuelva su nombre a 127.0.0.1 no puede leer el panel.
    El puerto da igual: con kubectl port-forward, el local lo elige quien abre el túnel."""
    return bool(LOCAL_HOST.match(host)) or host in extra


def make_handler(store, info, token, executor_url=EXECUTOR_URL, chat_url=CHAT_URL, extra_hosts=frozenset(EXTRA_HOSTS)):
    class Handler(BaseHTTPRequestHandler):
        def send(self, code, content_type, body):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", SECURITY_POLICY)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def json(self, code, body):
            self.send(code, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False).encode())

        def host_ok(self):
            if allowed_host(self.headers.get("Host", ""), extra_hosts):
                return True
            self.send(403, "text/plain; charset=utf-8", b"host no permitido")
            return False

        def do_GET(self):
            if not self.host_ok():
                return
            url = urlparse(self.path)
            if url.path == "/":
                self.send(200, "text/html; charset=utf-8", PAGE.read_bytes())
            elif url.path == "/api/events":
                events, version, error = store.snapshot()
                known = parse_qs(url.query).get("version", [""])[0]
                body = {"version": version, "error": error}
                if known != version:
                    body["events"] = events
                self.json(200, body)
            elif url.path == "/api/info":
                self.json(200, {**info, "approvals": bool(token() if callable(token) else token), "chat": bool(chat_url)})
            else:
                self.send(404, "text/plain; charset=utf-8", b"no encontrado")

        def read_request(self):
            """Solo peticiones del propio panel: cabecera propia, JSON y mismo origen."""
            origin = self.headers.get("Origin")
            if (
                self.headers.get(CSRF_HEADER) != "1"
                or not self.headers.get("Content-Type", "").startswith("application/json")
                or (origin is not None and origin not in (f"http://{self.headers.get('Host')}", f"https://{self.headers.get('Host')}"))
            ):
                self.json(403, {"error": "petición no permitida"})
                return None
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not 0 < length <= MAX_BODY_BYTES:
                self.json(400, {"error": "petición no válida"})
                return None
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                body = None
            if not isinstance(body, dict):
                self.json(400, {"error": "JSON no válido"})
                return None
            return body

        def do_POST(self):
            if not self.host_ok():
                return
            path = urlparse(self.path).path
            if path.startswith("/api/approvals/"):
                approval_id = path[len("/api/approvals/"):]
                body = self.read_request()
                if body is None:
                    return
                current = token() if callable(token) else token
                if not current:
                    self.json(403, {"error": "las aprobaciones no están activadas"})
                    return
                if not APPROVAL_ID.match(approval_id):
                    self.json(400, {"error": "identificador no válido"})
                    return
                # El panel solo envía el identificador y la decisión: nunca una acción
                decision = {"decision": body.get("decision"), "by": body.get("by")}
                self.json(*forward(f"{executor_url}/approvals/{approval_id}", decision, current, timeout=60))
            elif path == "/api/chat":
                body = self.read_request()
                if body is None:
                    return
                if not chat_url:
                    self.json(403, {"error": "el chat no está activado"})
                    return
                self.json(*forward(chat_url, body, timeout=180))
            else:
                self.send(404, "text/plain; charset=utf-8", b"no encontrado")

        def log_message(self, *args):
            pass

    return Handler


def main():
    store = EventStore(make_reader())
    threading.Thread(target=store.run, daemon=True).start()
    # El token se lee en cada aprobación: si el Secret cambia, el panel y el ejecutor usan el mismo
    handler = make_handler(store, load_info(), load_token)
    print(f"Panel de operación en http://localhost:{ADDRESS[1]}", flush=True)
    ThreadingHTTPServer(ADDRESS, handler).serve_forever()


if __name__ == "__main__":
    main()
