import json
import os
import tempfile
import unittest

import rules
from watcher import Watcher, find_incidents


def replica_set(name, deployment):
    return {"metadata": {"name": name, "ownerReferences": [{"kind": "Deployment", "name": deployment}]}}


def pod(name, owner, restarts, ready, reason="Error"):
    status = {
        "name": "main",
        "ready": ready,
        "restartCount": restarts,
        "lastState": {"terminated": {"reason": reason, "exitCode": 2}},
    }
    return {"metadata": {"name": name, "ownerReferences": [{"name": owner}]}, "status": {"containerStatuses": [status]}}


REPLICA_SETS = [replica_set("rs-new", "cart"), replica_set("rs-old", "cart"), replica_set("rs-pay", "payment")]


class FindIncidentsTest(unittest.TestCase):
    def test_failing_pod_is_an_incident(self):
        incident = find_incidents([pod("p", "rs-new", 2, False)], REPLICA_SETS)["rs-new"]
        self.assertEqual(incident["deployment"], "cart")
        self.assertEqual((incident["restarts"], incident["exit_code"]), (2, 2))

    def test_single_restart_is_ignored(self):
        self.assertEqual(find_incidents([pod("p", "rs-new", 1, False)], REPLICA_SETS), {})

    def test_healthy_pod_with_old_restarts_is_ignored(self):
        self.assertEqual(find_incidents([pod("p", "rs-old", 9, True)], REPLICA_SETS), {})

    def test_replicas_of_same_revision_are_one_incident(self):
        pods = [pod("a", "rs-new", 2, False), pod("b", "rs-new", 3, False)]
        self.assertEqual(len(find_incidents(pods, REPLICA_SETS)), 1)

    def test_unowned_pod_is_ignored(self):
        self.assertEqual(find_incidents([pod("p", "otro", 5, False)], REPLICA_SETS), {})

    def test_pod_without_status_is_ignored(self):
        self.assertEqual(find_incidents([{"metadata": {"name": "p"}}], REPLICA_SETS), {})


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events_file = os.path.join(self.tmp.name, "events.jsonl")
        self.sent = []
        self.responses = []

    def send(self, payload):
        self.sent.append(payload)
        return self.responses.pop(0) if self.responses else {"status": "executed"}

    def watcher(self):
        return Watcher(send=self.send, events_file=self.events_file)

    def events(self):
        with open(self.events_file, encoding="utf-8") as events:
            return [json.loads(line)["event"] for line in events]

    def test_proposes_rollback_once(self):
        watcher = self.watcher()
        failing = [pod("p", "rs-new", 2, False)]
        watcher.tick(failing, REPLICA_SETS)
        watcher.tick(failing, REPLICA_SETS)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["action"], rules.propose(find_incidents(failing, REPLICA_SETS)["rs-new"]))

    def test_incident_id_links_detection_and_action(self):
        self.watcher().tick([pod("p", "rs-new", 2, False)], REPLICA_SETS)
        with open(self.events_file, encoding="utf-8") as events:
            detected = json.loads(events.readline())
        self.assertEqual(detected["incident_id"], self.sent[0]["incident_id"])

    def test_closes_when_failure_disappears(self):
        watcher = self.watcher()
        watcher.tick([pod("p", "rs-new", 2, False)], REPLICA_SETS)
        watcher.tick([], REPLICA_SETS)
        self.assertEqual(self.events(), ["incident_detected", "incident_closed"])

    def test_recurring_failure_is_a_new_incident(self):
        watcher = self.watcher()
        failing = [pod("p", "rs-new", 2, False)]
        watcher.tick(failing, REPLICA_SETS)
        watcher.tick([], REPLICA_SETS)
        watcher.tick(failing, REPLICA_SETS)
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(self.sent[0]["incident_id"], self.sent[1]["incident_id"])

    def test_denied_action_escalates_with_reason(self):
        self.responses = [{"status": "denied", "reasons": ["target no permitido"]}]
        self.watcher().tick([pod("p", "rs-pay", 2, False)], REPLICA_SETS)
        action = self.sent[1]["action"]
        self.assertEqual(action["type"], "escalate")
        self.assertIn("target no permitido", action["params"]["summary"])

    def test_unreachable_executor_retries_without_duplicate_detection(self):
        self.responses = [{"status": "unreachable"}]
        watcher = self.watcher()
        failing = [pod("p", "rs-new", 2, False)]
        watcher.tick(failing, REPLICA_SETS)
        watcher.tick(failing, REPLICA_SETS)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.events(), ["incident_detected"])

    def test_escalation_by_proposer_is_not_repeated(self):
        self.responses = [{"status": "escalated"}]
        self.watcher().tick([pod("p", "rs-new", 2, False)], REPLICA_SETS)
        self.assertEqual(len(self.sent), 1)

    def test_independent_incidents_are_handled_separately(self):
        pods = [pod("a", "rs-new", 2, False), pod("b", "rs-pay", 2, False)]
        self.watcher().tick(pods, REPLICA_SETS)
        self.assertEqual({p["action"]["target"] for p in self.sent}, {"cart", "payment"})


if __name__ == "__main__":
    unittest.main()
