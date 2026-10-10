#!/usr/bin/env python3
"""Deterministic ventilation and safety model for the OpenEgiz mine demo.

The thresholds below are explicit demonstration assumptions.  They are not
industrial limits and must not be used to control real mine equipment.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
import random
from typing import Any


PUBLISH_INTERVAL_SECONDS = 2

ZONE_ORDER = ("shaft", "main_drift", "section_a", "section_b", "refuge_chamber")
ZONE_NAMES = {
    "shaft": "Ствол",
    "main_drift": "Главный штрек",
    "section_a": "Добычный участок A",
    "section_b": "Добычный участок B",
    "refuge_chamber": "Эвакуационная камера",
}

# Supply-air direction used by the visualisation and propagation model.
AIRFLOW_EDGES = (
    ("shaft", "main_drift"),
    ("main_drift", "section_a"),
    ("main_drift", "section_b"),
    ("main_drift", "refuge_chamber"),
)

BASE_AIRFLOW_MPS = {
    "shaft": 3.2,
    "main_drift": 2.8,
    "section_a": 2.1,
    "section_b": 2.0,
    "refuge_chamber": 1.5,
}

BASE_SENSORS = {
    "shaft": {"methane_pct": 0.18, "co_ppm": 4.0, "smoke_index": 0.02, "temperature_c": 19.0},
    "main_drift": {"methane_pct": 0.22, "co_ppm": 5.0, "smoke_index": 0.02, "temperature_c": 20.0},
    "section_a": {"methane_pct": 0.35, "co_ppm": 6.0, "smoke_index": 0.03, "temperature_c": 23.0},
    "section_b": {"methane_pct": 0.32, "co_ppm": 6.0, "smoke_index": 0.03, "temperature_c": 22.0},
    "refuge_chamber": {"methane_pct": 0.12, "co_ppm": 3.0, "smoke_index": 0.01, "temperature_c": 20.0},
}

# warning and critical thresholds, all intentionally conservative demo values.
THRESHOLDS = {
    "methane_pct": {"unit": "%", "warning": 1.0, "critical": 2.0, "direction": "high"},
    "co_ppm": {"unit": "ppm", "warning": 25.0, "critical": 50.0, "direction": "high"},
    "smoke_index": {"unit": "index", "warning": 0.2, "critical": 0.5, "direction": "high"},
    "temperature_c": {"unit": "°C", "warning": 35.0, "critical": 50.0, "direction": "high"},
    "airflow_mps": {"unit": "m/s", "warning": 0.8, "critical": 0.3, "direction": "low"},
}

NORMAL_RANGES = {
    "methane_pct": (0.08, 0.45),
    "co_ppm": (2.0, 8.0),
    "smoke_index": (0.0, 0.05),
    "temperature_c": (18.0, 25.0),
    "airflow_mps": (1.3, 3.5),
}

WORKER_ZONES = {
    "worker-01": "shaft",
    "worker-02": "main_drift",
    "worker-03": "main_drift",
    "worker-04": "section_a",
    "worker-05": "section_a",
    "worker-06": "section_a",
    "worker-07": "section_b",
    "worker-08": "section_b",
    "worker-09": "section_b",
    "worker-10": "refuge_chamber",
}

EQUIPMENT_ZONES = {"loader-01": "section_a", "transport-01": "section_b"}

VALID_SCENARIOS = {"main_fan_failure", "methane_release", "fire", "blocked_working"}
MINING_SECTIONS = {"section_a", "section_b"}
FIRE_ZONES = {"main_drift", "section_a", "section_b"}

METRIC_LABELS = {
    "methane_pct": "метан",
    "co_ppm": "CO",
    "smoke_index": "дым",
    "temperature_c": "температура",
    "airflow_mps": "скорость воздуха",
}

FORECAST_HORIZON_MINUTES = 10
FORECAST_STEP_MINUTES = 2
RISK_RANK = {"normal": 0, "warning": 1, "critical": 2}

# Demonstration nameplate values and tariff for a transparent energy estimate.
# Variable-speed fan power follows the affinity-law approximation P ~ speed^3.
FAN_RATED_POWER_KW = {"fan-main": 180.0, "fan-aux-a": 45.0, "fan-aux-b": 45.0}
ENERGY_TARIFF_KZT_PER_KWH = 30.0
EVACUATION_DESTINATIONS = ("shaft", "refuge_chamber")
EVACUATION_EDGE_MINUTES = 1.5


def downstream_zones(source: str, edges: tuple[tuple[str, str], ...]) -> dict[str, int]:
    """Return reachable zones and hop distances in deterministic breadth-first order."""
    distances = {source: 0}
    queue = [source]
    while queue:
        current = queue.pop(0)
        for start, end in edges:
            if start == current and end not in distances:
                distances[end] = distances[current] + 1
                queue.append(end)
    return distances


def metric_severity(metric: str, value: float) -> int:
    """Return 0 normal, 1 warning or 2 critical for one explicit threshold."""
    rule = THRESHOLDS[metric]
    if rule["direction"] == "high":
        if value >= rule["critical"]:
            return 2
        if value >= rule["warning"]:
            return 1
    else:
        if value <= rule["critical"]:
            return 2
        if value <= rule["warning"]:
            return 1
    return 0


def evaluate_risk(sensors: dict[str, dict[str, float]], workers: dict[str, str]) -> dict[str, Any]:
    """Pure, deterministic risk assessment from readings and worker locations."""
    zone_results: dict[str, dict[str, Any]] = {}
    recommendations: list[str] = []
    affected_zones: list[str] = []

    for zone in ZONE_ORDER:
        readings = sensors[zone]
        causes: list[str] = []
        severity = 0
        triggered: set[str] = set()
        for metric in THRESHOLDS:
            value = readings[metric]
            current = metric_severity(metric, value)
            if current:
                severity = max(severity, current)
                triggered.add(metric)
                limit_name = "критического" if current == 2 else "предупредительного"
                causes.append(
                    f"{METRIC_LABELS[metric]}: {value:g} {THRESHOLDS[metric]['unit']} — "
                    f"достижение {limit_name} порога"
                )

        level = ("normal", "warning", "critical")[severity]
        zone_results[zone] = {"level": level, "causes": causes}
        if severity:
            affected_zones.append(zone)
        if "airflow_mps" in triggered:
            recommendations.append("Проверить главный и вспомогательные вентиляторы; подготовить резервный режим вентиляции.")
        if "methane_pct" in triggered:
            recommendations.append(f"Закрыть доступ в зону «{ZONE_NAMES[zone]}» и вывести людей по безопасному маршруту.")
        if triggered.intersection({"co_ppm", "smoke_index", "temperature_c"}):
            recommendations.append(f"Объявить пожарную тревогу в зоне «{ZONE_NAMES[zone]}» и начать ручную эвакуацию.")

    affected_people = [worker_id for worker_id, zone in workers.items() if zone in affected_zones]
    overall_severity = max(
        (("normal", "warning", "critical").index(item["level"]) for item in zone_results.values()),
        default=0,
    )
    # Preserve the stable first occurrence of every recommendation.
    unique_recommendations = list(dict.fromkeys(recommendations))
    if not unique_recommendations:
        unique_recommendations = ["Продолжать штатный мониторинг вентиляции и газовой обстановки."]

    return {
        "level": ("normal", "warning", "critical")[overall_severity],
        "by_zone": zone_results,
        "affected_zones": affected_zones,
        "affected_people": affected_people,
        "recommendations": unique_recommendations,
    }


def _normal_target(zone: str) -> dict[str, float]:
    """Return the stable target used by the transparent advisory projection."""
    return {**BASE_SENSORS[zone], "airflow_mps": BASE_AIRFLOW_MPS[zone]}


def _round_readings(readings: dict[str, float]) -> dict[str, float]:
    return {
        "methane_pct": round(max(0.0, readings["methane_pct"]), 3),
        "co_ppm": round(max(0.0, readings["co_ppm"]), 1),
        "smoke_index": round(max(0.0, readings["smoke_index"]), 3),
        "temperature_c": round(readings["temperature_c"], 1),
        "airflow_mps": round(max(0.0, readings["airflow_mps"]), 2),
    }


def _project_without_action(
    sensors: dict[str, dict[str, float]],
    scenario: str,
    source: str | None,
    active_edges: tuple[tuple[str, str], ...],
    minute: int,
) -> dict[str, dict[str, float]]:
    projected = deepcopy(sensors)
    if minute == 0 or scenario == "none":
        return projected
    if scenario == "main_fan_failure":
        for zone in ZONE_ORDER:
            projected[zone]["airflow_mps"] -= 0.005 * minute
            if zone != "shaft":
                projected[zone]["methane_pct"] += 0.07 * minute
                projected[zone]["co_ppm"] += 0.25 * minute
    elif scenario == "methane_release" and source:
        projected[source]["methane_pct"] += 0.08 * minute
    elif scenario == "fire" and source:
        for zone, distance in downstream_zones(source, active_edges).items():
            factor = 0.55 ** distance
            projected[zone]["co_ppm"] += 3.0 * factor * minute
            projected[zone]["smoke_index"] += 0.025 * factor * minute
            projected[zone]["temperature_c"] += 0.8 * factor * minute
    elif scenario == "blocked_working" and source:
        projected[source]["airflow_mps"] -= 0.005 * minute
        projected[source]["methane_pct"] += 0.06 * minute
    return {zone: _round_readings(values) for zone, values in projected.items()}


def _project_with_advice(
    sensors: dict[str, dict[str, float]],
    scenario: str,
    source: str | None,
    minute: int,
) -> dict[str, dict[str, float]]:
    """Project a hypothetical response; this never mutates fans or scenario state."""
    if minute == 0:
        return deepcopy(sensors)
    recovery = 1.0 - math.exp(-minute / 2.5)
    projected: dict[str, dict[str, float]] = {}
    for zone in ZONE_ORDER:
        target = _normal_target(zone)
        if scenario == "fire" and zone == source:
            # A source fire remains critical; the advice aims to contain downstream exposure.
            target = {**target, "co_ppm": 55.0, "smoke_index": 0.55, "temperature_c": 52.0}
        projected[zone] = _round_readings({
            metric: current + (target[metric] - current) * recovery
            for metric, current in sensors[zone].items()
        })
    return projected


def _forecast_point(minute: int, sensors: dict[str, dict[str, float]]) -> dict[str, Any]:
    risk = evaluate_risk(sensors, WORKER_ZONES)
    readings = list(sensors.values())
    return {
        "minute": minute,
        "risk_level": risk["level"],
        "affected_zones": risk["affected_zones"],
        "affected_people": len(risk["affected_people"]),
        "max_methane_pct": round(max(item["methane_pct"] for item in readings), 3),
        "max_co_ppm": round(max(item["co_ppm"] for item in readings), 1),
        "min_airflow_mps": round(min(item["airflow_mps"] for item in readings), 2),
    }


def build_ventilation_advisor(
    sensors: dict[str, dict[str, float]],
    scenario: str,
    source: str | None,
    active_edges: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    """Build a deterministic 10-minute comparison without controlling equipment."""
    target_speeds = {"fan-main": 100, "fan-aux-a": 75, "fan-aux-b": 75}
    actions = ["Сохранить текущий режим и продолжать контроль телеметрии."]
    mode_id = "normal_monitoring"
    mode_title = "Штатный контроль"

    if scenario == "main_fan_failure":
        mode_id = "restore_main_fan"
        mode_title = "Восстановление главного потока"
        target_speeds.update({"fan-aux-a": 90, "fan-aux-b": 90})
        actions = [
            "Восстановить главный вентилятор или переключить его на резерв.",
            "До восстановления поднять готовность вспомогательных вентиляторов до 90%.",
            "Ограничить работы до подтверждения устойчивого воздушного потока.",
        ]
    elif scenario == "methane_release" and source:
        mode_id = "purge_methane"
        mode_title = "Проветривание метановой зоны"
        target_speeds[f"fan-aux-{'a' if source == 'section_a' else 'b'}"] = 100
        actions = [
            f"Закрыть доступ в зону «{ZONE_NAMES[source]}» и вывести людей.",
            "Установить вспомогательный вентилятор выбранного участка на 100% после ручной проверки.",
            "Контролировать CH₄ до устойчивого возврата ниже предупредительного порога.",
        ]
    elif scenario == "fire" and source:
        mode_id = "contain_fire_smoke"
        mode_title = "Локализация дыма"
        target_speeds["fan-main"] = 70
        if source == "section_a":
            target_speeds.update({"fan-aux-a": 0, "fan-aux-b": 85})
        elif source == "section_b":
            target_speeds.update({"fan-aux-a": 85, "fan-aux-b": 0})
        else:
            target_speeds.update({"fan-aux-a": 60, "fan-aux-b": 60})
        actions = [
            f"Локализовать ветвь зоны «{ZONE_NAMES[source]}» по аварийному плану.",
            "Сохранить подпор воздуха к эвакуационной камере и не менять режим без ручного подтверждения.",
            "Начать эвакуацию затронутых зон и непрерывно контролировать CO и дым.",
        ]
    elif scenario == "blocked_working" and source:
        mode_id = "restore_blocked_branch"
        mode_title = "Восстановление ветви"
        target_speeds[f"fan-aux-{'a' if source == 'section_a' else 'b'}"] = 100
        actions = [
            f"Проверить и восстановить воздушную связь с зоной «{ZONE_NAMES[source]}».",
            "После подтверждения проходимости установить местный вентилятор на 100%.",
            "Не возобновлять работы до нормализации потока и метана.",
        ]

    minutes = list(range(0, FORECAST_HORIZON_MINUTES + 1, FORECAST_STEP_MINUTES))
    without_action = [
        _forecast_point(minute, _project_without_action(sensors, scenario, source, active_edges, minute))
        for minute in minutes
    ]
    with_advice = [
        _forecast_point(minute, _project_with_advice(sensors, scenario, source, minute))
        for minute in minutes
    ]
    final_without = without_action[-1]
    final_with = with_advice[-1]
    improved = (
        RISK_RANK[final_with["risk_level"]] < RISK_RANK[final_without["risk_level"]]
        or len(final_with["affected_zones"]) < len(final_without["affected_zones"])
    )
    if scenario == "none":
        summary = "На горизонте 10 минут ожидается штатный режим; менять вентиляцию не требуется."
    elif improved:
        summary = (
            f"При выполнении совета через 10 минут ожидается {len(final_with['affected_zones'])} "
            f"затронутых зон вместо {len(final_without['affected_zones'])}."
        )
    else:
        summary = "Даже после рекомендуемых мер риск остаётся критическим: требуется эвакуация и ручное подтверждение плана."

    return {
        "status": "stable" if scenario == "none" else "action_required",
        "horizon_minutes": FORECAST_HORIZON_MINUTES,
        "step_minutes": FORECAST_STEP_MINUTES,
        "mode": {"id": mode_id, "title": mode_title},
        "fan_targets_percent": target_speeds,
        "actions": actions,
        "summary": summary,
        "forecast": {"without_action": without_action, "with_advice": with_advice},
        "assumptions": [
            "Детерминированная демонстрационная экстраполяция, не CFD и не машинное обучение.",
            "Траектория «при совете» гипотетическая и не изменяет оборудование автоматически.",
            "Прогноз пересчитывается по текущим датчикам, сценарию и активному графу воздуха.",
        ],
    }


def _fan_power_kw(rated_power_kw: float, speed_percent: int | float, running: bool = True) -> float:
    """Estimate variable-speed fan power using the cubic affinity law."""
    if not running or speed_percent <= 0:
        return 0.0
    speed_ratio = min(float(speed_percent), 100.0) / 100.0
    return rated_power_kw * speed_ratio ** 3


def build_fan_energy_analysis(
    fans: dict[str, dict[str, Any]],
    target_speeds: dict[str, int],
    scenario: str,
) -> dict[str, Any]:
    """Compare current and advised fan energy without issuing control commands."""
    hours = FORECAST_HORIZON_MINUTES / 60.0
    breakdown: dict[str, dict[str, Any]] = {}
    for fan_id, rated_power in FAN_RATED_POWER_KW.items():
        fan = fans[fan_id]
        current_speed = fan["speed_percent"]
        target_speed = target_speeds[fan_id]
        current_power = _fan_power_kw(rated_power, current_speed, fan["state"] == "running")
        target_power = _fan_power_kw(rated_power, target_speed, target_speed > 0)
        breakdown[fan_id] = {
            "rated_power_kw": rated_power,
            "current_speed_percent": current_speed,
            "recommended_speed_percent": target_speed,
            "current_power_kw": round(current_power, 2),
            "recommended_power_kw": round(target_power, 2),
        }

    current_power = sum(
        _fan_power_kw(
            FAN_RATED_POWER_KW[fan_id],
            fans[fan_id]["speed_percent"],
            fans[fan_id]["state"] == "running",
        )
        for fan_id in FAN_RATED_POWER_KW
    )
    recommended_power = sum(
        _fan_power_kw(FAN_RATED_POWER_KW[fan_id], target_speeds[fan_id], target_speeds[fan_id] > 0)
        for fan_id in FAN_RATED_POWER_KW
    )
    full_power = sum(FAN_RATED_POWER_KW.values())
    current_energy = current_power * hours
    recommended_energy = recommended_power * hours
    full_energy = full_power * hours
    delta_power = recommended_power - current_power
    savings_power = full_power - recommended_power
    savings_percent = savings_power / full_power * 100.0 if full_power else 0.0

    if scenario == "none":
        summary = (
            f"Штатный безопасный режим экономит {savings_power:.1f} кВт "
            f"({savings_percent:.1f}%) относительно постоянной работы всех вентиляторов на 100%."
        )
    elif delta_power > 0:
        summary = (
            f"Для снижения риска совет требует дополнительно {delta_power:.1f} кВт; "
            "безопасность имеет приоритет над экономией."
        )
    else:
        summary = (
            f"Рекомендуемый аварийный режим снижает нагрузку на {-delta_power:.1f} кВт, "
            "но применять его можно только после ручного подтверждения плана безопасности."
        )

    def totals(power_kw: float, energy_kwh: float) -> dict[str, float]:
        return {
            "power_kw": round(power_kw, 2),
            "energy_10m_kwh": round(energy_kwh, 2),
            "cost_10m_kzt": round(energy_kwh * ENERGY_TARIFF_KZT_PER_KWH, 2),
        }

    return {
        "horizon_minutes": FORECAST_HORIZON_MINUTES,
        "tariff_kzt_per_kwh": ENERGY_TARIFF_KZT_PER_KWH,
        "current": totals(current_power, current_energy),
        "recommended": totals(recommended_power, recommended_energy),
        "full_speed_reference": totals(full_power, full_energy),
        "comparison": {
            "power_delta_vs_current_kw": round(delta_power, 2),
            "savings_vs_full_power_kw": round(savings_power, 2),
            "savings_vs_full_percent": round(savings_percent, 1),
            "projected_daily_savings_kwh": round(savings_power * 24.0, 1),
            "projected_daily_savings_kzt": round(
                savings_power * 24.0 * ENERGY_TARIFF_KZT_PER_KWH, 0
            ),
        },
        "fans": breakdown,
        "summary": summary,
        "safety_priority": scenario != "none",
        "assumptions": [
            "Паспортные мощности: главный вентилятор 180 кВт, вспомогательные по 45 кВт.",
            "Мощность оценена по кубической зависимости от относительной скорости вращения.",
            "Тариф 30 ₸/кВт·ч задан только для конкурсной демонстрации.",
            "Расчёт рекомендательный и не управляет оборудованием автоматически.",
        ],
    }


def _evacuation_paths(
    start: str,
    destination: str,
    active_edges: tuple[tuple[str, str], ...],
) -> list[list[str]]:
    """Enumerate simple paths in the small undirected evacuation graph."""
    neighbours: dict[str, list[str]] = {zone: [] for zone in ZONE_ORDER}
    for first, second in active_edges:
        neighbours[first].append(second)
        neighbours[second].append(first)
    paths: list[list[str]] = []

    def visit(current: str, path: list[str]) -> None:
        if current == destination:
            paths.append(path)
            return
        for neighbour in neighbours[current]:
            if neighbour not in path:
                visit(neighbour, [*path, neighbour])

    visit(start, [start])
    return paths


def build_evacuation_plan(
    risk: dict[str, Any],
    workers: dict[str, dict[str, str]],
    active_edges: tuple[tuple[str, str], ...],
    scenario: str,
) -> dict[str, Any]:
    """Build deterministic advisory routes without initiating an evacuation."""
    affected = risk["affected_people"]
    if scenario == "none" or not affected:
        return {
            "status": "standby",
            "total_people": 0,
            "routed_people": 0,
            "unrouted_people": 0,
            "routes": [],
            "summary": "Эвакуация не требуется; маршруты готовы к расчёту.",
            "manual_confirmation_required": True,
            "assumptions": [
                "Маршруты рассчитываются по упрощённому графу активных связей.",
                "Одна связь соответствует 1,5 минуты движения без учёта задержек.",
                "План рекомендательный и не запускает эвакуацию автоматически.",
            ],
        }

    affected_by_zone: dict[str, list[str]] = {}
    for worker_id in affected:
        affected_by_zone.setdefault(workers[worker_id]["zone"], []).append(worker_id)

    routes: list[dict[str, Any]] = []
    for start in ZONE_ORDER:
        worker_ids = affected_by_zone.get(start)
        if not worker_ids:
            continue
        candidates: list[tuple[float, int, list[str], str]] = []
        for destination_index, destination in enumerate(EVACUATION_DESTINATIONS):
            for path in _evacuation_paths(start, destination, active_edges):
                transit_risk = sum(
                    RISK_RANK[risk["by_zone"][zone]["level"]]
                    for zone in path[1:]
                )
                destination_risk = RISK_RANK[risk["by_zone"][destination]["level"]]
                # A safe destination is more important than a shorter route.
                score = len(path) - 1 + transit_risk * 10 + destination_risk * 100
                candidates.append((score, destination_index, path, destination))

        if not candidates:
            routes.append({
                "id": f"route-{start}",
                "from_zone": start,
                "destination": None,
                "destination_kind": None,
                "path": [start],
                "worker_ids": worker_ids,
                "people_count": len(worker_ids),
                "status": "blocked",
                "estimated_minutes": None,
                "instruction": "Безопасный выход по активным связям недоступен: укрыться и запросить спасательную группу.",
            })
            continue

        _, _, path, destination = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        dangerous_transit = [
            zone for zone in path[1:]
            if risk["by_zone"][zone]["level"] != "normal"
        ]
        route_status = "caution" if dangerous_transit else "safe"
        destination_kind = "surface" if destination == "shaft" else "refuge"
        if route_status == "safe":
            instruction = f"Следовать к точке «{ZONE_NAMES[destination]}» по выделенному маршруту."
        else:
            instruction = "Маршрут проходит через опасную зону; движение только после подтверждения диспетчера."
        routes.append({
            "id": f"route-{start}",
            "from_zone": start,
            "destination": destination,
            "destination_kind": destination_kind,
            "path": path,
            "worker_ids": worker_ids,
            "people_count": len(worker_ids),
            "status": route_status,
            "estimated_minutes": round(max(0.5, (len(path) - 1) * EVACUATION_EDGE_MINUTES), 1),
            "instruction": instruction,
        })

    routed = sum(item["people_count"] for item in routes if item["status"] != "blocked")
    unrouted = len(affected) - routed
    caution = any(item["status"] == "caution" for item in routes)
    if unrouted:
        status = "blocked"
        summary = f"Для {unrouted} человек маршрут перекрыт; требуется спасательная группа."
    elif caution:
        status = "caution"
        summary = f"Маршруты построены для {routed} человек, но часть пути проходит через опасные зоны."
    else:
        status = "ready"
        summary = f"Безопасные маршруты построены для {routed} человек."
    return {
        "status": status,
        "total_people": len(affected),
        "routed_people": routed,
        "unrouted_people": unrouted,
        "routes": routes,
        "summary": summary,
        "manual_confirmation_required": True,
        "assumptions": [
            "Маршруты рассчитываются по упрощённому графу активных связей.",
            "Опасные транзитные зоны получают штраф, но могут использоваться при отсутствии безопасной альтернативы.",
            "Одна связь соответствует 1,5 минуты движения без учёта задержек.",
            "План рекомендательный и не запускает эвакуацию автоматически.",
        ],
    }


class MineSafetyModel:
    """In-memory deterministic model stepped at a fixed two-second interval."""

    def __init__(self, seed: int = 42):
        self.seed = seed
        self.tick = 0
        self.active_scenario = "none"
        self.scenario_zone: str | None = None

    def start_scenario(self, scenario: str, zone: str | None = None) -> None:
        if scenario not in VALID_SCENARIOS:
            raise ValueError(f"unknown scenario: {scenario}")
        if scenario in {"methane_release", "blocked_working"} and zone not in MINING_SECTIONS:
            raise ValueError(f"{scenario} requires zone section_a or section_b")
        if scenario == "fire":
            zone = zone or "main_drift"
            if zone not in FIRE_ZONES:
                raise ValueError("fire requires zone main_drift, section_a or section_b")
        if scenario == "main_fan_failure":
            zone = "shaft"
        self.active_scenario = scenario
        self.scenario_zone = zone

    def reset(self) -> None:
        self.tick = 0
        self.active_scenario = "none"
        self.scenario_zone = None

    def step(self) -> dict[str, Any]:
        self.tick += 1
        return self.snapshot()

    def _normal_sensors(self) -> dict[str, dict[str, float]]:
        sensors: dict[str, dict[str, float]] = {}
        for zone in ZONE_ORDER:
            rng = random.Random(f"{self.seed}:{self.tick}:{zone}")
            base = BASE_SENSORS[zone]
            sensors[zone] = {
                "methane_pct": round(base["methane_pct"] + rng.uniform(-0.025, 0.025), 3),
                "co_ppm": round(base["co_ppm"] + rng.uniform(-0.7, 0.7), 1),
                "smoke_index": round(max(0.0, base["smoke_index"] + rng.uniform(-0.006, 0.006)), 3),
                "temperature_c": round(base["temperature_c"] + rng.uniform(-0.4, 0.4), 1),
                "airflow_mps": round(BASE_AIRFLOW_MPS[zone] + rng.uniform(-0.08, 0.08), 2),
            }
        return sensors

    def _active_edges(self) -> tuple[tuple[str, str], ...]:
        if self.active_scenario == "blocked_working" and self.scenario_zone:
            blocked = ("main_drift", self.scenario_zone)
            return tuple(edge for edge in AIRFLOW_EDGES if edge != blocked)
        return AIRFLOW_EDGES

    def _apply_scenario(self, sensors: dict[str, dict[str, float]]) -> None:
        scenario = self.active_scenario
        source = self.scenario_zone
        if scenario == "none":
            return
        if scenario == "main_fan_failure":
            failed_flow = {
                "shaft": 0.20,
                "main_drift": 0.22,
                "section_a": 0.12,
                "section_b": 0.12,
                "refuge_chamber": 0.65,
            }
            for zone, value in failed_flow.items():
                sensors[zone]["airflow_mps"] = value
            return
        if scenario == "blocked_working" and source:
            sensors[source]["airflow_mps"] = 0.10
            sensors[source]["methane_pct"] = 1.25
            return
        if scenario == "methane_release" and source:
            sensors[source]["methane_pct"] = 2.80
            return
        if scenario == "fire" and source:
            distances = downstream_zones(source, self._active_edges())
            for zone, distance in distances.items():
                factor = 0.55 ** distance
                sensors[zone]["co_ppm"] = round(max(sensors[zone]["co_ppm"], 75.0 * factor), 1)
                sensors[zone]["smoke_index"] = round(max(sensors[zone]["smoke_index"], 0.8 * factor), 3)
                sensors[zone]["temperature_c"] = round(max(sensors[zone]["temperature_c"], 58.0 * factor), 1)

    def snapshot(self) -> dict[str, Any]:
        sensors = self._normal_sensors()
        self._apply_scenario(sensors)
        risk = evaluate_risk(sensors, WORKER_ZONES)
        active_edges = self._active_edges()
        connections = [
            {
                "from": start,
                "to": end,
                "active": (start, end) in active_edges,
                "direction": "supply",
            }
            for start, end in AIRFLOW_EDGES
        ]
        fans = {
            "fan-main": {
                "zone": "shaft",
                "state": "stopped" if self.active_scenario == "main_fan_failure" else "running",
                "speed_percent": 0 if self.active_scenario == "main_fan_failure" else 100,
            },
            "fan-aux-a": {"zone": "section_a", "state": "running", "speed_percent": 75},
            "fan-aux-b": {"zone": "section_b", "state": "running", "speed_percent": 75},
        }
        workers = {
            worker_id: {
                "zone": zone,
                "state": "affected" if worker_id in risk["affected_people"] else "safe",
            }
            for worker_id, zone in WORKER_ZONES.items()
        }
        equipment = {
            equipment_id: {
                "zone": zone,
                "state": "restricted" if zone in risk["affected_zones"] else "operating",
            }
            for equipment_id, zone in EQUIPMENT_ZONES.items()
        }
        ventilation_advisor = build_ventilation_advisor(
            sensors,
            self.active_scenario,
            self.scenario_zone,
            active_edges,
        )
        fan_energy = build_fan_energy_analysis(
            fans,
            ventilation_advisor["fan_targets_percent"],
            self.active_scenario,
        )
        evacuation_plan = build_evacuation_plan(
            risk,
            workers,
            active_edges,
            self.active_scenario,
        )
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tick": self.tick,
            "simulated_seconds": self.tick * PUBLISH_INTERVAL_SECONDS,
            "publish_interval_seconds": PUBLISH_INTERVAL_SECONDS,
            "scenario": {"name": self.active_scenario, "zone": self.scenario_zone},
            "topology": {"zones": list(ZONE_ORDER), "connections": connections},
            "sensors": deepcopy(sensors),
            "fans": fans,
            "workers": workers,
            "equipment": equipment,
            "risk": risk,
            "ventilation_advisor": ventilation_advisor,
            "fan_energy": fan_energy,
            "evacuation_plan": evacuation_plan,
            "assumptions": {
                "thresholds": deepcopy(THRESHOLDS),
                "industrial_use": False,
                "description": "Синтетическая демонстрационная модель, не нормативный расчёт.",
            },
        }
