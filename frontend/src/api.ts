// Typed client for the marsgis API. All geometry is in the region CRS (metres)
// unless a field name says lonlat (planetocentric, east-positive, Mars sphere).

export interface LayerStats {
  valid_percent: number;
  minimum: number | null;
  maximum: number | null;
  mean: number | null;
  stddev: number | null;
  p02: number | null;
  p98: number | null;
}

export interface LayerInfo {
  id: string;
  title: string;
  description: string;
  units: string;
  version: string;
  bounds_xy: [number, number, number, number];
  resolution_m: number;
  dtype: string;
  nodata: number | "nan" | null;
  statistics: LayerStats;
  render: { colormap: string | null; rescale: "auto" | [number, number] };
  footprint_lonlat: { type: "Polygon"; coordinates: number[][][] };
}

export interface RegionInfo {
  id: string;
  name: string;
  body: string;
  crs: {
    name: string;
    wkt2: string;
    proj4: string;
    geographic_proj4: string;
    conventions: Record<string, string>;
  };
  frame_xy: [number, number, number, number];
  center_lonlat: [number, number];
  base_resolution_m: number;
  tms: { id: string; base_zoom: number; max_zoom: number; tile_size: number };
  layers: LayerInfo[];
}

export interface LayerSample {
  units: string;
  status: "ok" | "nodata" | "outside";
  value: number | null;
  row: number;
  col: number;
  resolution_m: number;
}

export interface InspectResult {
  region: string;
  location: {
    x_m: number;
    y_m: number;
    lon_east_180: number;
    lon_east_360: number;
    lon_west_360: number;
    lat_planetocentric: number;
    lat_planetographic: number;
    crs: string;
  };
  layers: Record<string, LayerSample>;
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${await r.text()}`);
  return (await r.json()) as T;
}

export const api = {
  region: (id: string) => getJSON<RegionInfo>(`/api/regions/${encodeURIComponent(id)}`),
  inspect: (id: string, x: number, y: number) =>
    getJSON<InspectResult>(`/api/regions/${encodeURIComponent(id)}/inspect?x=${x}&y=${y}`),
  colormap: (name: string) =>
    getJSON<Record<string, [number, number, number, number]>>(
      `/api/colorMaps/${encodeURIComponent(name)}`,
    ),
  tileUrl(tmsId: string, region: string, layer: string, params: Record<string, string>): string {
    const q = new URLSearchParams({ region, layer, ...params });
    return `/api/cog/tiles/${tmsId}/{z}/{x}/{y}.png?${q.toString()}`;
  },
};
