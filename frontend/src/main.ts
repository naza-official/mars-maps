import "ol/ol.css";
import "./style.css";

import Feature from "ol/Feature";
import Map from "ol/Map";
import View from "ol/View";
import { ScaleLine, defaults as defaultControls } from "ol/control";
import { Point, Polygon } from "ol/geom";
import TileLayer from "ol/layer/Tile";
import VectorLayer from "ol/layer/Vector";
import { Projection, transform } from "ol/proj";
import { register } from "ol/proj/proj4";
import XYZ from "ol/source/XYZ";
import VectorSource from "ol/source/Vector";
import { Circle, Fill, Stroke, Style } from "ol/style";
import TileGrid from "ol/tilegrid/TileGrid";
import proj4 from "proj4";

import { api, type InspectResult, type LayerInfo, type RegionInfo } from "./api";

const REGION_ID = new URLSearchParams(location.search).get("region") ?? "jezero";
const COLORMAPS = ["gist_earth", "terrain", "viridis", "magma", "cividis", "rdylgn_r", "gray"];

function $(id: string): HTMLElement {
  const el = document.getElementById(id);
  if (!el) throw new Error(`#${id} missing`);
  return el;
}

/**
 * Rewrite a PROJ string so proj4js (verified 2.22.0) evaluates it correctly.
 *
 * Two proj4js defects affect spherical Transverse Mercator (+proj=tmerc +R=...):
 *  1. The default exact algorithm ("etmerc") throws "Incorrect elliptical usage" on a
 *     sphere. `+approx` selects Snyder's tmerc, whose spherical branch is exact.
 *  2. That spherical inverse flips the latitude sign whenever the projected y < 0,
 *     instead of testing the sign of (lat_0 + y/R). With lat_0 != 0, every point south
 *     of the projection centre gets a mirrored latitude (found by the click-time
 *     cross-check against PROJ: y=-20 km returned 18.11°S instead of 18.11°N).
 *     Spherical TM with lat_0=φ0 is identical to lat_0=0 with y_0 -= R·φ0, and with
 *     lat_0=0 the faulty sign test coincides with the correct one.
 * The canonical CRS (backend, COGs) is unchanged; this only affects browser transforms.
 */
function proj4jsCompatible(def: string): string {
  const num = (key: string) => {
    const m = def.match(new RegExp(`\\+${key}=([-+0-9.eE]+)`));
    return m ? Number(m[1]) : 0;
  };
  if (!/\+proj=tmerc\b/.test(def) || !/\+R=/.test(def)) return def;
  const R = num("R");
  const lat0 = num("lat_0");
  const y0 = num("y_0") - (R * lat0 * Math.PI) / 180;
  return def
    .replace("+proj=tmerc", "+proj=tmerc +approx")
    .replace(/\+lat_0=[-+0-9.eE]+/, "+lat_0=0")
    .replace(/\+y_0=[-+0-9.eE]+/, `+y_0=${y0}`);
}

/**
 * Register the region CRS and the Mars geographic CRS with OpenLayers.
 *
 * Mars pitfall: OpenLayers derives map scale (ScaleLine, getPointResolution) by
 * transforming to EPSG:4326 and measuring on the *Earth* sphere. That would make every
 * distance ~1.9x too large on Mars. We override getPointResolution: the region CRS is
 * Transverse Mercator with k0 = 1, so 1 projected metre = 1 ground metre (< 0.03 % error
 * across the frame, asserted in backend tests). Nothing here ever uses EPSG:4326.
 */
function setupProjections(region: RegionInfo): { view: Projection; geo: string } {
  const viewCode = `MARSGIS:${region.id}`;
  const geoCode = "MARSGIS:IAU_2015_49900"; // Mars sphere lon/lat, planetocentric, east+
  proj4.defs(viewCode, proj4jsCompatible(region.crs.proj4));
  proj4.defs(geoCode, region.crs.geographic_proj4);
  register(proj4);
  const view = new Projection({
    code: viewCode,
    units: "m",
    extent: region.frame_xy,
    getPointResolution: (resolution: number) => resolution,
  });
  return { view, geo: geoCode };
}

