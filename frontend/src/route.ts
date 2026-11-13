// Route planning panel: waypoint picking, request, route display and diagnostics.
// All computation happens on the server on numerical rasters; this module only
// visualises the structured result (geometry, totals, per-vertex profile, class runs).

import Feature from "ol/Feature";
import type OLMap from "ol/Map";
import { LineString, Point } from "ol/geom";
import VectorLayer from "ol/layer/Vector";
import VectorSource from "ol/source/Vector";
import { Circle, Fill, Stroke, Style, Text } from "ol/style";

import { api, ApiError, type Category, type RegionInfo, type RouteResult, type Waypoint } from "./api";

type PickMode = "start" | "goal" | null;

function el(id: string): HTMLElement {
  const e = document.getElementById(id);
  if (!e) throw new Error(`#${id} missing`);
  return e;
}

function hms(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return `${h} h ${String(m).padStart(2, "0")} min`;
}

const n1 = (v: number | undefined, unit = "") => (v === undefined ? "—" : `${v.toFixed(1)}${unit}`);
const n2 = (v: number | undefined, unit = "") => (v === undefined ? "—" : `${v.toFixed(2)}${unit}`);

export interface RoutePanel {
  /** returns true if the click was consumed by waypoint picking */
  handleClick(coord: number[]): boolean;
}

