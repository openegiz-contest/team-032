"use strict";

const POLL_INTERVAL_MS = 2000;
const SVG_NS = "http://www.w3.org/2000/svg";

const zoneNames = {
  shaft: "Ствол",
  main_drift: "Главный штрек",
  section_a: "Добычный участок A",
  section_b: "Добычный участок B",
  refuge_chamber: "Эвакуационная камера",
};

const scenarioNames = {
  none: "Штатный режим",
  main_fan_failure: "Отказ главного вентилятора",
  methane_release: "Выброс метана",
  fire: "Пожар",
  blocked_working: "Перекрытие выработки",
};

const riskLabels = { normal: "НОРМА", warning: "ВНИМАНИЕ", critical: "ОПАСНОСТЬ" };
const fanNames = { "fan-main": "Главный вентилятор", "fan-aux-a": "Вспомогательный A", "fan-aux-b": "Вспомогательный B" };

const markerPositions = {
  shaft: [[82, 390], [148, 450]],
  main_drift: [[395, 330], [445, 330], [600, 330]],
  section_a: [[875, 180], [930, 180], [990, 180], [1018, 142]],
  section_b: [[875, 475], [930, 475], [990, 475], [1018, 437]],
  refuge_chamber: [[640, 542]],
};

const equipmentPositions = { "loader-01": [1020, 210], "transport-01": [1020, 505] };
const routePositions = {
  shaft: [130, 310],
  main_drift: [520, 318],
  section_a: [942, 165],
  section_b: [942, 465],
  refuge_chamber: [520, 530],
};
let currentState = null;
let busy = false;
let toastTimer = null;

const byId = (id) => document.getElementById(id);
const all = (selector) => Array.from(document.querySelectorAll(selector));