function tileGrid(region: RegionInfo): TileGrid {
  const [minx, , , maxy] = region.frame_xy;
  const z0 = (region.frame_xy[2] - minx) / region.tms.tile_size;
  const resolutions = Array.from({ length: region.tms.max_zoom + 1 }, (_, z) => z0 / 2 ** z);
  // OGC TMS "topLeft" corner of origin == OpenLayers origin at the top-left, y down.
  return new TileGrid({
    extent: region.frame_xy,
    origin: [minx, maxy],
    resolutions,
    tileSize: region.tms.tile_size,
  });
}

interface LayerState {
  info: LayerInfo;
  layer: TileLayer<XYZ>;
  colormap: string | null;
  rescale: [number, number];
}

function defaultRescale(info: LayerInfo): [number, number] {
  if (info.render.rescale !== "auto") return info.render.rescale;
  const s = info.statistics;
  const lo = s.p02 ?? s.minimum ?? 0;
  const hi = s.p98 ?? s.maximum ?? 1;
  const digits = Math.abs(hi - lo) < 10 ? 3 : 0;
  const f = 10 ** digits;
  return [Math.floor(lo * f) / f, Math.ceil(hi * f) / f];
}

function makeSource(region: RegionInfo, grid: TileGrid, proj: Projection, st: LayerState): XYZ {
  const params: Record<string, string> = { rescale: st.rescale.join(",") };
  if (st.colormap && st.colormap !== "gray") params.colormap_name = st.colormap;
  return new XYZ({
    url: api.tileUrl(region.tms.id, region.id, st.info.id, params),
    projection: proj,
    tileGrid: grid,
    interpolate: false, // show analysis cells as-is when over-zoomed
    crossOrigin: "anonymous",
  });
}

async function drawLegend(canvas: HTMLCanvasElement, colormap: string | null): Promise<void> {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const img = ctx.createImageData(256, 1);
  if (colormap && colormap !== "gray") {
    const cmap = await api.colormap(colormap);
    for (let i = 0; i < 256; i++) {
      const [r, g, b] = cmap[String(i)] ?? [0, 0, 0, 0];
      img.data.set([r, g, b, 255], i * 4);
    }
  } else {
    for (let i = 0; i < 256; i++) img.data.set([i, i, i, 255], i * 4);
  }
  ctx.putImageData(img, 0, 0);
}

function fmt(v: number | null, units: string): string {
  if (v === null) return "—";
  const a = Math.abs(v);
  const s = a >= 100 ? v.toFixed(1) : a >= 1 ? v.toFixed(2) : v.toFixed(4);
  return units === "1" ? s : `${s} ${units}`;
}

