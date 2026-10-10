#!/usr/bin/env python3
"""Public-HTTP smoke test for the underground mine safety submission."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


EXPECTED_COUNTS = {
    "zones": 5,
    "sensors": 5,
    "fans": 3,
    "workers": 10,
    "equipment": 2,
}
SCENARIOS = (
    ("main_fan_failure", None),
    ("methane_release", "section_a"),
    ("fire", "main_drift"),
    ("blocked_working", "section_b"),
)


class SmokeFailure(RuntimeError):
    """A user-facing smoke-test assertion failure."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


class PublicApi:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    def request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        method = "GET"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            method = "POST"
        request = Request(
            urljoin(self.base_url, path.lstrip("/")),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                require(response.status == 200, f"{path}: HTTP {response.status}, expected 200")
                result = json.load(response)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise SmokeFailure(f"{path}: HTTP {exc.code}: {body[:300]}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SmokeFailure(f"{path}: {exc}") from exc
        require(isinstance(result, dict), f"{path}: JSON object expected")
        return result

    def get(self, path: str) -> dict[str, Any]:
        return self.request(path)

    def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request(path, {} if payload is None else payload)


def validate_inventory(state: dict[str, Any]) -> None:
    for key in ("topology", "sensors", "fans", "workers", "equipment", "risk", "scenario", "incident", "ventilation_advisor", "fan_energy", "evacuation_plan"):
        require(key in state, f"state is missing '{key}'")

    topology = state["topology"]
    require(isinstance(topology, dict), "topology must be an object")
    zones = topology.get("zones")
    require(isinstance(zones, list), "topology.zones must be a list")
    require(len(zones) == EXPECTED_COUNTS["zones"], "expected 5 ventilation zones")

    observed_counts = {"zones": len(zones)}
    for key in ("sensors", "fans", "workers", "equipment"):
        value = state[key]
        require(isinstance(value, dict), f"{key} must be an object")
        require(len(value) == EXPECTED_COUNTS[key], f"unexpected {key} count: {len(value)}")
        observed_counts[key] = len(value)

    twin_count = 1 + sum(observed_counts.values())
    require(twin_count == 26, f"expected 26 objects in snapshot, observed {twin_count}")
    require(state.get("publish_interval_seconds") == 2, "publish interval must be 2 seconds")
    advisor = state["ventilation_advisor"]
    require(advisor.get("horizon_minutes") == 10, "advisor horizon must be 10 minutes")
    require(len(advisor.get("forecast", {}).get("without_action", [])) == 6, "advisor baseline forecast must have 6 points")
    require(len(advisor.get("forecast", {}).get("with_advice", [])) == 6, "advisor recommendation forecast must have 6 points")
    energy = state["fan_energy"]
    require(energy.get("horizon_minutes") == 10, "fan energy horizon must be 10 minutes")
    require(len(energy.get("fans", {})) == 3, "fan energy must contain 3 fan estimates")
    require(energy.get("full_speed_reference", {}).get("power_kw") == 270.0, "unexpected full-speed fan power")
    evacuation = state["evacuation_plan"]
    require(evacuation.get("manual_confirmation_required") is True, "evacuation must require manual confirmation")


def validate_normal(state: dict[str, Any]) -> None:
    require(state["scenario"].get("name") == "none", "scenario did not reset")
    require(state["risk"].get("level") == "normal", "risk did not return to normal")
    require(state["incident"].get("active") is False, "incident remained active after reset")
    require(not state["risk"].get("affected_zones"), "affected zones remained after reset")
    require(not state["risk"].get("affected_people"), "affected people remained after reset")
    require(state["ventilation_advisor"].get("status") == "stable", "advisor did not return to stable")
    require(state["fan_energy"].get("safety_priority") is False, "normal energy analysis kept safety-priority mode")
    require(state["evacuation_plan"].get("status") == "standby", "evacuation did not return to standby")


def verify_publish_period(api: PublicApi) -> None:
    initial = api.get("/api/state")
    previous_tick = initial.get("tick")
    require(isinstance(previous_tick, int), "state.tick must be an integer")
    changes: list[float] = []
    deadline = time.monotonic() + 7.0
    while len(changes) < 2 and time.monotonic() < deadline:
        time.sleep(0.1)
        current = api.get("/api/state")
        current_tick = current.get("tick")
        if isinstance(current_tick, int) and current_tick > previous_tick:
            changes.append(time.monotonic())
            previous_tick = current_tick
    require(len(changes) == 2, "telemetry did not advance twice within 7 seconds")
    period = changes[1] - changes[0]
    require(1.0 <= period <= 3.5, f"telemetry period is {period:.2f}s, expected about 2s")
    print(f"PASS telemetry updates every {period:.2f}s (target 2s)")


def verify_scenario(api: PublicApi, name: str, zone: str | None) -> None:
    payload = {} if zone is None else {"zone": zone}
    started_at = time.monotonic()
    state = api.post(f"/api/scenarios/{name}", payload)
    elapsed = time.monotonic() - started_at
    require(elapsed <= 5.0, f"{name}: reflected after {elapsed:.2f}s, limit is 5s")
    require(state["scenario"].get("name") == name, f"{name}: wrong active scenario")
    require(state["risk"].get("level") in {"warning", "critical"}, f"{name}: risk stayed normal")
    require(bool(state["risk"].get("affected_zones")), f"{name}: no affected zones")
    require(bool(state["risk"].get("affected_people")), f"{name}: no affected people")
    require(bool(state["risk"].get("recommendations")), f"{name}: no recommendations")
    incident = state["incident"]
    require(incident.get("active") is True, f"{name}: incident is not active")
    require(incident.get("type") == name, f"{name}: wrong incident type")
    require(bool(incident.get("causes")), f"{name}: no incident causes")
    advisor = state["ventilation_advisor"]
    require(advisor.get("status") == "action_required", f"{name}: advisor did not request action")
    require(bool(advisor.get("actions")), f"{name}: advisor has no actions")
    require(bool(advisor.get("fan_targets_percent")), f"{name}: advisor has no fan targets")
    energy = state["fan_energy"]
    require(energy.get("safety_priority") is True, f"{name}: energy analysis did not prioritize safety")
    require(energy.get("recommended", {}).get("power_kw") is not None, f"{name}: no recommended fan power")
    evacuation = state["evacuation_plan"]
    require(evacuation.get("total_people") == len(state["risk"]["affected_people"]), f"{name}: evacuation people mismatch")
    require(bool(evacuation.get("routes")), f"{name}: no evacuation routes or blocked-route notices")
    require(advisor["forecast"]["without_action"][-1].get("risk_level") in {"warning", "critical"}, f"{name}: baseline forecast lost the incident")
    if zone is not None:
        require(incident.get("source_zone") == zone, f"{name}: wrong source zone")
    print(f"PASS scenario {name} reflected in {elapsed:.2f}s")

    reset_state = api.post("/api/reset")
    validate_normal(reset_state)
    print(f"PASS scenario {name} reset to normal")


def run(base_url: str, timeout: float) -> None:
    api = PublicApi(base_url, timeout)
    health = api.get("/health")
    require(health.get("status") == "ok", f"service health is {health.get('status')!r}")
    require(health.get("mqtt_enabled") is True, "MQTT publishing is not enabled")
    require(health.get("publish_interval_seconds") == 2, "health reports a non-2s interval")
    require(health.get("mqtt_publish_interval_seconds") == 2, "health reports a non-2s MQTT interval")
    require(health.get("mqtt_history_route") == "opentwins", "health reports a wrong MQTT history route")
    print("PASS service is healthy and MQTT publishing is enabled")

    reset_state = api.post("/api/reset")
    validate_inventory(reset_state)
    validate_normal(reset_state)
    print("PASS snapshot contains 26 typed objects and starts in normal state")

    verify_publish_period(api)
    for name, zone in SCENARIOS:
        verify_scenario(api, name, zone)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8091")
    parser.add_argument("--timeout", type=float, default=5.0, help="per-request timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api = PublicApi(args.base_url, args.timeout)
    try:
        run(args.base_url, args.timeout)
    except (SmokeFailure, KeyError, TypeError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            api.post("/api/reset")
        except SmokeFailure as exc:
            print(f"WARN final reset failed: {exc}", file=sys.stderr)
    print("PASS mine-safety public HTTP smoke test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
