"""HTTP, telemetry-loop and Ditto-message tests for the mine-safety service."""

from __future__ import annotations

from contextlib import contextmanager
import importlib
import json
from pathlib import Path
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

app = importlib.import_module("app")
model = importlib.import_module("model")


class FakePublishResult:
    rc = 0


class FakePublisher:
    def __init__(self):
        self.calls = []

    def publish(self, topic, payload, qos):
        self.calls.append((topic, json.loads(payload), qos))
        return FakePublishResult()


@contextmanager
def running_api(interval=2.0, publisher=None):
    application = app.SafetyApplication(model.MineSafetyModel(seed=42), publisher, interval=interval)
    server = app.SafetyHTTPServer(("127.0.0.1", 0), application)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    application.start()
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", application
    finally:
        server.shutdown()
        server.server_close()
        application.stop()
        thread.join(timeout=2)


def request_json(url, method="GET", body=None):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.status, json.load(response)


class SnapshotMappingTests(unittest.TestCase):
    def test_snapshot_maps_all_declared_twins_to_ditto_commands(self):
        snapshot = app.enrich_snapshot(model.MineSafetyModel(seed=42).snapshot())
        features = app.snapshot_to_twin_features(snapshot)
        declared = json.loads((ROOT / "twins.json").read_text(encoding="utf-8"))
        declared_names = {thing_id.split(":", 1)[1] for thing_id in declared}
        self.assertEqual(26, len(features))
        self.assertEqual(declared_names, set(features))

        publisher = FakePublisher()
        count = app.publish_snapshot(publisher, snapshot)
        self.assertEqual(26, count)
        self.assertEqual(26, len(publisher.calls))
        for topic, command, qos in publisher.calls:
            name = topic.removeprefix("telemetry/")
            self.assertEqual(0, qos)
            self.assertEqual(f"org.openegiz.safety/{name}/things/twin/commands/modify", command["topic"])
            self.assertEqual("/features", command["path"])
            self.assertTrue(command["value"])

        self.assertEqual(
            {
                "mine-01",
                "sensor-shaft",
                "sensor-main-drift",
                "sensor-section-a",
                "sensor-section-b",
                "sensor-refuge-chamber",
            },
            set(app.STREAMING_TWIN_NAMES),
        )
        streaming_publisher = FakePublisher()
        streaming_count = app.publish_snapshot(
            streaming_publisher, snapshot, app.STREAMING_TWIN_NAMES
        )
        self.assertEqual(6, streaming_count)
        self.assertEqual(set(app.STREAMING_TWIN_NAMES), {
            topic.removeprefix("telemetry/")
            for topic, _payload, _qos in streaming_publisher.calls
        })
        paced_publisher = FakePublisher()
        paced_application = app.SafetyApplication(
            model.MineSafetyModel(seed=42), paced_publisher
        )
        paced_application.start_scenario("methane_release", "section_a")
        paced_application.reset()
        self.assertEqual(12, len(paced_publisher.calls))
        for topic, event, qos in paced_publisher.calls:
            self.assertTrue(topic.startswith("opentwins/twin/events/org.openegiz.safety/"))
            self.assertEqual(1, qos)
            self.assertEqual("/features", event["path"])
            self.assertTrue(event["extra"]["thingId"].startswith("org.openegiz.safety:"))
            if topic.endswith("/mine-01"):
                self.assertIn("overall_risk", event["value"])
                self.assertNotIn("recommendations", event["value"])
                self.assertNotIn("fan_targets_percent", event["value"])
                self.assertNotIn("evacuation_routes", event["value"])

    def test_enriched_state_has_required_public_sections(self):
        state = app.enrich_snapshot(model.MineSafetyModel().snapshot())
        required = {
            "timestamp", "topology", "airflow", "sensors", "workers", "equipment",
            "fans", "incident", "risk", "scenario", "assumptions", "ventilation_advisor",
            "fan_energy",
            "evacuation_plan",
        }
        self.assertTrue(required.issubset(state))
        self.assertFalse(state["incident"]["active"])