function renderLayerPanel(
  states: LayerState[],
  rebuild: (st: LayerState) => void,
): void {
  const root = $("layers");
  root.replaceChildren();
  // top-most layer first in the panel
  for (const st of [...states].reverse()) {
    const { info } = st;
    const el = document.createElement("div");
    el.className = "layer";
    el.innerHTML = `
      <label class="row"><input type="checkbox" class="vis">
        <strong>${info.title}</strong><span class="muted">${info.units === "1" ? "" : info.units}</span></label>
      <div class="row"><span class="muted">opacity</span><input type="range" class="op" min="0" max="1" step="0.05"></div>
      <div class="row"><select class="cmap"></select>
        <input type="number" class="lo" step="any"><input type="number" class="hi" step="any"></div>
      <canvas class="legend" width="256" height="1"></canvas>
      <div class="row legend-labels"><span class="lo-l"></span><span class="hi-l"></span></div>
      <p class="muted small">${info.description} · ${info.resolution_m} m · v${info.version} ·
        ${info.statistics.valid_percent.toFixed(1)}% valid</p>`;
    const vis = el.querySelector<HTMLInputElement>(".vis")!;
    const op = el.querySelector<HTMLInputElement>(".op")!;
    const cmap = el.querySelector<HTMLSelectElement>(".cmap")!;
    const lo = el.querySelector<HTMLInputElement>(".lo")!;
    const hi = el.querySelector<HTMLInputElement>(".hi")!;
    const legend = el.querySelector<HTMLCanvasElement>(".legend")!;
    const labels = () => {
      el.querySelector(".lo-l")!.textContent = fmt(st.rescale[0], info.units);
      el.querySelector(".hi-l")!.textContent = fmt(st.rescale[1], info.units);
    };
    vis.checked = st.layer.getVisible();
    op.value = String(st.layer.getOpacity());
    for (const name of COLORMAPS) cmap.add(new Option(name, name));
    cmap.value = st.colormap ?? "gray";
    lo.value = String(st.rescale[0]);
    hi.value = String(st.rescale[1]);
    labels();
    void drawLegend(legend, st.colormap);

    vis.onchange = () => st.layer.setVisible(vis.checked);
    op.oninput = () => st.layer.setOpacity(Number(op.value));
    cmap.onchange = () => {
      st.colormap = cmap.value;
      void drawLegend(legend, st.colormap);
      rebuild(st);
    };
    const onRange = () => {
      const a = Number(lo.value);
      const b = Number(hi.value);
      if (Number.isFinite(a) && Number.isFinite(b) && b > a) {
        st.rescale = [a, b];
        labels();
        rebuild(st);
      }
    };
    lo.onchange = onRange;
    hi.onchange = onRange;
    root.append(el);
  }
}

function renderInspect(res: InspectResult, region: RegionInfo): void {
  const L = res.location;
  const rows = region.layers
    .map((l) => {
      const s = res.layers[l.id];
      if (!s) return "";
      const val = s.status === "ok" ? fmt(s.value, s.units) : `<em>${s.status}</em>`;
      return `<tr><td>${l.title}</td><td class="num">${val}</td></tr>`;
    })
    .join("");
  const any = Object.values(res.layers)[0];
  $("inspect").innerHTML = `
    <table>
      <tr><td>Lat (planetocentric)</td><td class="num">${L.lat_planetocentric.toFixed(5)}°</td></tr>
      <tr><td>Lat (planetographic)</td><td class="num">${L.lat_planetographic.toFixed(5)}°</td></tr>
      <tr><td>Lon (east, ±180)</td><td class="num">${L.lon_east_180.toFixed(5)}°E</td></tr>
      <tr><td>Lon (west, 0–360)</td><td class="num">${L.lon_west_360.toFixed(5)}°W</td></tr>
      <tr><td>x / y (region TM)</td><td class="num">${L.x_m.toFixed(1)} / ${L.y_m.toFixed(1)} m</td></tr>
      ${any ? `<tr><td>grid cell (row, col)</td><td class="num">${any.row}, ${any.col}</td></tr>` : ""}
    </table>
    <table class="values">${rows}</table>`;
}

