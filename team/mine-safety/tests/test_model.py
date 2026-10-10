"""Contract tests for the underground mine digital twins."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
TWINS_PATH = ROOT / "twins.json"


def load_setup_module():
    spec = importlib.util.spec_from_file_location("mine_safety_setup_twins", ROOT / "setup_twins.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


setup_twins = load_setup_module()


def load_model_module():
    spec = importlib.util.spec_from_file_location("mine_safety_model", ROOT / "model.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


model = load_model_module()


class TwinContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.twins = json.loads(TWINS_PATH.read_text(encoding="utf-8"))

    def objects_of_kind(self, kind):
        return {
            thing_id: body
            for thing_id, body in self.twins.items()
            if body["attributes"]["kind"] == kind
        }

    def test_expected_inventory(self):
        self.assertEqual(26, len(self.twins))
        expected = {"mine": 1, "zone": 5, "fan": 3, "sensor": 5, "worker": 10, "equipment": 2}
        self.assertEqual(expected, {kind: len(self.objects_of_kind(kind)) for kind in expected})

    def test_zone_ids_and_topology(self):
        zones = self.objects_of_kind("zone")
        zone_names = {body["attributes"]["zone"] for body in zones.values()}
        self.assertEqual(
            {"shaft", "main_drift", "section_a", "section_b", "refuge_chamber"},
            zone_names,
        )
        main_drift = self.twins["org.openegiz.safety:zone-main-drift"]["attributes"]
        self.assertEqual(
            {"shaft", "section_a", "section_b", "refuge_chamber"},
            set(main_drift["connected_to"]),
        )
        for body in zones.values():
            self.assertIn("connected_to", body["attributes"])
            self.assertIn("airflow_to", body["attributes"])

    def test_children_have_parent_zone_and_operational_state(self):
        valid_zones = {"shaft", "main_drift", "section_a", "section_b", "refuge_chamber"}
        for thing_id, body in self.twins.items():
            attributes = body["attributes"]
            features = body["features"]
            self.assertTrue(thing_id.startswith("org.openegiz.safety:"))
            self.assertIn("name", attributes)
            self.assertIn("state", features)
            if attributes["kind"] == "mine":
                continue
            self.assertIn("_parents", attributes)
            self.assertIn(attributes["zone"], valid_zones)
            self.assertEqual(attributes["zone"], features["current_zone"]["properties"]["value"])

    def test_sensor_contract_has_every_metric(self):
        expected = {"state", "current_zone", "methane_pct", "co_ppm", "smoke_index", "temperature_c", "airflow_mps"}
        for body in self.objects_of_kind("sensor").values():
            self.assertEqual(expected, set(body["features"]))

    def test_workers_are_anonymous_demo_entities(self):
        for thing_id, body in self.objects_of_kind("worker").items():
            self.assertRegex(thing_id, r":worker-\d{2}$")
            self.assertRegex(body["attributes"]["name"], r"^Работник \d{2}$")


class SetupTwinsTests(unittest.TestCase):
    def test_put_request_is_idempotent_and_utf8(self):
        request = setup_twins.make_put_request(
            "http://ditto-nginx:80/",
            "Basic token",
            "org.openegiz.safety:zone-shaft",
            {"attributes": {"name": "Ствол"}},
        )
        self.assertEqual("PUT", request.get_method())
        self.assertEqual(
            "http://ditto-nginx:80/api/2/things/org.openegiz.safety:zone-shaft",
            request.full_url,
        )
        self.assertEqual("Basic token", request.get_header("Authorization"))
        self.assertEqual("Ствол", json.loads(request.data.decode("utf-8"))["attributes"]["name"])

    def test_put_retries_transient_http_error(self):
        request = setup_twins.make_put_request("http://ditto", "Basic token", "ns:item", {})
        response = mock.MagicMock()
        response.__enter__.return_value.status = 204
        temporary = urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
        with mock.patch.object(setup_twins.urllib.request, "urlopen", side_effect=[temporary, response]) as urlopen:
            with mock.patch.object(setup_twins.time, "sleep"):
                status = setup_twins.put_twin(request, attempts=2, retry_delay=0)
        self.assertEqual(204, status)
        self.assertEqual(2, urlopen.call_count)

    def test_put_does_not_retry_permanent_http_error(self):
        request = setup_twins.make_put_request("http://ditto", "Basic token", "ns:item", {})
        permanent = urllib.error.HTTPError(request.full_url, 400, "bad request", {}, None)
        with mock.patch.object(setup_twins.urllib.request, "urlopen", side_effect=permanent) as urlopen:
            with self.assertRaises(urllib.error.HTTPError):
                setup_twins.put_twin(request, attempts=3, retry_delay=0)
        self.assertEqual(1, urlopen.call_count)


class MineSafetyModelTests(unittest.TestCase):
    def assert_normal_readings(self, snapshot):
        for readings in snapshot["sensors"].values():
            for metric, (lower, upper) in model.NORMAL_RANGES.items():
                self.assertGreaterEqual(readings[metric], lower, metric)
                self.assertLessEqual(readings[metric], upper, metric)

    def test_normal_mode_has_safe_ranges_and_two_second_step(self):
        mine = model.MineSafetyModel(seed=42)
        initial = mine.snapshot()
        stepped = mine.step()
        self.assert_normal_readings(initial)
        self.assert_normal_readings(stepped)
        self.assertEqual("normal", initial["risk"]["level"])
        self.assertEqual(2, stepped["simulated_seconds"])
        self.assertEqual(2, stepped["publish_interval_seconds"])

    def test_same_seed_and_steps_are_reproducible(self):
        first = model.MineSafetyModel(seed=17)
        second = model.MineSafetyModel(seed=17)
        first_snapshot = first.step()
        second_snapshot = second.step()
        self.assertEqual(first_snapshot["sensors"], second_snapshot["sensors"])
        self.assertEqual(first_snapshot["risk"], second_snapshot["risk"])
        self.assertEqual(first_snapshot["topology"], second_snapshot["topology"])

    def test_risk_function_is_pure_and_explainable(self):
        mine = model.MineSafetyModel(seed=7)
        mine.start_scenario("methane_release", "section_a")
        snapshot = mine.snapshot()
        first = model.evaluate_risk(snapshot["sensors"], model.WORKER_ZONES)
        second = model.evaluate_risk(snapshot["sensors"], model.WORKER_ZONES)
        self.assertEqual(first, second)
        self.assertEqual("critical", first["by_zone"]["section_a"]["level"])
        self.assertTrue(first["by_zone"]["section_a"]["causes"])
        self.assertTrue(first["recommendations"])
        self.assertEqual(["worker-04", "worker-05", "worker-06"], first["affected_people"])

    def test_main_fan_failure_reduces_airflow_and_stops_fan(self):
        mine = model.MineSafetyModel()
        mine.start_scenario("main_fan_failure")
        snapshot = mine.snapshot()
        self.assertEqual("stopped", snapshot["fans"]["fan-main"]["state"])
        self.assertEqual(0, snapshot["fans"]["fan-main"]["speed_percent"])
        self.assertEqual("critical", snapshot["risk"]["level"])
        self.assertIn("shaft", snapshot["risk"]["affected_zones"])

    def test_methane_release_is_local_to_selected_section(self):
        mine = model.MineSafetyModel()
        mine.start_scenario("methane_release", "section_b")
        snapshot = mine.snapshot()
        self.assertEqual(2.8, snapshot["sensors"]["section_b"]["methane_pct"])
        self.assertEqual("critical", snapshot["risk"]["by_zone"]["section_b"]["level"])
        self.assertEqual("normal", snapshot["risk"]["by_zone"]["section_a"]["level"])

    def test_fire_propagates_downstream_from_main_drift(self):
        mine = model.MineSafetyModel()
        mine.start_scenario("fire", "main_drift")
        snapshot = mine.snapshot()
        self.assertEqual("critical", snapshot["risk"]["by_zone"]["main_drift"]["level"])
        for zone in ("section_a", "section_b", "refuge_chamber"):
            self.assertGreaterEqual(snapshot["sensors"][zone]["co_ppm"], 25.0)
            self.assertIn(zone, snapshot["risk"]["affected_zones"])
        self.assertEqual("normal", snapshot["risk"]["by_zone"]["shaft"]["level"])

    def test_blocked_working_disables_edge_and_creates_low_flow_risk(self):
        mine = model.MineSafetyModel()
        mine.start_scenario("blocked_working", "section_a")
        snapshot = mine.snapshot()
        edge = next(
            item
            for item in snapshot["topology"]["connections"]
            if item["from"] == "main_drift" and item["to"] == "section_a"
        )
        self.assertFalse(edge["active"])
        self.assertEqual(0.1, snapshot["sensors"]["section_a"]["airflow_mps"])
        self.assertEqual("critical", snapshot["risk"]["by_zone"]["section_a"]["level"])

    def test_reset_restores_initial_normal_state(self):
        mine = model.MineSafetyModel(seed=99)
        initial = mine.snapshot()
        mine.start_scenario("fire", "main_drift")
        mine.step()
        mine.reset()
        reset = mine.snapshot()
        self.assertEqual("none", reset["scenario"]["name"])
        self.assertEqual("normal", reset["risk"]["level"])
        self.assertEqual(initial["sensors"], reset["sensors"])
        self.assertEqual(initial["topology"], reset["topology"])

    def test_invalid_scenario_combinations_are_rejected(self):
        mine = model.MineSafetyModel()
        with self.assertRaises(ValueError):
            mine.start_scenario("unknown")
        with self.assertRaises(ValueError):
            mine.start_scenario("methane_release", "main_drift")
        with self.assertRaises(ValueError):
            mine.start_scenario("blocked_working", "refuge_chamber")

    def test_thresholds_have_units_and_declared_direction(self):
        for rule in model.THRESHOLDS.values():
            self.assertIn("unit", rule)
            self.assertIn(rule["direction"], {"high", "low"})
            if rule["direction"] == "high":
                self.assertLess(rule["warning"], rule["critical"])
            else:
                self.assertGreater(rule["warning"], rule["critical"])

    def test_normal_ventilation_forecast_stays_stable(self):
        advisor = model.MineSafetyModel(seed=42).snapshot()["ventilation_advisor"]
        self.assertEqual("stable", advisor["status"])
        self.assertEqual(10, advisor["horizon_minutes"])
        self.assertEqual([0, 2, 4, 6, 8, 10], [item["minute"] for item in advisor["forecast"]["with_advice"]])
        self.assertTrue(all(item["risk_level"] == "normal" for item in advisor["forecast"]["without_action"]))
        self.assertEqual({"fan-main": 100, "fan-aux-a": 75, "fan-aux-b": 75}, advisor["fan_targets_percent"])

    def test_methane_advice_projects_lower_risk_without_mutating_model(self):
        mine = model.MineSafetyModel(seed=42)
        mine.start_scenario("methane_release", "section_a")
        first = mine.snapshot()
        advisor = first["ventilation_advisor"]
        baseline = advisor["forecast"]["without_action"][-1]
        advised = advisor["forecast"]["with_advice"][-1]
        self.assertEqual("action_required", advisor["status"])
        self.assertEqual(100, advisor["fan_targets_percent"]["fan-aux-a"])
        self.assertEqual("critical", baseline["risk_level"])
        self.assertEqual("normal", advised["risk_level"])
        self.assertLess(advised["max_methane_pct"], baseline["max_methane_pct"])
        self.assertEqual(first["sensors"], mine.snapshot()["sensors"])

    def test_fire_advice_contains_downstream_exposure_but_keeps_source_critical(self):
        mine = model.MineSafetyModel(seed=42)
        mine.start_scenario("fire", "main_drift")
        advisor = mine.snapshot()["ventilation_advisor"]
        baseline = advisor["forecast"]["without_action"][-1]
        advised = advisor["forecast"]["with_advice"][-1]
        self.assertEqual("critical", advised["risk_level"])
        self.assertLess(len(advised["affected_zones"]), len(baseline["affected_zones"]))
        self.assertIn("подтвержден", " ".join(advisor["actions"]))

    def test_forecast_is_reproducible(self):
        first = model.MineSafetyModel(seed=17)
        second = model.MineSafetyModel(seed=17)
        first.start_scenario("blocked_working", "section_b")
        second.start_scenario("blocked_working", "section_b")
        self.assertEqual(
            first.snapshot()["ventilation_advisor"],
            second.snapshot()["ventilation_advisor"],
        )

    def test_normal_fan_energy_uses_cubic_law_and_reports_savings(self):
        energy = model.MineSafetyModel(seed=42).snapshot()["fan_energy"]
        self.assertEqual(270.0, energy["full_speed_reference"]["power_kw"])
        self.assertAlmostEqual(217.97, energy["current"]["power_kw"], places=2)
        self.assertEqual(energy["current"], energy["recommended"])
        self.assertAlmostEqual(19.3, energy["comparison"]["savings_vs_full_percent"], places=1)
        self.assertEqual(30.0, energy["tariff_kzt_per_kwh"])
        self.assertFalse(energy["safety_priority"])

    def test_incident_energy_prioritizes_safe_advised_mode(self):
        mine = model.MineSafetyModel(seed=42)
        mine.start_scenario("main_fan_failure")
        energy = mine.snapshot()["fan_energy"]
        self.assertEqual(0.0, energy["fans"]["fan-main"]["current_power_kw"])
        self.assertEqual(180.0, energy["fans"]["fan-main"]["recommended_power_kw"])
        self.assertGreater(energy["recommended"]["power_kw"], energy["current"]["power_kw"])
        self.assertTrue(energy["safety_priority"])
        self.assertIn("безопасность", energy["summary"])

    def test_energy_analysis_is_deterministic_and_does_not_mutate_fans(self):
        mine = model.MineSafetyModel(seed=17)
        mine.start_scenario("fire", "section_a")
        first = mine.snapshot()
        second = mine.snapshot()
        self.assertEqual(first["fan_energy"], second["fan_energy"])
        self.assertEqual(first["fans"], second["fans"])

    def test_normal_evacuation_plan_is_on_standby(self):
        plan = model.MineSafetyModel(seed=42).snapshot()["evacuation_plan"]
        self.assertEqual("standby", plan["status"])
        self.assertEqual(0, plan["total_people"])
        self.assertEqual([], plan["routes"])
        self.assertTrue(plan["manual_confirmation_required"])

    def test_methane_routes_affected_workers_to_surface(self):
        mine = model.MineSafetyModel(seed=42)
        mine.start_scenario("methane_release", "section_a")
        plan = mine.snapshot()["evacuation_plan"]
        self.assertEqual("ready", plan["status"])
        self.assertEqual(3, plan["routed_people"])
        self.assertEqual(0, plan["unrouted_people"])
        self.assertEqual(["section_a", "main_drift", "shaft"], plan["routes"][0]["path"])
        self.assertEqual("safe", plan["routes"][0]["status"])

    def test_blocked_working_reports_workers_without_route(self):
        mine = model.MineSafetyModel(seed=42)
        mine.start_scenario("blocked_working", "section_b")
        plan = mine.snapshot()["evacuation_plan"]
        self.assertEqual("blocked", plan["status"])
        self.assertEqual(0, plan["routed_people"])
        self.assertEqual(3, plan["unrouted_people"])
        self.assertEqual("blocked", plan["routes"][0]["status"])
        self.assertIsNone(plan["routes"][0]["destination"])

    def test_fire_from_main_drift_prefers_surface_and_marks_caution(self):
        mine = model.MineSafetyModel(seed=42)
        mine.start_scenario("fire", "main_drift")
        plan = mine.snapshot()["evacuation_plan"]
        self.assertEqual("caution", plan["status"])
        self.assertEqual(9, plan["routed_people"])
        self.assertTrue(all(route["destination"] == "shaft" for route in plan["routes"]))
        self.assertTrue(any(route["status"] == "caution" for route in plan["routes"]))

    def test_evacuation_plan_is_reproducible(self):
        first = model.MineSafetyModel(seed=17)
        second = model.MineSafetyModel(seed=17)
        first.start_scenario("main_fan_failure")
        second.start_scenario("main_fan_failure")
        self.assertEqual(
            first.snapshot()["evacuation_plan"],
            second.snapshot()["evacuation_plan"],
        )


if __name__ == "__main__":
    unittest.main()
