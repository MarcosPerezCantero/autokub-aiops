import json
import os
import tempfile
import unittest

import rules
import watcher as module
from watcher import Watcher, deployment_health, find_incidents, load_config


def replica_set(name, deployment, revision=1):
    return {
        "metadata": {
            "name": name,
            "ownerReferences": [{"kind": "Deployment", "name": deployment}],
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
        }
    }


def pod(name, owner, restarts, ready, reason="Error"):
    status = {
        "name": "main",
        "ready": ready,
        "restartCount": restarts,
        "lastState": {"terminated": {"reason": reason, "exitCode": 2}},
    }
    return {"metadata": {"name": name, "ownerReferences": [{"name": owner}]}, "status": {"containerStatuses": [status]}}


def deployment(name="cart", revision=2, replicas=1, updated=1, ready=1, unavailable=None, observed=1):
    status = {"observedGeneration": observed, "updatedReplicas": updated, "readyReplicas": ready, "replicas": replicas}
    if unavailable:
        status["unavailableReplicas"] = unavailable
    return {
        "metadata": {
            "name": name,
            "generation": 1,
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
        },
        "spec": {"replicas": 1},
        "status": status,
    }


REPLICA_SETS = [
    replica_set("rs-old", "cart", 1),
    replica_set("rs-new", "cart", 2),
    replica_set("rs-pay", "payment", 1),
]
FAILING = [pod("old", "rs-old", 0, True), pod("p", "rs-new", 2, False)]
BROKEN = [deployment(replicas=2, updated=1, ready=1, unavailable=1)]
RECOVERED_PODS = [pod("old", "rs-new", 0, True)]
RECOVERED = [deployment()]


class FindIncidentsTest(unittest.TestCase):
    def test_failing_pod_is_an_incident(self):
        incident = find_incidents([pod("p", "rs-new", 2, False)], REPLICA_SETS, "otel-demo")["cart"]
        self.assertEqual(incident["replica_set"], "rs-new")
        self.assertEqual((incident["restarts"], incident["exit_code"]), (2, 2))

    def test_single_restart_is_ignored(self):
        self.assertEqual(find_incidents([pod("p", "rs-new", 1, False)], REPLICA_SETS, "otel-demo"), {})

    def test_healthy_pod_with_old_restarts_is_ignored(self):
        self.assertEqual(find_incidents([pod("p", "rs-old", 9, True)], REPLICA_SETS, "otel-demo"), {})

    def test_replicas_of_same_deployment_are_one_incident(self):
        pods = [pod("a", "rs-new", 2, False), pod("b", "rs-new", 3, False)]
        self.assertEqual(len(find_incidents(pods, REPLICA_SETS, "otel-demo")), 1)

    def test_unowned_pod_is_ignored(self):
        self.assertEqual(find_incidents([pod("p", "otro", 5, False)], REPLICA_SETS, "otel-demo"), {})

    def test_pod_without_status_is_ignored(self):
        self.assertEqual(find_incidents([{"metadata": {"name": "p"}}], REPLICA_SETS, "otel-demo"), {})


