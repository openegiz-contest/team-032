#!/usr/bin/env python3
"""Mine-safety telemetry publisher and local scenario-control HTTP API."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import logging
import mimetypes
import os
from pathlib import Path
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from model import MineSafetyModel, PUBLISH_INTERVAL_SECONDS, ZONE_ORDER


NAMESPACE = "org.openegiz.safety"
TOPIC_PREFIX = "telemetry"
STATIC_DIR = Path(__file__).with_name("static")
MAX_REQUEST_BYTES = 64 * 1024

log = logging.getLogger("mine-safety")


def feature(value: Any, unit: str | None = None) -> dict[str, Any]:
    properties = {"value": value}
    if unit is not None:
        properties["unit"] = unit
    return properties


def snapshot_to_twin_features(snapshot: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Map one public state snapshot to the 26 twin feature payloads."""
    risk = snapshot["risk"]
    scenario = snapshot["scenario"]
    advisor = snapshot["ventilation_advisor"]
    energy = snapshot["fan_energy"]
    evacuation = snapshot["evacuation_plan"]
    advised_final = advisor["forecast"]["with_advice"][-1]
    baseline_final = advisor["forecast"]["without_action"][-1]
    features: dict[str, dict[str, dict[str, Any]]] = {
        "mine-01": {
            "state": feature("alarm" if risk["level"] != "normal" else "normal"),
            "overall_risk": feature(risk["level"]),
            "active_scenario": feature(scenario["name"]),
            "affected_people": feature(len(risk["affected_people"]), "people"),
            "incident_zone": feature(scenario["zone"] or "none"),
            "recommendations": feature(risk["recommendations"]),
            "advisor_mode": feature(advisor["mode"]["id"]),
            "forecast_risk_10m": feature(baseline_final["risk_level"]),
            "advised_risk_10m": feature(advised_final["risk_level"]),
            "fan_targets_percent": feature(advisor["fan_targets_percent"]),
            "fan_power_current_kw": feature(energy["current"]["power_kw"], "kW"),
            "fan_power_recommended_kw": feature(energy["recommended"]["power_kw"], "kW"),
            "fan_energy_10m_kwh": feature(energy["recommended"]["energy_10m_kwh"], "kWh"),
            "fan_energy_savings_percent": feature(energy["comparison"]["savings_vs_full_percent"], "%"),
            "evacuation_status": feature(evacuation["status"]),
            "evacuation_routed_people": feature(evacuation["routed_people"], "people"),
            "evacuation_unrouted_people": feature(evacuation["unrouted_people"], "people"),
            "evacuation_routes": feature([
                {"from": item["from_zone"], "to": item["destination"], "status": item["status"], "path": item["path"]}
                for item in evacuation["routes"]
            ]),
        }
    }

    active_targets: dict[str, list[str]] = {zone: [] for zone in ZONE_ORDER}
    for connection in snapshot["topology"]["connections"]:
        if connection["active"]:
            active_targets[connection["from"]].append(connection["to"])

    for zone in ZONE_ORDER:
        readings = snapshot["sensors"][zone]
        zone_risk = risk["by_zone"][zone]
        features[f"zone-{zone.replace('_', '-')}"] = {
            "state": feature("danger" if zone_risk["level"] != "normal" else "normal"),
            "current_zone": feature(zone),
            "risk_level": feature(zone_risk["level"]),
            "risk_causes": feature(zone_risk["causes"]),
            "airflow_direction": feature(",".join(active_targets[zone]) or "return"),
            "airflow_speed": feature(readings["airflow_mps"], "m/s"),
        }
        features[f"sensor-{zone.replace('_', '-')}"] = {
            "state": feature("online"),
            "current_zone": feature(zone),
            "methane_pct": feature(readings["methane_pct"], "%"),
            "co_ppm": feature(readings["co_ppm"], "ppm"),
            "smoke_index": feature(readings["smoke_index"]),
            "temperature_c": feature(readings["temperature_c"], "°C"),
            "airflow_mps": feature(readings["airflow_mps"], "m/s"),
        }

    for fan_id, fan in snapshot["fans"].items():
        fan_energy = energy["fans"][fan_id]
        features[fan_id] = {
            "state": feature(fan["state"]),
            "current_zone": feature(fan["zone"]),
            "speed_percent": feature(fan["speed_percent"], "%"),
            "airflow": feature(snapshot["sensors"][fan["zone"]]["airflow_mps"], "m/s"),
            "power_kw": feature(fan_energy["current_power_kw"], "kW"),
            "recommended_power_kw": feature(fan_energy["recommended_power_kw"], "kW"),
        }

    for worker_id, worker in snapshot["workers"].items():
        assigned_route = next(
            (item for item in evacuation["routes"] if worker_id in item["worker_ids"]),
            None,
        )
        features[worker_id] = {
            "state": feature(worker["state"]),
            "current_zone": feature(worker["zone"]),
            "evacuation_status": feature(
                assigned_route["status"] if assigned_route is not None else "none"
            ),
            "evacuation_destination": feature(
                assigned_route["destination"] if assigned_route is not None else "none"
            ),
        }

    for equipment_id, equipment in snapshot["equipment"].items():
        features[equipment_id] = {
            "state": feature(equipment["state"]),
            "current_zone": feature(equipment["zone"]),
        }
    return features