function fmt(value, digits = 1) {
  return Number(value).toLocaleString("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function setText(id, value) {
  const element = byId(id);
  if (element) element.textContent = value;
}

function setConnection(online, message) {
  byId("live-dot").className = `live-dot ${online ? "online" : "offline"}`;
  setText("connection-label", online ? "Телеметрия активна" : "Нет связи");
  setText("updated-at", message);
}

function showToast(message, isError = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.className = `toast visible${isError ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = "toast"; }, 3200);
}

function renderKpis(state) {
  const readings = Object.values(state.sensors);
  const maxMethane = Math.max(...readings.map((item) => item.methane_pct));
  const maxCo = Math.max(...readings.map((item) => item.co_ppm));
  const minAirflow = Math.min(...readings.map((item) => item.airflow_mps));
  const risk = state.risk.level;
  const riskElement = byId("kpi-risk");
  riskElement.textContent = riskLabels[risk] || risk.toUpperCase();
  riskElement.className = `kpi-value risk-${risk}`;
  setText("kpi-risk-caption", risk === "normal" ? "Все зоны штатны" : `Опасных зон: ${state.risk.affected_zones.length}`);
  setText("kpi-methane", fmt(maxMethane, 2));
  setText("kpi-co", fmt(maxCo, 1));
  setText("kpi-airflow", fmt(minAirflow, 2));
  setText("kpi-people", String(state.risk.affected_people.length));
}

function renderZones(state) {
  all(".zone[data-zone]").forEach((element) => {
    const zone = element.dataset.zone;
    const readings = state.sensors[zone];
    const level = state.risk.by_zone[zone].level;
    element.classList.remove("normal", "warning", "critical");
    element.classList.add(level);
    element.setAttribute("aria-label", `${zoneNames[zone]}. Риск: ${riskLabels[level]}. Метан ${fmt(readings.methane_pct, 2)} процента, CO ${fmt(readings.co_ppm)} ppm, поток ${fmt(readings.airflow_mps, 2)} метра в секунду.`);
    setText(`metrics-${zone}`, `CH₄ ${fmt(readings.methane_pct, 2)}% · CO ${fmt(readings.co_ppm)} · ${fmt(readings.airflow_mps, 2)} м/с`);
  });

  state.topology.connections.forEach((edge) => {
    const id = `edge-${edge.from}-${edge.to}`;
    const element = byId(id);
    if (element) element.setAttribute("class", `air-edge ${edge.active ? "active" : "blocked"}`);
  });
}

function svgElement(name, attributes = {}) {
  const node = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function renderWorkers(state) {
  const layer = byId("worker-layer");
  layer.replaceChildren();
  const offsets = {};
  Object.entries(state.workers).forEach(([workerId, worker]) => {
    const index = offsets[worker.zone] || 0;
    offsets[worker.zone] = index + 1;
    const positions = markerPositions[worker.zone];
    const [x, y] = positions[index % positions.length];
    const group = svgElement("g", { class: `worker-marker ${worker.state}`, transform: `translate(${x} ${y})` });
    group.setAttribute("aria-label", `${workerId}, ${zoneNames[worker.zone]}, ${worker.state === "affected" ? "затронут" : "в безопасности"}`);
    const circle = svgElement("circle", { r: 12 });
    const label = svgElement("text", { "text-anchor": "middle", y: 3 });
    label.textContent = workerId.slice(-2);
    const title = svgElement("title");
    title.textContent = `${workerId} · ${zoneNames[worker.zone]}`;
    group.append(title, circle, label);
    layer.append(group);
  });
  setText("worker-summary", `${Object.keys(state.workers).length} в шахте · ${state.risk.affected_people.length} затронуто`);
}

function renderEquipment(state) {
  const layer = byId("equipment-layer");
  layer.replaceChildren();
  Object.entries(state.equipment).forEach(([equipmentId, equipment]) => {
    const [x, y] = equipmentPositions[equipmentId];
    const group = svgElement("g", { class: `equipment-marker ${equipment.state}`, transform: `translate(${x} ${y})` });
    const rect = svgElement("rect", { x: -25, y: -13, width: 50, height: 26, rx: 7 });
    const label = svgElement("text", { "text-anchor": "middle", y: 3 });
    label.textContent = equipmentId.startsWith("loader") ? "ПДМ" : "ТР";
    const title = svgElement("title");
    title.textContent = `${equipmentId} · ${zoneNames[equipment.zone]} · ${equipment.state}`;
    group.append(title, rect, label);
    layer.append(group);
  });
  const operating = Object.values(state.equipment).filter((item) => item.state === "operating").length;
  setText("equipment-summary", `${operating} из ${Object.keys(state.equipment).length} в работе`);
}

function renderFans(state) {
  const list = byId("fan-list");
  list.replaceChildren();
  let running = 0;
  Object.entries(state.fans).forEach(([fanId, fan]) => {
    const isRunning = fan.state === "running";
    if (isRunning) running += 1;
    const mapFan = byId(fanId);
    if (mapFan) mapFan.setAttribute("class", `fan${isRunning ? "" : " stopped"}`);

    const item = document.createElement("div");
    item.className = "fan-item";
    const name = document.createElement("strong");
    name.textContent = fanNames[fanId] || fanId;
    const location = document.createElement("span");
    location.textContent = `${zoneNames[fan.zone]} · ${fan.speed_percent}%`;
    const status = document.createElement("span");
    status.className = `fan-state${isRunning ? "" : " stopped"}`;
    status.textContent = isRunning ? "РАБОТАЕТ" : "ОСТАНОВЛЕН";
    item.append(name, location, status);
    list.append(item);
  });
  setText("fan-summary", `${running} из ${Object.keys(state.fans).length} работают`);
}

function renderList(element, values, emptyText) {
  element.replaceChildren();
  (values.length ? values : [emptyText]).forEach((value) => {
    const item = document.createElement("li");
    item.textContent = value;
    element.append(item);
  });
}

function renderIncident(state) {
  const incident = state.incident;
  const level = incident.level;
  const panel = byId("incident-panel");
  panel.classList.remove("normal", "warning", "critical");
  panel.classList.add(level);
  const badge = byId("incident-badge");
  badge.className = `status-badge ${level}`;
  badge.textContent = riskLabels[level];
  setText("incident-title", scenarioNames[incident.type] || incident.type);
  setText("incident-summary", incident.active
    ? `Зафиксировано отклонение «${scenarioNames[incident.type]}». Проверьте причины и подтвердите действия вручную.`
    : "Опасные отклонения не обнаружены. Вентиляция работает в штатном режиме.");
  setText("incident-source", incident.source_zone ? zoneNames[incident.source_zone] : "—");
  setText("incident-zones", String(incident.affected_zones.length));
  setText("incident-people", String(incident.affected_people.length));
  const causes = Object.entries(incident.causes).flatMap(([zone, items]) => items.map((item) => `${zoneNames[zone]}: ${item}`));
  renderList(byId("cause-list"), causes, "Показатели в норме");
  renderList(byId("recommendation-list"), incident.recommendations, "Продолжать штатный мониторинг.");
}

function renderEvacuation(state) {
  const plan = state.evacuation_plan;
  const badge = byId("evacuation-badge");
  const statusView = {
    standby: ["ГОТОВНОСТЬ", "normal"],
    ready: ["МАРШРУТ ГОТОВ", "normal"],
    caution: ["С ПОДТВЕРЖДЕНИЕМ", "warning"],
    blocked: ["МАРШРУТ ЗАКРЫТ", "critical"],
  }[plan.status] || [plan.status.toUpperCase(), "warning"];
  badge.textContent = statusView[0];
  badge.className = `status-badge ${statusView[1]}`;
  const panel = byId("evacuation-panel");
  panel.classList.remove("normal", "warning", "critical");
  panel.classList.add(statusView[1]);
  setText("evacuation-summary", plan.summary);
  setText("evacuation-total", String(plan.total_people));
  setText("evacuation-routed", String(plan.routed_people));
  setText("evacuation-unrouted", String(plan.unrouted_people));

  const routeList = byId("evacuation-routes");
  routeList.replaceChildren();
  if (!plan.routes.length) {
    const empty = document.createElement("p");
    empty.className = "empty-route";
    empty.textContent = "Активных маршрутов нет.";
    routeList.append(empty);
  }

  const layer = byId("evacuation-layer");
  layer.replaceChildren();
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  plan.routes.forEach((route, index) => {
    const card = document.createElement("article");
    card.className = `evacuation-route-card ${route.status}`;
    const heading = document.createElement("div");
    const title = document.createElement("strong");
    const status = document.createElement("span");
    title.textContent = `${zoneNames[route.from_zone]} · ${route.people_count} чел.`;
    status.textContent = route.status === "blocked" ? "НЕТ ВЫХОДА" :
      route.status === "caution" ? "ОСТОРОЖНО" : "БЕЗОПАСНЫЙ";
    heading.append(title, status);
    const path = document.createElement("p");
    path.textContent = route.destination
      ? route.path.map((zone) => zoneNames[zone]).join(" → ")
      : `${zoneNames[route.from_zone]} → ожидание спасательной группы`;
    const meta = document.createElement("small");
    meta.textContent = route.estimated_minutes === null
      ? route.instruction
      : `Оценка ${fmt(route.estimated_minutes, 1)} мин · ${route.instruction}`;
    card.append(heading, path, meta);
    routeList.append(card);

    const points = route.path.map((zone) => routePositions[zone]);
    const startPoint = points[0];
    if (route.status === "blocked") {
      const blocked = svgElement("g", { class: "evacuation-blocked", transform: `translate(${startPoint[0]} ${startPoint[1]})` });
      const ring = svgElement("circle", { r: 22 });
      const mark = svgElement("text", { "text-anchor": "middle", y: 6 });
      mark.textContent = "!";
      blocked.append(ring, mark);
      layer.append(blocked);
      return;
    }

    const pathData = points.map(([x, y], pointIndex) => `${pointIndex ? "L" : "M"}${x} ${y}`).join(" ");
    if (points.length > 1) {
      const routePath = svgElement("path", { class: `evacuation-map-path ${route.status}`, d: pathData });
      layer.append(routePath);
      const traveller = svgElement("circle", { class: `evacuation-traveller ${route.status}`, r: 7 });
      if (!reduceMotion) {
        const motion = svgElement("animateMotion", {
          path: pathData,
          dur: `${Math.max(4, route.estimated_minutes * 1.4)}s`,
          begin: `${index * 0.45}s`,
          repeatCount: "indefinite",
        });
        traveller.append(motion);
      } else {
        traveller.setAttribute("cx", String(points[points.length - 1][0]));
        traveller.setAttribute("cy", String(points[points.length - 1][1]));
      }
      layer.append(traveller);
    }
    const [destinationX, destinationY] = points[points.length - 1];
    const destination = svgElement("circle", {
      class: `evacuation-destination ${route.status}`,
      cx: destinationX,
      cy: destinationY,
      r: 15,
    });
    layer.append(destination);

    for (let pointIndex = 0; pointIndex < route.path.length - 1; pointIndex += 1) {
      const first = route.path[pointIndex];
      const second = route.path[pointIndex + 1];
      const edge = byId(`edge-${first}-${second}`) || byId(`edge-${second}-${first}`);
      if (edge) edge.classList.add("evacuation-route", route.status);
    }
  });
}

function zoneCountLabel(count) {
  const remainder10 = count % 10;
  const remainder100 = count % 100;
  const word = remainder10 === 1 && remainder100 !== 11 ? "зона" :
    remainder10 >= 2 && remainder10 <= 4 && (remainder100 < 12 || remainder100 > 14) ? "зоны" : "зон";
  return `${count} ${word}`;
}

function renderForecastRow(label, points) {
  const row = document.createElement("div");
  row.className = "forecast-row";
  const rowLabel = document.createElement("strong");
  rowLabel.textContent = label;
  row.append(rowLabel);
  points.forEach((point) => {
    const cell = document.createElement("div");
    cell.className = `forecast-point ${point.risk_level}`;
    cell.setAttribute("aria-label", `${point.minute} минут: ${riskLabels[point.risk_level]}, ${zoneCountLabel(point.affected_zones.length)}`);
    const dot = document.createElement("i");
    const minute = document.createElement("span");
    minute.textContent = `${point.minute}`;
    const zones = document.createElement("small");
    zones.textContent = String(point.affected_zones.length);
    cell.append(dot, minute, zones);
    row.append(cell);
  });
  return row;
}

function renderAdvisor(state) {
  const advisor = state.ventilation_advisor;
  const baseline = advisor.forecast.without_action;
  const advised = advisor.forecast.with_advice;
  const baselineFinal = baseline[baseline.length - 1];
  const advisedFinal = advised[advised.length - 1];
  const level = advisor.status === "stable" ? "normal" : advisedFinal.risk_level;
  const panel = byId("advisor-panel");
  panel.classList.remove("normal", "warning", "critical");
  panel.classList.add(level);
  const badge = byId("advisor-badge");
  badge.className = `status-badge ${level}`;
  badge.textContent = advisor.status === "stable" ? "СТАБИЛЬНО" : "ТРЕБУЕТ ДЕЙСТВИЙ";
  setText("advisor-mode", advisor.mode.title);
  setText("advisor-summary", advisor.summary);

  const comparisons = [
    ["forecast-baseline", "forecast-baseline-zones", baselineFinal],
    ["forecast-advised", "forecast-advised-zones", advisedFinal],
  ];
  comparisons.forEach(([riskId, zonesId, point]) => {
    const risk = byId(riskId);
    risk.className = `risk-${point.risk_level}`;
    risk.textContent = riskLabels[point.risk_level];
    setText(zonesId, zoneCountLabel(point.affected_zones.length));
  });

  const chart = byId("forecast-chart");
  chart.replaceChildren(renderForecastRow("Без действий", baseline), renderForecastRow("По совету", advised));

  const targets = byId("fan-targets");
  targets.replaceChildren();
  Object.entries(advisor.fan_targets_percent).forEach(([fanId, value]) => {
    const item = document.createElement("div");
    const name = document.createElement("span");
    const target = document.createElement("strong");
    name.textContent = fanNames[fanId] || fanId;
    target.textContent = `${value}%`;
    item.append(name, target);
    targets.append(item);
  });
  renderList(byId("advisor-action-list"), advisor.actions, "Продолжать штатный мониторинг.");
}

function renderEnergy(state) {
  const energy = state.fan_energy;
  const comparison = energy.comparison;
  const delta = comparison.power_delta_vs_current_kw;
  const badge = byId("energy-saving");
  badge.textContent = `−${fmt(comparison.savings_vs_full_percent, 1)}%`;
  badge.title = "Экономия мощности относительно работы всех вентиляторов на 100%";
  badge.className = `energy-badge${energy.safety_priority ? " safety" : ""}`;

  setText("energy-current-power", `${fmt(energy.current.power_kw, 1)} кВт`);
  setText("energy-current-cost", `${fmt(energy.current.cost_10m_kzt, 0)} ₸ / 10 мин`);
  setText("energy-advised-power", `${fmt(energy.recommended.power_kw, 1)} кВт`);
  setText("energy-advised-cost", `${fmt(energy.recommended.cost_10m_kzt, 0)} ₸ / 10 мин`);
  setText("energy-summary", energy.summary);
  setText(
    "energy-daily-saving",
    `${fmt(comparison.projected_daily_savings_kwh, 0)} кВт·ч · ${fmt(comparison.projected_daily_savings_kzt, 0)} ₸`,
  );

  const list = byId("energy-fans");
  list.replaceChildren();
  Object.entries(energy.fans).forEach(([fanId, item]) => {
    const row = document.createElement("div");
    const label = document.createElement("span");
    const bars = document.createElement("div");
    const current = document.createElement("i");
    const advised = document.createElement("i");
    const value = document.createElement("strong");
    label.textContent = fanNames[fanId] || fanId;
    bars.className = "energy-bars";
    current.className = "energy-bar current";
    advised.className = "energy-bar advised";
    current.style.width = `${Math.max(1, item.current_power_kw / item.rated_power_kw * 100)}%`;
    advised.style.width = `${Math.max(1, item.recommended_power_kw / item.rated_power_kw * 100)}%`;
    bars.append(current, advised);
    value.textContent = `${fmt(item.current_power_kw, 1)} → ${fmt(item.recommended_power_kw, 1)} кВт`;
    row.append(label, bars, value);
    list.append(row);
  });

  const panel = byId("energy-panel");
  panel.classList.toggle("safety-priority", energy.safety_priority);
  panel.setAttribute("data-power-direction", delta > 0 ? "increase" : delta < 0 ? "decrease" : "same");
}

function renderControls(state) {
  all(".scenario-button").forEach((button) => {
    button.disabled = busy;
    button.classList.toggle("active", button.dataset.scenario === state.scenario.name);
  });
  byId("reset-scenario").disabled = busy || state.scenario.name === "none";
}

function render(state) {
  currentState = state;
  renderKpis(state);
  renderZones(state);
  renderWorkers(state);
  renderEquipment(state);
  renderFans(state);
  renderIncident(state);
  renderEvacuation(state);
  renderAdvisor(state);
  renderEnergy(state);
  renderControls(state);
  const time = new Date(state.timestamp).toLocaleTimeString("ru-RU");
  setConnection(true, `Обновлено ${time} · каждые ${state.publish_interval_seconds} с`);
  setText("telemetry-sequence", `Кадр телеметрии: ${state.tick}`);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.message || `HTTP ${response.status}`);
  return body;
}

async function refreshState() {
  try {
    render(await api("/api/state"));
  } catch (error) {
    setConnection(false, error.message);
  }
}

async function runScenario(button) {
  if (busy) return;
  busy = true;
  if (currentState) renderControls(currentState);
  const scenario = button.dataset.scenario;
  const selectorId = button.dataset.zoneSelect;
  const zone = selectorId ? byId(selectorId).value : undefined;
  setText("control-status", "Сценарий запускается…");
  byId("control-status").className = "control-status";
  try {
    const state = await api(`/api/scenarios/${scenario}`, {
      method: "POST",
      body: JSON.stringify(zone ? { zone } : {}),
    });
    render(state);
    setText("control-status", `Активен: ${scenarioNames[scenario]}`);
    byId("control-status").className = "control-status success";
    showToast(`Сценарий «${scenarioNames[scenario]}» запущен`);
  } catch (error) {
    setText("control-status", `Ошибка: ${error.message}`);
    byId("control-status").className = "control-status error";
    showToast(`Не удалось запустить сценарий: ${error.message}`, true);
  } finally {
    busy = false;
    if (currentState) renderControls(currentState);
  }
}

async function resetScenario() {
  if (busy) return;
  busy = true;
  if (currentState) renderControls(currentState);
  setText("control-status", "Возврат в штатный режим…");
  try {
    const state = await api("/api/reset", { method: "POST", body: "{}" });
    render(state);
    setText("control-status", "Штатный режим восстановлен");
    byId("control-status").className = "control-status success";
    showToast("Сценарий сброшен, показатели вернулись в норму");
  } catch (error) {
    setText("control-status", `Ошибка: ${error.message}`);
    byId("control-status").className = "control-status error";
    showToast(`Не удалось выполнить сброс: ${error.message}`, true);
  } finally {
    busy = false;
    if (currentState) renderControls(currentState);
  }
}

all(".scenario-button").forEach((button) => button.addEventListener("click", () => runScenario(button)));
byId("reset-scenario").addEventListener("click", resetScenario);
refreshState();
setInterval(refreshState, POLL_INTERVAL_MS);