class HealthTest(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(deployment_health(deployment(), REPLICA_SETS, RECOVERED_PODS), (True, 0))

    def test_rollout_hidden_by_old_replica_is_not_healthy(self):
        healthy, restarts = deployment_health(BROKEN[0], REPLICA_SETS, FAILING)
        self.assertFalse(healthy)
        self.assertEqual(restarts, 2)

    def test_reports_ready_as_one_of_one_but_new_pod_failing(self):
        state = deployment(updated=1, ready=1, replicas=1)
        self.assertFalse(deployment_health(state, REPLICA_SETS, [pod("p", "rs-new", 3, False)])[0])

    def test_old_generation_is_not_healthy(self):
        self.assertFalse(deployment_health(deployment(observed=0), REPLICA_SETS, RECOVERED_PODS)[0])

    def test_no_pods_is_not_healthy(self):
        self.assertFalse(deployment_health(deployment(), REPLICA_SETS, [])[0])

    def test_pods_of_old_revision_do_not_count(self):
        self.assertFalse(deployment_health(deployment(), REPLICA_SETS, [pod("p", "rs-old", 0, True)])[0])


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events_file = os.path.join(self.tmp.name, "events.jsonl")
        self.sent = []
        self.responses = []
        self.now = 0.0

    def send(self, payload):
        self.sent.append(payload)
        if self.responses:
            return self.responses.pop(0)
        if payload["action"]["type"] == "escalate":
            return {"status": "escalated"}
        return {"status": "executed"}

    def watcher(self, propose=rules.propose):
        return Watcher(propose=propose, send=self.send, events_file=self.events_file, clock=lambda: self.now)

    def advance(self, watcher, seconds, pods, deployments):
        for _ in range(int(seconds // 5)):
            self.now += 5
            watcher.tick(pods, REPLICA_SETS, deployments, "otel-demo")

    def events(self):
        if not os.path.exists(self.events_file):
            return []
        with open(self.events_file, encoding="utf-8") as events:
            return [json.loads(line)["event"] for line in events]

    def actions(self):
        return [payload["action"]["type"] for payload in self.sent]

    def test_proposes_once_while_verifying(self):
        watcher = self.watcher()
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        self.assertEqual(self.actions(), ["rollout_undo"])

    def test_incident_id_links_detection_and_action(self):
        self.watcher().tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        with open(self.events_file, encoding="utf-8") as events:
            detected = json.loads(events.readline())
        self.assertEqual(detected["incident_id"], self.sent[0]["incident_id"])

    def test_closes_only_after_stable_period(self):
        watcher = self.watcher()
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        self.advance(watcher, 25, RECOVERED_PODS, RECOVERED)
        self.assertEqual(self.events(), ["incident_detected"])
        self.advance(watcher, 15, RECOVERED_PODS, RECOVERED)
        self.assertEqual(self.events(), ["incident_detected", "verification_passed", "incident_closed"])
        self.assertEqual(watcher.active, {})

    def test_flapping_pod_resets_stability(self):
        watcher = self.watcher()
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        for restarts in range(1, 13):
            self.advance(watcher, 10, [pod("p", "rs-new", restarts, True)], RECOVERED)
        self.assertNotIn("verification_passed", self.events())

    def test_failed_verification_retries_with_history_then_escalates(self):
        seen = []

        def propose(incident):
            seen.append(incident["previous_attempts"])
            return {"type": "rollout_undo", "namespace": "otel-demo", "target": "cart", "params": {}}

        watcher = self.watcher(propose)
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        self.advance(watcher, 125, FAILING, BROKEN)
        self.advance(watcher, 125, FAILING, BROKEN)
        self.assertEqual(self.actions(), ["rollout_undo", "rollout_undo", "escalate"])
        self.assertEqual(seen[0], [])
        self.assertEqual(seen[1][0]["action"], "rollout_undo")
        self.assertEqual(self.events().count("verification_failed"), 2)

    def test_rules_do_not_repeat_a_failed_rollback(self):
        watcher = self.watcher()
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        self.advance(watcher, 125, FAILING, BROKEN)
        self.assertEqual(self.actions(), ["rollout_undo", "escalate"])

    def test_denied_action_escalates_with_reason(self):
        self.responses = [{"status": "denied", "reasons": ["target no permitido"]}]
        self.watcher().tick([pod("p", "rs-pay", 2, False)], REPLICA_SETS, [], "otel-demo")
        self.assertEqual(self.sent[1]["action"]["type"], "escalate")
        self.assertIn("target no permitido", self.sent[1]["action"]["params"]["summary"])

    def test_flapping_failure_escalates_only_once(self):
        self.responses = [{"status": "denied", "reasons": ["x"]}]
        watcher = self.watcher()
        failing = [pod("p", "rs-pay", 2, False)]
        healthy = [pod("p", "rs-pay", 2, True)]
        for _ in range(10):
            self.advance(watcher, 20, healthy, [])
            self.advance(watcher, 5, failing, [])
        self.assertEqual(self.actions().count("escalate"), 1)
        self.assertNotIn("incident_closed", self.events())

    def test_escalated_incident_closes_when_quiet(self):
        self.responses = [{"status": "denied", "reasons": ["x"]}]
        watcher = self.watcher()
        watcher.tick([pod("p", "rs-pay", 2, False)], REPLICA_SETS, [], "otel-demo")
        self.advance(watcher, module.DETECTION["clear_seconds"], [], [])
        self.assertEqual(self.events()[-1], "incident_closed")
        self.assertEqual(watcher.active, {})

    def test_recurring_failure_after_closing_is_a_new_incident(self):
        watcher = self.watcher()
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        self.advance(watcher, 40, RECOVERED_PODS, RECOVERED)
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(self.sent[0]["incident_id"], self.sent[1]["incident_id"])

    def test_proposer_escalation_is_not_repeated(self):
        escalate = {"type": "escalate", "params": {"summary": "duda"}}
        watcher = self.watcher(lambda incident: escalate)
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        self.assertEqual(self.actions(), ["escalate"])

    def test_unreachable_executor_retries_without_duplicate_detection(self):
        self.responses = [{"status": "unreachable"}]
        watcher = self.watcher()
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.events(), ["incident_detected"])

    def test_independent_incidents_are_handled_separately(self):
        pods = [pod("a", "rs-new", 2, False), pod("b", "rs-pay", 2, False)]
        self.watcher().tick(pods, REPLICA_SETS, BROKEN, "otel-demo")
        self.assertEqual({p["action"]["target"] for p in self.sent}, {"cart", "payment"})


class RulesTest(unittest.TestCase):
    INCIDENT = {"namespace": "otel-demo", "deployment": "cart"}

    def test_first_attempt_is_rollback(self):
        self.assertEqual(rules.propose({**self.INCIDENT, "previous_attempts": []})["type"], "rollout_undo")

    def test_previous_attempt_escalates(self):
        attempt = [{"action": "rollout_undo", "target": "cart"}]
        self.assertEqual(rules.propose({**self.INCIDENT, "previous_attempts": attempt})["type"], "escalate")



class NamespacesTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.events_file = os.path.join(tmp.name, "events.jsonl")

    def send(self, payload):
        self.sent.append(payload)
        return {"status": "executed"}

    def test_same_deployment_name_in_two_namespaces_are_two_incidents(self):
        watcher = Watcher(send=self.send, events_file=self.events_file, clock=lambda: 0)
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "tienda")
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        self.assertEqual({p["action"]["namespace"] for p in self.sent}, {"tienda", "otel-demo"})
        self.assertEqual(set(watcher.active), {("tienda", "cart"), ("otel-demo", "cart")})

    def test_other_namespace_reading_does_not_touch_open_incident(self):
        watcher = Watcher(send=self.send, events_file=self.events_file, clock=lambda: 0)
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "tienda")
        watcher.tick([], [], [], "otel-demo")
        self.assertIn(("tienda", "cart"), watcher.active)

    def test_detection_settings_come_from_config(self):
        watcher = Watcher(send=self.send, events_file=self.events_file, detection={"min_restarts": 5})
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "tienda")
        self.assertEqual(self.sent, [])

    def test_events_file_is_optional(self):
        watcher = Watcher(send=self.send, events_file="", clock=lambda: 0)
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "tienda")
        self.assertEqual(len(self.sent), 1)