async function main(): Promise<void> {
  const region = await api.region(REGION_ID);
  $("region-name").textContent = region.name;
  $("crs-name").textContent = region.crs.name;

  const { view: proj, geo } = setupProjections(region);
  const grid = tileGrid(region);

  // Bottom -> top draw order and initial visibility.
  const order = ["orthoimage", "elevation", "hillshade", "slope"];
  const initial: Record<string, { visible: boolean; opacity: number }> = {
    orthoimage: { visible: false, opacity: 1 },
    elevation: { visible: true, opacity: 1 },
    hillshade: { visible: true, opacity: 0.45 },
    slope: { visible: false, opacity: 0.8 },
  };
  const rank = (id: string) => (order.includes(id) ? order.indexOf(id) : order.length);
  const infos = [...region.layers].sort((a, b) => rank(a.id) - rank(b.id));
  const states: LayerState[] = infos.map((info) => {
    const st: LayerState = {
      info,
      // extent = layer bounds: no tile requests outside the data (TiTiler answers 404 there)
      layer: new TileLayer<XYZ>({ extent: info.bounds_xy }),
      colormap: info.render.colormap,
      rescale: defaultRescale(info),
    };
    const init = initial[info.id] ?? { visible: false, opacity: 1 };
    st.layer.setVisible(init.visible);
    st.layer.setOpacity(init.opacity);
    st.layer.setSource(makeSource(region, grid, proj, st));
    return st;
  });

  // Vector overlays: data footprints (from STAC geometry, lon/lat -> region CRS) and the
  // inspection marker. Future: routes and cost-surface contours go here.
  const footprints = new VectorSource();
  const seen = new Set<string>();
  for (const l of region.layers) {
    const key = JSON.stringify(l.bounds_xy);
    if (seen.has(key)) continue;
    seen.add(key);
    const ring = l.footprint_lonlat.coordinates[0].map((c) => transform(c, geo, proj));
    footprints.addFeature(new Feature(new Polygon([ring])));
  }
  const marker = new VectorSource();
  const overlays = [
    new VectorLayer({
      source: footprints,
      style: new Style({ stroke: new Stroke({ color: "#ffb347", width: 1.5, lineDash: [6, 4] }) }),
    }),
    new VectorLayer({
      source: marker,
      style: new Style({
        image: new Circle({
          radius: 6,
          stroke: new Stroke({ color: "#fff", width: 2 }),
          fill: new Fill({ color: "#e4572e" }),
        }),
      }),
    }),
  ];

  const elevation = region.layers.find((l) => l.id === "elevation") ?? region.layers[0];
  const [x0, y0, x1, y1] = elevation.bounds_xy;
  const map = new Map({
    target: "map",
    layers: [...states.map((s) => s.layer), ...overlays],
    controls: defaultControls().extend([new ScaleLine({ units: "metric" })]),
    view: new View({
      projection: proj,
      resolutions: grid.getResolutions(),
      extent: region.frame_xy,
      constrainOnlyCenter: true,
    }),
  });
  map.getView().fit([x0, y0, x1, y1], { padding: [20, 20, 20, 20] });

  renderLayerPanel(states, (st) => st.layer.setSource(makeSource(region, grid, proj, st)));

  map.on("pointermove", (e) => {
    const [x, y] = e.coordinate;
    const [lon, lat] = transform([x, y], proj, geo);
    $("cursor").textContent =
      `${lat.toFixed(4)}°N  ${lon.toFixed(4)}°E (planetocentric)  ·  x ${x.toFixed(0)} m, y ${y.toFixed(0)} m`;
  });

  map.on("singleclick", async (e) => {
    const [x, y] = e.coordinate;
    marker.clear();
    marker.addFeature(new Feature(new Point([x, y])));
    $("inspect").textContent = "Loading…";
    try {
      const res = await api.inspect(region.id, x, y);
      renderInspect(res, region);
      // Cross-check browser (proj4js) vs server (PROJ) coordinate transforms.
      const [lon, lat] = transform([x, y], proj, geo);
      const R = 3396190;
      const rad = Math.PI / 180;
      const dLat = (lat - res.location.lat_planetocentric) * rad * R;
      const dLon = (lon - res.location.lon_east_180) * rad * R * Math.cos(lat * rad);
      const err = Math.hypot(dLat, dLon);
      const note = document.createElement("p");
      note.className = "muted small";
      note.dataset.crsCheck = String(err);
      note.textContent = `Browser vs server CRS transform agreement: ${err.toExponential(1)} m`;
      if (err > 0.01) {
        note.style.color = "var(--accent)";
        console.warn("proj4js and PROJ disagree", { err, lon, lat, server: res.location });
      }
      $("inspect").append(note);
    } catch (err) {
      $("inspect").textContent = `Inspection failed: ${String(err)}`;
    }
  });

  // Expose for debugging / automated checks.
  (window as unknown as { marsgis: unknown }).marsgis = {
    map,
    region,
    states,
    toLonLat: (c: number[]) => transform(c, proj, geo),
  };
}

main().catch((err) => {
  $("region-name").textContent = `Failed to load: ${String(err)}`;
  console.error(err);
});
