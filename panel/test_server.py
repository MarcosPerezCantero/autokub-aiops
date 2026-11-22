import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

from server import EventStore, allowed_host, load_info, make_handler, merge, parse_lines, read_file, tag


def event(timestamp, name, incident="inc-1"):
    return {"incident_id": incident, "timestamp": timestamp, "event": name, "summary": "", "data": {}}


class EventsTest(unittest.TestCase):
    def test_parse_ignores_non_event_lines(self):
        lines = ["Detector en marcha", json.dumps(event("t", "a")), '{"x":', json.dumps({"no": "event"}), "[1]"]
        self.assertEqual([e["event"] for e in parse_lines(lines)], ["a"])

    def test_merge_orders_by_time_and_keeps_order_within_a_component(self):
        agent = [event("2026-01-01T00:00:01.100+00:00", "detected"), event("2026-01-01T00:00:01.900+00:00", "diagnosis")]
        executor = [event("2026-01-01T00:00:01.500+00:00", "x1"), event("2026-01-01T00:00:01.500+00:00", "x2")]
        self.assertEqual([e["event"] for e in merge(agent, executor)], ["detected", "x1", "x2", "diagnosis"])

    def test_missing_file(self):
        self.assertEqual(read_file("/no/existe.jsonl"), [])

    def test_store_keeps_last_good_events_when_reading_fails(self):
        results = [[event("t", "a")], OSError("forbidden")]

        def read():
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        store = EventStore(read)
        store.refresh()
        _, version, _ = store.snapshot()
        store.refresh()
        events, same_version, error = store.snapshot()
        self.assertEqual((len(events), same_version, error), (1, version, "forbidden"))

    def test_info_has_no_secrets(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as file:
            json.dump({"mode": "approve", "namespaces": {"b": {}, "a": {}}, "model": {"name": "m"}}, file)
        self.addCleanup(os.unlink, file.name)
        self.assertEqual(load_info(file.name), {"mode": "approve", "namespaces": ["a", "b"]})

    def test_allowed_hosts(self):
        for host in ("localhost:8091", "127.0.0.1:9000", "[::1]:8091", "localhost"):
            self.assertTrue(allowed_host(host, frozenset()))
        for host in ("evil.example:8091", "localhost.evil.example", "", "127.0.0.1.nip.io:8091"):
            self.assertFalse(allowed_host(host, frozenset()))
        self.assertTrue(allowed_host("panel.example", frozenset({"panel.example"})))


class SourceTest(unittest.TestCase):
    def test_source_is_set_by_the_panel_not_by_the_event(self):
        """Un agente manipulado no puede hacerse pasar por el ejecutor escribiendo el campo él mismo."""
        forged = {**event("t", "action_pending_approval"), "component": "executor", "_source": "executor"}
        self.assertEqual(tag([forged], "agent")[0]["_source"], "agent")


class Upstream(BaseHTTPRequestHandler):
    """Hace de ejecutor y de chat: guarda lo que recibe."""

    received = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Upstream.received.append((self.path, self.headers.get("Authorization"), body))
        payload = json.dumps({"status": "executed", "answer": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class HttpTest(unittest.TestCase):
    TOKEN = "t" * 40
    APPROVAL = "/api/approvals/apr-0123456789ab"

    def setUp(self):
        Upstream.received = []
        upstream = HTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.shutdown)
        base = self.base = f"http://127.0.0.1:{upstream.server_address[1]}"
        store = EventStore(lambda: [event("t", "incident_detected")])
        store.refresh()
        self.start(make_handler(store, {"mode": "approve", "namespaces": ["otel-demo"]}, self.TOKEN, base, base + "/chat"))

    def start(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def request(self, path, body=None, headers=None):
        default = {"Content-Type": "application/json", "X-Autokub-Panel": "1"} if body is not None else {}
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers={**default, **(headers or {})})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_page_has_strict_security_headers(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        with urllib.request.urlopen(request) as response:
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_events_are_only_sent_when_they_change(self):
        _, body = self.request("/api/events")
        first = json.loads(body)
        self.assertEqual(len(first["events"]), 1)
        _, body = self.request(f"/api/events?version={first['version']}")
        self.assertNotIn("events", json.loads(body))

    def test_foreign_host_is_refused(self):
        self.assertEqual(self.request("/api/events", headers={"Host": "evil.example"})[0], 403)

    def test_approval_is_forwarded_with_token_and_only_id_and_decision(self):
        body = {"decision": "approve", "by": "ana", "action": {"type": "rollout_undo", "target": "x"}}
        code, _ = self.request(self.APPROVAL, body)
        self.assertEqual(code, 200)
        path, authorization, forwarded = Upstream.received[0]
        self.assertEqual(path, "/approvals/apr-0123456789ab")
        self.assertEqual(authorization, f"Bearer {self.TOKEN}")
        self.assertEqual(forwarded, {"decision": "approve", "by": "ana"})

    def test_cross_site_requests_are_refused(self):
        for headers in (
            {"X-Autokub-Panel": ""},
            {"Content-Type": "text/plain"},
            {"Origin": "http://evil.example"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(self.APPROVAL, {"decision": "approve"}, headers)[0], 403)
        self.assertEqual(Upstream.received, [])

    def test_same_origin_is_accepted(self):
        origin = {"Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.request(self.APPROVAL, {"decision": "reject"}, origin)[0], 200)

    def test_invalid_approval_id_is_not_forwarded(self):
        self.assertEqual(self.request("/api/approvals/..%2Factions", {"decision": "approve"})[0], 400)
        self.assertEqual(Upstream.received, [])

    def test_without_token_approvals_are_disabled(self):
        store = EventStore(list)
        self.start(make_handler(store, {"mode": "approve", "namespaces": []}, None, "http://127.0.0.1:1", ""))
        self.assertEqual(self.request(self.APPROVAL, {"decision": "approve"})[0], 403)
        _, body = self.request("/api/info")
        self.assertEqual(json.loads(body), {"mode": "approve", "namespaces": [], "approvals": False, "chat": False})

    def test_token_is_read_on_each_approval(self):
        current = {"token": None}
        store = EventStore(list)
        self.start(make_handler(store, {"mode": "approve", "namespaces": []}, lambda: current["token"], self.base, ""))
        self.assertEqual(self.request(self.APPROVAL, {"decision": "approve"})[0], 403)
        current["token"] = "nuevo" * 8
        self.assertEqual(self.request(self.APPROVAL, {"decision": "approve"})[0], 200)
        self.assertEqual(Upstream.received[-1][1], f"Bearer {current['token']}")

    def test_https_origin_behind_a_proxy_is_accepted(self):
        origin = {"Origin": f"https://127.0.0.1:{self.port}"}
        self.assertEqual(self.request(self.APPROVAL, {"decision": "reject"}, origin)[0], 200)

    def test_chat_is_forwarded_without_token(self):
        code, body = self.request("/api/chat", {"namespace": "otel-demo", "question": "¿qué pasa?"})
        self.assertEqual((code, json.loads(body)["answer"]), (200, "ok"))
        self.assertEqual(Upstream.received[0][:2], ("/chat", None))


if __name__ == "__main__":
    unittest.main()