class UnreachableExecutorTest(unittest.TestCase):
    def test_resends_same_action_without_asking_the_model_again(self):
        proposals, sent = [], []
        replies = [{"status": "unreachable"}, {"status": "unreachable"}, {"status": "executed"}]

        def propose(incident):
            proposals.append(incident)
            return {"type": "rollout_undo", "namespace": "otel-demo", "target": "cart", "params": {}}

        def send(payload):
            sent.append(payload)
            return replies.pop(0)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        watcher = Watcher(propose=propose, send=send, events_file=os.path.join(tmp.name, "e.jsonl"), clock=lambda: 0)
        for _ in range(3):
            watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(len(sent), 3)
        self.assertEqual(watcher.active[("otel-demo", "cart")]["phase"], "verifying")


class ProposeModeTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.now = 0
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.events_file = os.path.join(tmp.name, "events.jsonl")

    def send(self, payload):
        self.sent.append(payload)
        return {"status": "proposed"}

    def events(self):
        with open(self.events_file, encoding="utf-8") as events:
            return [json.loads(line) for line in events]

    def test_proposal_is_sent_once_and_never_verified(self):
        watcher = Watcher(send=self.send, events_file=self.events_file, clock=lambda: self.now)
        for _ in range(60):
            self.now += 5
            watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        self.assertEqual(len(self.sent), 1)
        kinds = [e["event"] for e in self.events()]
        self.assertNotIn("verification_failed", kinds)
        self.assertNotIn("incident_escalated", kinds)

    def test_closes_when_failure_disappears(self):
        watcher = Watcher(send=self.send, events_file=self.events_file, clock=lambda: self.now)
        watcher.tick(FAILING, REPLICA_SETS, BROKEN, "otel-demo")
        self.now += module.DETECTION["clear_seconds"]
        watcher.tick([], REPLICA_SETS, [], "otel-demo")
        closed = [e for e in self.events() if e["event"] == "incident_closed"]
        self.assertEqual(closed[0]["summary"], "propuesta registrada; sin fallos desde entonces")
        self.assertEqual(watcher.active, {})


class LoadConfigTest(unittest.TestCase):
    def write(self, config):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(os.unlink, tmp.name)
        json.dump(config, tmp)
        tmp.close()
        return tmp.name

    def test_lab_config_is_valid(self):
        namespaces, detection = load_config(os.path.join(os.path.dirname(__file__), "..", "config", "lab.json"))
        self.assertEqual(namespaces, ["otel-demo"])
        self.assertEqual(detection["min_restarts"], 2)

    def test_defaults_when_detection_missing(self):
        _, detection = load_config(self.write({"namespaces": {"a": {"targets": ["x"]}}}))
        self.assertEqual(detection, module.DETECTION)

    def test_invalid_configs_fail(self):
        for config in (
            {},
            {"namespaces": {}},
            {"namespaces": ["a"]},
            {"namespaces": {"a": {}}, "detection": {"min_restarts": 0}},
            {"namespaces": {"a": {}}, "detection": {"min_restarts": "2"}},
            {"namespaces": {"a": {}}, "detection": {"unknown": 1}},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                load_config(self.write(config))


if __name__ == "__main__":
    unittest.main()