export function setupRoutePanel(map: OLMap, region: RegionInfo): RoutePanel {
  const vehicles = Object.keys(region.planning);
  const root = el("route");
  if (vehicles.length === 0) {
    root.innerHTML = `<p class="muted">No planning layers configured for this region.</p>`;
    return { handleClick: () => false };
  }
  const classLayer = region.layers.find((l) => l.id === region.planning[vehicles[0]].classes);
  const categories: Category[] = classLayer?.categories ?? [];
  const colorOf = (c: number) => categories.find((k) => k.value === c)?.color ?? "#ffffff";

  root.innerHTML = `
    <div class="row">
      <button id="pick-start" class="btn">Set start</button>
      <button id="pick-goal" class="btn">Set destination</button>
      <button id="route-clear" class="btn ghost">Clear</button>
    </div>
    <div class="row">
      <span class="muted">vehicle</span><select id="route-vehicle"></select>
      <span class="muted">engine</span>
      <select id="route-engine"><option value="dijkstra">dijkstra (SciPy)</option>
        <option value="mcp">mcp (scikit-image)</option></select>
    </div>
    <div class="row"><button id="route-plan" class="btn primary" disabled>Plan route</button>
      <span id="route-status" class="muted small"></span></div>
    <div id="route-result"></div>`;
  const vSel = el("route-vehicle") as HTMLSelectElement;
  for (const v of vehicles) vSel.add(new Option(v, v));
  const eSel = el("route-engine") as HTMLSelectElement;
  const planBtn = el("route-plan") as HTMLButtonElement;
  const status = el("route-status");

  const wpSource = new VectorSource();
  const routeSource = new VectorSource();
  const cursorSource = new VectorSource();
  const wpStyle = (label: string, color: string) =>
    new Style({
      image: new Circle({ radius: 7, fill: new Fill({ color }), stroke: new Stroke({ color: "#fff", width: 2 }) }),
      text: new Text({ text: label, offsetY: -16, fill: new Fill({ color: "#fff" }),
        stroke: new Stroke({ color: "#000", width: 3 }), font: "bold 12px system-ui" }),
    });
  map.addLayer(new VectorLayer({
    source: routeSource,
    style: (f) => [
      new Style({ stroke: new Stroke({ color: "rgba(0,0,0,.75)", width: 6 }) }),
      new Style({ stroke: new Stroke({ color: colorOf(f.get("class") as number), width: 3 }) }),
    ],
    zIndex: 10,
  }));
  map.addLayer(new VectorLayer({ source: wpSource, zIndex: 11 }));
  map.addLayer(new VectorLayer({
    source: cursorSource, zIndex: 12,
    style: new Style({ image: new Circle({ radius: 5, fill: new Fill({ color: "#fff" }),
      stroke: new Stroke({ color: "#000", width: 2 }) }) }),
  }));

  let mode: PickMode = null;
  const wps: { start?: Waypoint; goal?: Waypoint } = {};
  const setMode = (m: PickMode) => {
    mode = m;
    el("pick-start").classList.toggle("active", m === "start");
    el("pick-goal").classList.toggle("active", m === "goal");
    const target = map.getTargetElement();
    if (target) target.style.cursor = m ? "crosshair" : "";
    status.textContent = m ? `click the map to place the ${m === "start" ? "start" : "destination"}` : "";
  };
  const drawWaypoints = () => {
    wpSource.clear();
    if (wps.start) {
      const f = new Feature(new Point([wps.start.x, wps.start.y]));
      f.setStyle(wpStyle("A", "#2ca25f"));
      wpSource.addFeature(f);
    }
    if (wps.goal) {
      const f = new Feature(new Point([wps.goal.x, wps.goal.y]));
      f.setStyle(wpStyle("B", "#e4572e"));
      wpSource.addFeature(f);
    }
    planBtn.disabled = !(wps.start && wps.goal);
  };

  // buttons always select their mode (after placing A the panel auto-advances to B, so a
  // toggle would cancel picking when the user then presses "Set destination"); Esc cancels
  el("pick-start").onclick = () => setMode("start");
  el("pick-goal").onclick = () => setMode("goal");
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setMode(null);
  });
  el("route-clear").onclick = () => {
    delete wps.start;
    delete wps.goal;
    routeSource.clear();
    cursorSource.clear();
    el("route-result").replaceChildren();
    drawWaypoints();
    setMode(null);
  };

  planBtn.onclick = async () => {
    if (!wps.start || !wps.goal) return;
    planBtn.disabled = true;
    status.textContent = "planning…";
    routeSource.clear();
    el("route-result").replaceChildren();
    try {
      const res = await api.route(region.id, [wps.start, wps.goal], vSel.value, eSel.value);
      drawRoute(res);
      renderResult(res);
      status.textContent = `${res.compute_ms.toFixed(0)} ms on the server`;
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      el("route-result").innerHTML = `<p class="error">${msg}</p>`;
      status.textContent = "";
    } finally {
      planBtn.disabled = false;
    }
  };

  function drawRoute(res: RouteResult): void {
    // one feature per class run so the line is coloured by traversability class
    const coords = res.geometry_xy.coordinates;
    const cls = res.profile.class;
    let start = 0;
    for (let i = 1; i <= coords.length; i++) {
      if (i === coords.length || cls[i] !== cls[start]) {
        const seg = coords.slice(start, Math.min(i + 1, coords.length));
        if (seg.length >= 2) {
          const f = new Feature(new LineString(seg));
          f.set("class", cls[start]);
          routeSource.addFeature(f);
        }
        start = i;
      }
    }
  }

  function renderResult(res: RouteResult): void {
    const s = res.summary;
    const byClass = Object.entries(s.length_by_class_m)
      .map(([k, v]) => {
        const cat = categories.find((c) => c.label === k);
        return `<span class="chip"><i style="background:${cat?.color ?? "#fff"}"></i>${k} ${v.toFixed(0)} m</span>`;
      })
      .join(" ");
    const warn = res.warnings.map((w) => `<p class="warn">⚠ ${w}</p>`).join("");
    const box = el("route-result");
    box.innerHTML = `
      <table class="values">
        <tr><td>Distance (planimetric)</td><td class="num">${n1(s.distance_m, " m")}</td></tr>
        <tr><td>Distance (surface)</td><td class="num">${n1(s.surface_distance_m, " m")}</td></tr>
        <tr><td>Straight line</td><td class="num">${n1(s.straight_line_m, " m")}</td></tr>
        <tr><td>Est. drive time <span class="muted">(model)</span></td><td class="num">${hms(s.estimated_drive_time_s)}</td></tr>
        <tr><td>Objective <span class="muted">(risk-weighted s)</span></td><td class="num">${s.objective_cost_s.toFixed(0)}</td></tr>
        <tr><td>Climb / descent</td><td class="num">${n1(s.elevation_gain_m)} / ${n1(s.elevation_loss_m, " m")}</td></tr>
        <tr><td>Potential energy of climbs</td><td class="num">${n1(s.potential_energy_climb_wh, " Wh")}</td></tr>
        <tr><td>Max footprint tilt</td><td class="num">${n1(s.tilt_max, "°")}</td></tr>
        <tr><td>Max footprint roughness</td><td class="num">${n2(s.roughness_max, " m")}</td></tr>
        <tr><td>Corner cuts</td><td class="num">${s.corner_cuts}</td></tr>
      </table>
      <div class="chips">${byClass}</div>
      ${warn}
      <canvas id="profile" width="600" height="260"></canvas>
      <p id="profile-readout" class="muted small">Hover the profile to locate a point on the route.</p>
      <details class="small muted"><summary>Inputs, engine and notes</summary>
        <p>engine ${res.engine}, ${res.resolution_m} m grid, ${res.legs.map((l) => l.cells_settled).join("+")} cells settled ·
          vehicle ${res.vehicle.name} (${res.vehicle.status})</p>
        <ul>${Object.entries(res.inputs).map(([k, v]) => `<li>${k}: ${v.layer} v${v.version}</li>`).join("")}</ul>
        <ul>${res.notes.map((n) => `<li>${n}</li>`).join("")}</ul>
      </details>`;
    drawProfile(el("profile") as HTMLCanvasElement, res);
  }

  function drawProfile(canvas: HTMLCanvasElement, res: RouteResult): void {
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const W = canvas.width;
    const H = canvas.height;
    const pad = { l: 46, r: 40, t: 14, b: 34 };
    const d = res.profile.distance_m;
    const z = res.profile.elevation as (number | null)[] | undefined;
    const tilt = res.profile.tilt as (number | null)[] | undefined;
    const total = d[d.length - 1] || 1;
    const X = (v: number) => pad.l + ((W - pad.l - pad.r) * v) / total;
    const plotH = H - pad.t - pad.b;

    const zs = (z ?? []).filter((v): v is number => v !== null);
    const zmin = Math.min(...zs);
    const zmax = Math.max(...zs);
    const zr = zmax - zmin || 1;
    const Yz = (v: number) => pad.t + plotH * (1 - (v - zmin) / zr);
    const tmax = 25;
    const Yt = (v: number) => pad.t + plotH * (1 - Math.min(v, tmax) / tmax);

    const draw = (hover?: number) => {
      ctx.clearRect(0, 0, W, H);
      ctx.font = "11px system-ui";
      // class strip
      for (let i = 0; i < d.length - 1; i++) {
        ctx.fillStyle = colorOf(res.profile.class[i]);
        ctx.fillRect(X(d[i]), H - pad.b + 6, Math.max(1, X(d[i + 1]) - X(d[i])), 6);
      }
      // axes
      ctx.strokeStyle = "#3a3f48";
      ctx.strokeRect(pad.l, pad.t, W - pad.l - pad.r, plotH);
      ctx.fillStyle = "#9aa0a6";
      ctx.fillText(`${zmax.toFixed(0)} m`, 2, pad.t + 8);
      ctx.fillText(`${zmin.toFixed(0)} m`, 2, H - pad.b);
      ctx.fillText(`${tmax}°`, W - pad.r + 4, pad.t + 8);
      ctx.fillText("0°", W - pad.r + 4, H - pad.b);
      ctx.fillText("0", pad.l, H - 6);
      ctx.fillText(`${total.toFixed(0)} m`, W - pad.r - 40, H - 6);
      ctx.fillText("elevation", pad.l + 4, pad.t + 12);
      ctx.fillStyle = "#f4a259";
      ctx.fillText("footprint tilt", pad.l + 64, pad.t + 12);
      // tilt limit line
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = "rgba(165,0,38,.9)";
      ctx.beginPath();
      ctx.moveTo(pad.l, Yt(20));
      ctx.lineTo(W - pad.r, Yt(20));
      ctx.stroke();
      ctx.setLineDash([]);
      const line = (vals: (number | null)[] | undefined, Y: (v: number) => number, color: string) => {
        if (!vals) return;
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        let pen = false;
        vals.forEach((v, i) => {
          if (v === null) { pen = false; return; }
          if (pen) ctx.lineTo(X(d[i]), Y(v));
          else ctx.moveTo(X(d[i]), Y(v));
          pen = true;
        });
        ctx.stroke();
      };
      line(tilt, Yt, "#f4a259");
      line(z, Yz, "#e8e6e3");
      if (hover !== undefined) {
        ctx.strokeStyle = "#fff";
        ctx.beginPath();
        ctx.moveTo(X(d[hover]), pad.t);
        ctx.lineTo(X(d[hover]), H - pad.b);
        ctx.stroke();
      }
    };
    draw();

    canvas.onmousemove = (e) => {
      const rect = canvas.getBoundingClientRect();
      const px = ((e.clientX - rect.left) / rect.width) * W;
      const target = ((px - pad.l) / (W - pad.l - pad.r)) * total;
      let i = d.findIndex((v) => v >= target);
      if (i < 0) i = d.length - 1;
      draw(i);
      const [x, y] = res.geometry_xy.coordinates[i];
      cursorSource.clear();
      cursorSource.addFeature(new Feature(new Point([x, y])));
      const p = res.profile;
      const cat = categories.find((c) => c.value === p.class[i]);
      el("profile-readout").textContent =
        `${d[i].toFixed(0)} m · z ${(p.elevation?.[i] as number | null)?.toFixed(1) ?? "—"} m · ` +
        `tilt ${(p.tilt?.[i] as number | null)?.toFixed(1) ?? "—"}° · ` +
        `roughness ${(p.roughness?.[i] as number | null)?.toFixed(2) ?? "—"} m · ` +
        `${cat?.label ?? p.class[i]} · t ${hms(p.cumulative_time_s[i])}`;
    };
    canvas.onmouseleave = () => {
      draw();
      cursorSource.clear();
    };
  }

  return {
    handleClick(coord: number[]): boolean {
      if (!mode) return false;
      const wp = { x: coord[0], y: coord[1] };
      if (mode === "start") wps.start = wp;
      else wps.goal = wp;
      drawWaypoints();
      setMode(mode === "start" && !wps.goal ? "goal" : null);
      return true;
    },
  };
}