def ditto_modify_features(name: str, features: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a Ditto Protocol command replacing all features of one twin."""
    return {
        "topic": f"{NAMESPACE}/{name}/things/twin/commands/modify",
        "path": "/features",
        "value": {feature_name: {"properties": properties} for feature_name, properties in features.items()},
    }


STREAMING_SENSOR_TWIN_NAMES = (
    "sensor-shaft",
    "sensor-main-drift",
    "sensor-section-a",
    "sensor-section-b",
    "sensor-refuge-chamber",
)
STREAMING_TWIN_NAMES = ("mine-01", *STREAMING_SENSOR_TWIN_NAMES)
SENSOR_PARENT_IDS = {
    name: f"org.openegiz.safety:zone-{name.removeprefix('sensor-')}"
    for name in STREAMING_SENSOR_TWIN_NAMES
}


def history_features(name: str, features: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep MQTT history events flat enough for Telegraf's json_v2 parser."""
    if name != "mine-01":
        return features
    return {
        feature_name: properties
        for feature_name, properties in features.items()
        if isinstance(properties.get("value"), (str, int, float, bool))
    }


def publish_snapshot(
    publisher: Any,
    snapshot: dict[str, Any],
    twin_names: tuple[str, ...] | None = None,
) -> int:
    """Publish selected twins, or the complete snapshot when no selection is given."""
    twin_features = snapshot_to_twin_features(snapshot)
    selected_names = tuple(twin_features) if twin_names is None else twin_names
    for name in selected_names:
        features = twin_features[name]
        payload = json.dumps(ditto_modify_features(name, features), ensure_ascii=False, separators=(",", ":"))
        result = publisher.publish(f"{TOPIC_PREFIX}/{name}", payload, qos=0)
        if hasattr(result, "rc") and result.rc != 0:
            raise RuntimeError(f"MQTT publish failed for {name}: rc={result.rc}")
    return len(selected_names)


def publish_history_snapshot(publisher: Any, snapshot: dict[str, Any]) -> int:
    """Publish history events in the native topic/JSON shape consumed by Telegraf."""
    twin_features = snapshot_to_twin_features(snapshot)
    for name in STREAMING_TWIN_NAMES:
        thing_id = f"{NAMESPACE}:{name}"
        extra = {"thingId": thing_id}
        parent_id = SENSOR_PARENT_IDS.get(name)
        if parent_id:
            extra["attributes"] = {"_parents": parent_id}
        event_features = history_features(name, twin_features[name])
        event = {
            "topic": f"{NAMESPACE}/{name}/things/twin/events/modified",
            "headers": {
                "ditto-originator": "org.openegiz.safety:mine-safety",
                "response-required": False,
                "version": 2,
                "requested-acks": [],
                "content-type": "application/json",
            },
            "path": "/features",
            "value": {
                feature_name: {"properties": properties}
                for feature_name, properties in event_features.items()
            },
            "extra": extra,
            "revision": snapshot["tick"],
            "timestamp": snapshot["timestamp"],
        }
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        topic = f"opentwins/twin/events/{NAMESPACE}/{name}"
        result = publisher.publish(topic, payload, qos=1)
        if hasattr(result, "rc") and result.rc != 0:
            raise RuntimeError(f"MQTT history publish failed for {name}: rc={result.rc}")
    return len(STREAMING_TWIN_NAMES)


def enrich_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Add API-oriented airflow and incident summaries without mutating the model."""
    result = deepcopy(snapshot)
    result["airflow"] = {
        zone: {
            "speed_mps": result["sensors"][zone]["airflow_mps"],
            "outgoing": [
                item["to"]
                for item in result["topology"]["connections"]
                if item["from"] == zone and item["active"]
            ],
        }
        for zone in ZONE_ORDER
    }
    causes = {
        zone: result["risk"]["by_zone"][zone]["causes"]
        for zone in result["risk"]["affected_zones"]
    }
    result["incident"] = {
        "active": result["scenario"]["name"] != "none",
        "type": result["scenario"]["name"],
        "source_zone": result["scenario"]["zone"],
        "level": result["risk"]["level"],
        "affected_zones": result["risk"]["affected_zones"],
        "affected_people": result["risk"]["affected_people"],
        "causes": causes,
        "recommendations": result["risk"]["recommendations"],
    }
    return result


class SafetyApplication:
    """Thread-safe owner of model state, publishing and scenario commands."""

    def __init__(self, model: MineSafetyModel, publisher: Any | None, interval: float = PUBLISH_INTERVAL_SECONDS):
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.model = model
        self.publisher = publisher
        self.interval = interval
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.telemetry_thread: threading.Thread | None = None
        self.latest = enrich_snapshot(model.snapshot())
        self.last_publish_error: str | None = None

    def state(self) -> dict[str, Any]:
        with self.lock:
            return deepcopy(self.latest)

    def _publish_locked(self, twin_names: tuple[str, ...] | None = None) -> None:
        if self.publisher is None:
            return
        try:
            publish_snapshot(self.publisher, self.latest, twin_names)
            self.last_publish_error = None
        except Exception as exc:  # the loop must survive a temporary broker failure
            self.last_publish_error = str(exc)
            log.exception("telemetry publish failed")

    def _publish_history_locked(self) -> None:
        if self.publisher is None:
            return
        try:
            publish_history_snapshot(self.publisher, self.latest)
            self.last_publish_error = None
        except Exception as exc:  # the loop must survive a temporary broker failure
            self.last_publish_error = str(exc)
            log.exception("history publish failed")

    def run_cycle(self) -> dict[str, Any]:
        with self.lock:
            self.latest = enrich_snapshot(self.model.step())
            self._publish_history_locked()
            return deepcopy(self.latest)

    def start_scenario(self, scenario: str, zone: str | None) -> dict[str, Any]:
        with self.lock:
            self.model.start_scenario(scenario, zone)
            self.latest = enrich_snapshot(self.model.snapshot())
            # The public API changes immediately. MQTT remains on its paced
            # history route and does not enqueue a Ditto modify command.
            self._publish_history_locked()
            return deepcopy(self.latest)

    def reset(self) -> dict[str, Any]:
        with self.lock:
            self.model.reset()
            self.latest = enrich_snapshot(self.model.snapshot())
            self._publish_history_locked()
            return deepcopy(self.latest)

    def health(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": "ok" if self.last_publish_error is None else "degraded",
                "tick": self.latest["tick"],
                "publish_interval_seconds": self.interval,
                "mqtt_publish_interval_seconds": self.interval,
                "mqtt_history_route": "opentwins",
                "mqtt_enabled": self.publisher is not None,
                "last_publish_error": self.last_publish_error,
            }

    def start(self) -> None:
        if self.telemetry_thread and self.telemetry_thread.is_alive():
            return
        self.stop_event.clear()
        with self.lock:
            # Setup creates the 26 Ditto twins. Historical telemetry uses the
            # established opentwins MQTT format consumed by Telegraf, avoiding
            # a permanent command backlog in a 3.5 GiB Ditto cluster.
            self._publish_history_locked()
        self.telemetry_thread = threading.Thread(target=self._telemetry_loop, name="telemetry", daemon=True)
        self.telemetry_thread.start()

    def _telemetry_loop(self) -> None:
        while not self.stop_event.wait(self.interval):
            self.run_cycle()

    def stop(self) -> None:
        self.stop_event.set()
        if self.telemetry_thread:
            self.telemetry_thread.join(timeout=max(1.0, self.interval + 0.5))


class SafetyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], application: SafetyApplication):
        self.application = application
        super().__init__(server_address, SafetyRequestHandler)


class SafetyRequestHandler(BaseHTTPRequestHandler):
    server: SafetyHTTPServer

    def log_message(self, message: str, *args: Any) -> None:
        log.info("http %s - %s", self.address_string(), message % args)

    def _json(self, status: HTTPStatus | int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _request_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request body is too large")
        if length == 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid UTF-8 JSON") from exc
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = unquote(urlparse(self.path).path)
        if path == "/health":
            self._json(HTTPStatus.OK, self.server.application.health())
            return
        if path == "/api/state":
            self._json(HTTPStatus.OK, self.server.application.state())
            return
        if path == "/" or path.startswith("/static/"):
            self._serve_static(path)
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = unquote(urlparse(self.path).path)
        try:
            body = self._request_json()
            if path == "/api/reset":
                self._json(HTTPStatus.OK, self.server.application.reset())
                return
            prefix = "/api/scenarios/"
            if path.startswith(prefix) and path.count("/") == 3:
                scenario = path[len(prefix):]
                zone = body.get("zone")
                if zone is not None and not isinstance(zone, str):
                    raise ValueError("zone must be a string")
                self._json(HTTPStatus.OK, self.server.application.start_scenario(scenario, zone))
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "message": str(exc)})

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path == "/" else path.removeprefix("/static/")
        requested = (STATIC_DIR / relative).resolve()
        static_root = STATIC_DIR.resolve()
        if static_root not in requested.parents and requested != static_root:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not requested.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "ui_not_built"})
            return
        payload = requested.read_bytes()
        mime_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime_type}; charset=utf-8" if mime_type.startswith("text/") else mime_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)


def connect_mqtt(host: str, port: int, attempts: int = 30, delay: float = 2.0) -> Any:
    """Connect a paho client lazily so model/API tests need no broker package."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"openegiz-safety-{os.getpid()}")
    for attempt in range(1, attempts + 1):
        try:
            client.connect(host, port, keepalive=30)
            client.loop_start()
            return client
        except OSError as exc:
            if attempt == attempts:
                raise
            log.warning("MQTT %s:%s unavailable (%s), retry %s/%s", host, port, exc, attempt, attempts)
            time.sleep(delay)
    raise RuntimeError("unreachable MQTT retry state")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HTTP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HTTP_PORT", "8091")))
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "mosquitto"))
    parser.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("SIM_INTERVAL", "2")))
    parser.add_argument("--seed", type=int, default=int(os.environ.get("SIM_SEED", "42")))
    parser.add_argument("--no-mqtt", action="store_true", help="run API without MQTT (development only)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    mqtt_client = None if args.no_mqtt else connect_mqtt(args.mqtt_host, args.mqtt_port)
    application = SafetyApplication(MineSafetyModel(seed=args.seed), mqtt_client, interval=args.interval)
    server = SafetyHTTPServer((args.host, args.port), application)

    def request_stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    application.start()
    log.info("mine-safety API listening on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        application.stop()
        server.server_close()
        if mqtt_client is not None:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
