import json
import os
import tempfile
import unittest

import rules
import watcher as module
from watcher import Watcher, deployment_health, find_incidents


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
        incident = find_incidents([pod("p", "rs-new", 2, False)], REPLICA_SETS)["cart"]
        self.assertEqual(incident["replica_set"], "rs-new")
        self.assertEqual((incident["restarts"], incident["exit_code"]), (2, 2))

    def test_single_restart_is_ignored(self):
        self.assertEqual(find_incidents([pod("p", "rs-new", 1, False)], REPLICA_SETS), {})

    def test_healthy_pod_with_old_restarts_is_ignored(self):
        self.assertEqual(find_incidents([pod("p", "rs-old", 9, True)], REPLICA_SETS), {})

    def test_replicas_of_same_deployment_are_one_incident(self):
        pods = [pod("a", "rs-new", 2, False), pod("b", "rs-new", 3, False)]
        self.assertEqual(len(find_incidents(pods, REPLICA_SETS)), 1)

    def test_unowned_pod_is_ignored(self):
        self.assertEqual(find_incidents([pod("p", "otro", 5, False)], REPLICA_SETS), {})

    def test_pod_without_status_is_ignored(self):
        self.assertEqual(find_incidents([{"metadata": {"name": "p"}}], REPLICA_SETS), {})


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
            watcher.tick(pods, REPLICA_SETS, deployments)

    def events(self):
        if not os.path.exists(self.events_file):
            return []
        with open(self.events_file, encoding="utf-8") as events:
            return [json.loads(line)["event"] for line in events]

    def actions(self):
        return [payload["action"]["type"] for payload in self.sent]

    def test_proposes_once_while_verifying(self):
        watcher = self.watcher()
        watcher.tick(FAILING, REPLICA_SETS, BROKEN)
        watcher.tick(FAILING, REPLICA_SETS, BROKEN)
        self.assertEqual(self.actions(), ["rollout_undo"])

    def test_incident_id_links_detection_and_action(self):
        self.watcher().tick(FAILING, REPLICA_SETS, BROKEN)
        with open(self.events_file, encoding="utf-8") as events:
            detected = json.loads(events.readline())
        self.assertEqual(detected["incident_id"], self.sent[0]["incident_id"])

    def test_closes_only_after_stable_period(self):
        watcher = self.watcher()
        watcher.tick(FAILING, REPLICA_SETS, BROKEN)
        self.advance(watcher, 25, RECOVERED_PODS, RECOVERED)
        self.assertEqual(self.events(), ["incident_detected"])
        self.advance(watcher, 15, RECOVERED_PODS, RECOVERED)
        self.assertEqual(self.events(), ["incident_detected", "verification_passed", "incident_closed"])
        self.assertEqual(watcher.active, {})

    def test_flapping_pod_resets_stability(self):
        watcher = self.watcher()
        watcher.tick(FAILING, REPLICA_SETS, BROKEN)
        for restarts in range(1, 13):
            self.advance(watcher, 10, [pod("p", "rs-new", restarts, True)], RECOVERED)
        self.assertNotIn("verification_passed", self.events())

    def test_failed_verification_retries_with_history_then_escalates(self):
        seen = []

        def propose(incident):
            seen.append(incident["previous_attempts"])
            return {"type": "rollout_undo", "namespace": "otel-demo", "target": "cart", "params": {}}

        watcher = self.watcher(propose)
        watcher.tick(FAILING, REPLICA_SETS, BROKEN)
        self.advance(watcher, 125, FAILING, BROKEN)
        self.advance(watcher, 125, FAILING, BROKEN)
        self.assertEqual(self.actions(), ["rollout_undo", "rollout_undo", "escalate"])
        self.assertEqual(seen[0], [])
        self.assertEqual(seen[1][0]["action"], "rollout_undo")
        self.assertEqual(self.events().count("verification_failed"), 2)

    def test_rules_do_not_repeat_a_failed_rollback(self):
        watcher = self.watcher()
        watcher.tick(FAILING, REPLICA_SETS, BROKEN)
        self.advance(watcher, 125, FAILING, BROKEN)
        self.assertEqual(self.actions(), ["rollout_undo", "escalate"])

    def test_denied_action_escalates_with_reason(self):
        self.responses = [{"status": "denied", "reasons": ["target no permitido"]}]
        self.watcher().tick([pod("p", "rs-pay", 2, False)], REPLICA_SETS, [])
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
        watcher.tick([pod("p", "rs-pay", 2, False)], REPLICA_SETS, [])
        self.advance(watcher, module.CLEAR_SECONDS, [], [])
        self.assertEqual(self.events()[-1], "incident_closed")
        self.assertEqual(watcher.active, {})

    def test_recurring_failure_after_closing_is_a_new_incident(self):
        watcher = self.watcher()
        watcher.tick(FAILING, REPLICA_SETS, BROKEN)
        self.advance(watcher, 40, RECOVERED_PODS, RECOVERED)
        watcher.tick(FAILING, REPLICA_SETS, BROKEN)
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(self.sent[0]["incident_id"], self.sent[1]["incident_id"])

    def test_proposer_escalation_is_not_repeated(self):
        escalate = {"type": "escalate", "params": {"summary": "duda"}}
        watcher = self.watcher(lambda incident: escalate)
        watcher.tick(FAILING, REPLICA_SETS, BROKEN)
        watcher.tick(FAILING, REPLICA_SETS, BROKEN)
        self.assertEqual(self.actions(), ["escalate"])

    def test_unreachable_executor_retries_without_duplicate_detection(self):
        self.responses = [{"status": "unreachable"}]
        watcher = self.watcher()
        watcher.tick(FAILING, REPLICA_SETS, BROKEN)
        watcher.tick(FAILING, REPLICA_SETS, BROKEN)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.events(), ["incident_detected"])

    def test_independent_incidents_are_handled_separately(self):
        pods = [pod("a", "rs-new", 2, False), pod("b", "rs-pay", 2, False)]
        self.watcher().tick(pods, REPLICA_SETS, BROKEN)
        self.assertEqual({p["action"]["target"] for p in self.sent}, {"cart", "payment"})


class RulesTest(unittest.TestCase):
    INCIDENT = {"namespace": "otel-demo", "deployment": "cart"}

    def test_first_attempt_is_rollback(self):
        self.assertEqual(rules.propose({**self.INCIDENT, "previous_attempts": []})["type"], "rollout_undo")

    def test_previous_attempt_escalates(self):
        attempt = [{"action": "rollout_undo", "target": "cart"}]
        self.assertEqual(rules.propose({**self.INCIDENT, "previous_attempts": attempt})["type"], "escalate")


if __name__ == "__main__":
    unittest.main()