class HTTPAPITests(unittest.TestCase):
    def test_ui_and_local_assets_are_served_without_external_dependencies(self):
        with running_api() as (base_url, _application):
            with urllib.request.urlopen(f"{base_url}/", timeout=3) as response:
                html = response.read().decode("utf-8")
                self.assertEqual(200, response.status)
                self.assertIn("text/html", response.headers["Content-Type"])
            with urllib.request.urlopen(f"{base_url}/static/app.js", timeout=3) as response:
                javascript = response.read().decode("utf-8")
                self.assertEqual(200, response.status)
            with urllib.request.urlopen(f"{base_url}/static/styles.css", timeout=3) as response:
                styles = response.read().decode("utf-8")
                self.assertEqual(200, response.status)

        self.assertEqual(5, html.count('data-zone="'))
        self.assertEqual(4, html.count('data-scenario="'))
        self.assertIn('id="reset-scenario"', html)
        self.assertIn("Безопасность подземной шахты", html)
        self.assertIn("Рекомендация диспетчеру", html)
        self.assertIn("Советник по вентиляции", html)
        self.assertIn("Энергетика вентиляторов", html)
        self.assertIn("Маршруты эвакуации", html)
        self.assertIn('id="forecast-chart"', html)
        self.assertIn('id="energy-fans"', html)
        self.assertIn('id="evacuation-layer"', html)
        self.assertIn("Работники", html)
        self.assertIn("Вентиляторы", html)
        self.assertIn("/api/state", javascript)
        self.assertIn("/api/scenarios/", javascript)
        self.assertIn("/api/reset", javascript)
        self.assertIn("POLL_INTERVAL_MS = 2000", javascript)
        self.assertIn("renderAdvisor", javascript)
        self.assertIn("renderEnergy", javascript)
        self.assertIn("renderEvacuation", javascript)
        self.assertIn("prefers-reduced-motion", styles)
        for content in (html, javascript, styles):
            self.assertNotIn("https://", content)
            self.assertNotIn("src=\"//", content)

    def test_static_path_traversal_is_rejected(self):
        with running_api() as (base_url, _application):
            request = urllib.request.Request(f"{base_url}/static/%2e%2e/model.py")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(404, caught.exception.code)
            caught.exception.close()

    def test_health_and_state(self):
        with running_api() as (base_url, _application):
            status, health = request_json(f"{base_url}/health")
            self.assertEqual(200, status)
            self.assertEqual("ok", health["status"])
            self.assertEqual(2.0, health["publish_interval_seconds"])
            self.assertEqual(2.0, health["mqtt_publish_interval_seconds"])
            self.assertEqual("opentwins", health["mqtt_history_route"])

            status, state = request_json(f"{base_url}/api/state")
            self.assertEqual(200, status)
            self.assertEqual("none", state["scenario"]["name"])
            self.assertEqual("normal", state["risk"]["level"])

    def test_every_scenario_responds_with_full_state_under_five_seconds(self):
        cases = (
            ("main_fan_failure", {}),
            ("methane_release", {"zone": "section_a"}),
            ("fire", {"zone": "main_drift"}),
            ("blocked_working", {"zone": "section_b"}),
        )
        with running_api() as (base_url, _application):
            for scenario, body in cases:
                started = time.monotonic()
                status, state = request_json(f"{base_url}/api/scenarios/{scenario}", "POST", body)
                elapsed = time.monotonic() - started
                self.assertEqual(200, status)
                self.assertLess(elapsed, 5.0)
                self.assertEqual(scenario, state["scenario"]["name"])
                self.assertTrue(state["incident"]["active"])
                self.assertNotEqual("normal", state["risk"]["level"])
                self.assertTrue(state["risk"]["affected_zones"])
                self.assertTrue(state["risk"]["affected_people"])
                self.assertTrue(state["risk"]["recommendations"])
            advisor = state["ventilation_advisor"]
            self.assertEqual("action_required", advisor["status"])
            self.assertEqual(10, advisor["horizon_minutes"])
            self.assertEqual(6, len(advisor["forecast"]["without_action"]))
            self.assertEqual(6, len(advisor["forecast"]["with_advice"]))
            energy = state["fan_energy"]
            self.assertEqual(10, energy["horizon_minutes"])
            self.assertEqual(3, len(energy["fans"]))
            self.assertGreater(energy["recommended"]["power_kw"], 0)
            evacuation = state["evacuation_plan"]
            self.assertTrue(evacuation["manual_confirmation_required"])
            self.assertEqual(len(state["risk"]["affected_people"]), evacuation["total_people"])

    def test_new_scenario_atomically_replaces_previous_and_reset_is_normal(self):
        with running_api() as (base_url, _application):
            request_json(f"{base_url}/api/scenarios/fire", "POST", {"zone": "main_drift"})
            _, methane = request_json(
                f"{base_url}/api/scenarios/methane_release", "POST", {"zone": "section_b"}
            )
            self.assertEqual("methane_release", methane["scenario"]["name"])
            self.assertEqual("section_b", methane["scenario"]["zone"])
            self.assertEqual("normal", methane["risk"]["by_zone"]["main_drift"]["level"])

            status, reset = request_json(f"{base_url}/api/reset", "POST", {})
            self.assertEqual(200, status)
            self.assertEqual("none", reset["scenario"]["name"])
            self.assertFalse(reset["incident"]["active"])
            self.assertEqual("normal", reset["risk"]["level"])

    def test_invalid_scenario_zone_and_json_return_4xx(self):
        with running_api() as (base_url, _application):
            bad_requests = (
                (f"{base_url}/api/scenarios/unknown", b"{}"),
                (f"{base_url}/api/scenarios/methane_release", b'{"zone":"main_drift"}'),
                (f"{base_url}/api/scenarios/fire", b"not-json"),
            )
            for url, payload in bad_requests:
                request = urllib.request.Request(
                    url, data=payload, method="POST", headers={"Content-Type": "application/json"}
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(400, caught.exception.code)
                caught.exception.close()

    def test_background_loop_advances_and_publishes_on_interval(self):
        publisher = FakePublisher()
        with running_api(interval=0.05, publisher=publisher) as (base_url, _application):
            _, first = request_json(f"{base_url}/api/state")
            deadline = time.monotonic() + 1.0
            latest = first
            while latest["tick"] < first["tick"] + 2 and time.monotonic() < deadline:
                time.sleep(0.02)
                _, latest = request_json(f"{base_url}/api/state")
            self.assertGreaterEqual(latest["tick"], first["tick"] + 2)
            # Startup and each periodic cycle emit six history events.
            self.assertGreaterEqual(len(publisher.calls), 6 + (2 * 6))
            periodic_names = {
                topic.rsplit("/", 1)[-1]
                for topic, _payload, _qos in publisher.calls[2:]
            }
            self.assertTrue(periodic_names.issubset(set(app.STREAMING_TWIN_NAMES)))


if __name__ == "__main__":
    unittest.main()
