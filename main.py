#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fl-dwd-warnings (WFS) – DWD Warnpolygone + Kurztext + gültig bis
- Web UI: AOI zeichnen (1 Feature), Warnungen laden, AOI/Warnungen als GeoJSON downloaden
- API: POST /api/warnings  (GeoJSON -> Warnungen als GeoJSON FeatureCollection)

Quelle (DWD GeoServer / WFS):
- https://maps.dwd.de/geoserver/dwd/ows  (WFS 2.0.0)

Hinweis CRS:
- Frontend zeichnet in EPSG:4326 (Leaflet)
- WFS BBOX Filter: srsName=CRS:84 (lon,lat)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from flask import Flask, Response, jsonify, render_template_string, request

try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:
    ZoneInfo = None  # type: ignore


# -------------------------------
# Config (Cloud Run friendly)
# -------------------------------

APP_TITLE = os.getenv("APP_TITLE", "fl-dwd-warnings (WFS) – DWD Warnpolygone")
SERVICE_SLUG = os.getenv("SERVICE_SLUG", "fl-dwd-warnings")

DWD_WFS_BASE = os.getenv("DWD_WFS_BASE", "https://maps.dwd.de/geoserver/dwd/ows")
DWD_WFS_VERSION = os.getenv("DWD_WFS_VERSION", "2.0.0")
DWD_TYPENAME_DEFAULT = os.getenv("DWD_TYPENAME_DEFAULT", "dwd:Warnungen_Gemeinden_vereinigt")

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "25"))  # seconds
MAX_FEATURES_DEFAULT = int(os.getenv("MAX_FEATURES_DEFAULT", "500"))

# in-memory cache
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "20"))
MAX_CACHE_ITEMS = int(os.getenv("MAX_CACHE_ITEMS", "200"))

LOCAL_TZ = os.getenv("LOCAL_TZ", "Europe/Berlin")
USER_AGENT = os.getenv("USER_AGENT", f"{SERVICE_SLUG}/1.0 (+https://data-tales.dev/)")


# -------------------------------
# Flask
# -------------------------------

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["JSON_AS_ASCII"] = False


@app.after_request
def _add_headers(resp: Response) -> Response:
    # API usability: allow cross-origin reads
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# -------------------------------
# Helpers
# -------------------------------

def _now_ts() -> float:
    return time.time()


def _format_dt_local(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if ZoneInfo is None:
        return dt.isoformat()
    try:
        return dt.astimezone(ZoneInfo(LOCAL_TZ)).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return dt.isoformat()


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    if not isinstance(value, str):
        return None

    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_geojson(payload: Any) -> Dict[str, Any]:
    if payload is None:
        raise ValueError("Kein GeoJSON übergeben.")
    if isinstance(payload, str):
        payload = payload.strip()
        if not payload:
            raise ValueError("Leerer GeoJSON-String.")
        return json.loads(payload)
    if isinstance(payload, dict):
        return payload
    raise ValueError("GeoJSON muss ein JSON-Objekt oder String sein.")


def _extract_single_feature_geojson(gj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Returns a GeoJSON Feature (type=Feature) containing exactly one geometry.
    Accepts: Feature, FeatureCollection(1), Polygon, MultiPolygon.
    """
    t = gj.get("type")
    if t == "Feature":
        geom = gj.get("geometry")
        if not geom:
            raise ValueError("Feature ohne geometry.")
        return {"type": "Feature", "properties": (gj.get("properties") or {}), "geometry": geom}

    if t == "FeatureCollection":
        feats = gj.get("features") or []
        if len(feats) != 1:
            raise ValueError("FeatureCollection muss genau 1 Feature enthalten.")
        f = feats[0]
        if not isinstance(f, dict) or f.get("type") != "Feature":
            raise ValueError("FeatureCollection enthält kein gültiges Feature.")
        geom = f.get("geometry")
        if not geom:
            raise ValueError("Feature ohne geometry.")
        return {"type": "Feature", "properties": (f.get("properties") or {}), "geometry": geom}

    if t in ("Polygon", "MultiPolygon"):
        return {"type": "Feature", "properties": {}, "geometry": gj}

    raise ValueError("Nicht unterstützter GeoJSON-Typ. Erlaubt: Feature, FeatureCollection(1), Polygon, MultiPolygon.")


def _iter_coords_from_geom(geom: Dict[str, Any]) -> Iterable[Tuple[float, float]]:
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if not gtype or coords is None:
        return

    def walk(obj: Any):
        if isinstance(obj, (list, tuple)):
            if len(obj) == 2 and all(isinstance(v, (int, float)) for v in obj):
                yield float(obj[0]), float(obj[1])
            else:
                for it in obj:
                    yield from walk(it)

    yield from walk(coords)


def _geojson_feature_to_bbox_crs84(feature: Dict[str, Any]) -> Tuple[float, float, float, float]:
    geom = feature.get("geometry")
    if not isinstance(geom, dict) or "type" not in geom:
        raise ValueError("Ungültiges Feature: keine Geometry.")
    if geom.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("AOI muss Polygon oder MultiPolygon sein.")

    xs: List[float] = []
    ys: List[float] = []
    for x, y in _iter_coords_from_geom(geom):
        xs.append(x)
        ys.append(y)

    if not xs or not ys:
        raise ValueError("AOI enthält keine Koordinaten.")

    # Leaflet liefert EPSG:4326 (lon,lat). Für WFS nutzen wir CRS:84 (lon,lat) => identische Achsenreihenfolge.
    return (min(xs), min(ys), max(xs), max(ys))


def _pick(props: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in props and props.get(k) not in (None, ""):
            return props.get(k)
    return None


def _normalize_feature_properties(props: Dict[str, Any]) -> Dict[str, Any]:
    # Defensive: Attribute variieren je nach Layer/Version.
    headline = _pick(props, ["HEADLINE", "headline", "Headline", "EVENT", "event", "DESCRIPTION", "description"])
    expires_raw = _pick(props, ["EXPIRES", "expires", "Expires", "GUELTIG_BIS", "gueltig_bis", "VALID_UNTIL", "valid_until"])
    onset_raw = _pick(props, ["ONSET", "onset", "Onset", "EFFECTIVE", "effective"])
    severity = _pick(props, ["SEVERITY", "severity", "WARNSTUFE", "warnstufe", "LEVEL", "level"])
    area = _pick(props, ["AREADESC", "areadesc", "NAME", "name"])

    expires_dt = _parse_iso_dt(expires_raw)
    onset_dt = _parse_iso_dt(onset_raw)

    out = {
        "kurztext": headline,
        "gueltig_bis": expires_raw,
        "gueltig_bis_local": _format_dt_local(expires_dt),
        "gueltig_ab": onset_raw,
        "gueltig_ab_local": _format_dt_local(onset_dt),
        "severity": severity,
        "gebiet": area,
    }

    # keep some ids if present
    for k in ["WARNCELLID", "warncellid", "ID", "id", "EC_II", "EC_GROUP", "EVENT", "STATUS", "MSGTYPE"]:
        if k in props and props.get(k) not in (None, ""):
            out[k] = props.get(k)

    return out


def _http_get_json(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        r = requests.get(
            url,
            params=params,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json,application/geo+json,*/*"},
        )
    except Exception as e:
        raise RuntimeError(f"DWD WFS Request fehlgeschlagen: {e}")

    ct = (r.headers.get("Content-Type") or "").lower()
    txt = r.text or ""

    if not r.ok:
        # GeoServer liefert teils text/xml Fehler. Wir geben kurz aus.
        raise RuntimeError(f"DWD WFS Upstream HTTP {r.status_code}. Auszug: {txt[:900]}")

    # Try JSON regardless of content-type (GeoServer ist manchmal inkonsistent).
    try:
        js = r.json()
    except Exception as e:
        raise RuntimeError(f"DWD WFS JSON Parse Error: {e}. Content-Type={ct}. Auszug: {txt[:300]}")

    if not isinstance(js, dict):
        raise RuntimeError("DWD WFS lieferte kein JSON-Objekt.")

    return js


# -------------------------------
# In-memory cache
# -------------------------------

@dataclass
class CacheEntry:
    ts: float
    data: Dict[str, Any]


_cache: Dict[str, CacheEntry] = {}


def _cache_cleanup() -> None:
    try:
        now = _now_ts()
        # TTL cleanup
        dead = [k for k, v in _cache.items() if (now - v.ts) > CACHE_TTL_SECONDS]
        for k in dead:
            _cache.pop(k, None)

        # size cap (drop oldest)
        if len(_cache) > MAX_CACHE_ITEMS:
            items = sorted(_cache.items(), key=lambda kv: kv[1].ts, reverse=True)
            keep = dict(items[:MAX_CACHE_ITEMS])
            _cache.clear()
            _cache.update(keep)
    except Exception:
        pass


def _cache_key(typename: str, bbox: Tuple[float, float, float, float], max_features: int) -> str:
    return f"{typename}|{bbox[0]:.6f},{bbox[1]:.6f},{bbox[2]:.6f},{bbox[3]:.6f}|{max_features}"


def _fetch_dwd_warnings_geojson(
    typename: str,
    bbox: Tuple[float, float, float, float],
    max_features: int,
) -> Dict[str, Any]:
    _cache_cleanup()

    key = _cache_key(typename, bbox, max_features)
    now = _now_ts()

    hit = _cache.get(key)
    if hit and (now - hit.ts) <= CACHE_TTL_SECONDS:
        return hit.data

    minx, miny, maxx, maxy = bbox

    params = {
        "service": "WFS",
        "version": DWD_WFS_VERSION,
        "request": "GetFeature",
        "typeName": typename,
        "outputFormat": "application/json",
        "srsName": "CRS:84",
        "bbox": f"{minx},{miny},{maxx},{maxy},CRS:84",
        "count": str(int(max_features)),
    }

    js = _http_get_json(DWD_WFS_BASE, params=params)

    if js.get("type") != "FeatureCollection":
        raise RuntimeError("DWD WFS lieferte keine GeoJSON FeatureCollection.")

    _cache[key] = CacheEntry(ts=now, data=js)
    return js


def _build_featurecollection(
    raw_fc: Dict[str, Any],
    typename: str,
    bbox: Tuple[float, float, float, float],
    include_raw_props: bool,
) -> Dict[str, Any]:
    feats = raw_fc.get("features") or []
    out_feats: List[Dict[str, Any]] = []
    summary: List[Dict[str, Any]] = []

    for f in feats:
        if not isinstance(f, dict) or f.get("type") != "Feature":
            continue

        props = f.get("properties") or {}
        if not isinstance(props, dict):
            props = {}

        norm = _normalize_feature_properties(props)
        if include_raw_props:
            norm["properties_raw"] = props

        out_feats.append({
            "type": "Feature",
            "geometry": f.get("geometry"),
            "properties": norm,
        })

        summary.append({
            "kurztext": norm.get("kurztext"),
            "gueltig_bis": norm.get("gueltig_bis"),
            "gueltig_bis_local": norm.get("gueltig_bis_local"),
            "severity": norm.get("severity"),
            "gebiet": norm.get("gebiet"),
        })

    return {
        "type": "FeatureCollection",
        "features": out_feats,
        "meta": {
            "source": "DWD WFS",
            "endpoint": DWD_WFS_BASE,
            "typeName": typename,
            "bbox": list(bbox),
            "count": len(out_feats),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
        },
    }


# -------------------------------
# Routes / API
# -------------------------------

@app.post("/api/warnings")
def api_warnings():
    try:
        body = request.get_json(force=True, silent=False) or {}

        # Backward compatible: accept "aoi" or "geojson"
        gj = _parse_geojson(body.get("geojson") if body.get("geojson") is not None else body.get("aoi"))

        typename = (body.get("typeName") or body.get("typename") or DWD_TYPENAME_DEFAULT).strip()
        max_n = int(body.get("max", body.get("count", MAX_FEATURES_DEFAULT)))
        max_n = max(1, min(max_n, 2000))

        include_raw = bool(body.get("raw", False))

        feature = _extract_single_feature_geojson(gj)
        bbox = _geojson_feature_to_bbox_crs84(feature)

        raw_fc = _fetch_dwd_warnings_geojson(typename=typename, bbox=bbox, max_features=max_n)
        out_fc = _build_featurecollection(raw_fc, typename=typename, bbox=bbox, include_raw_props=include_raw)

        return jsonify(out_fc)

    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.get("/healthz")
def healthz():
    return Response("ok", mimetype="text/plain")


# -------------------------------
# Web UI
# -------------------------------

INDEX_HTML = r"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{{ title }}</title>

  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css" />

  <style>
    :root{
      --bg:#0b0f19;
      --card:#111a2e;
      --text:#e6eaf2;
      --muted:#a8b3cf;
      --border: rgba(255,255,255,.10);
      --primary:#6ea8fe;
      --focus: rgba(110,168,254,.45);
      --radius: 16px;
      --container: 1200px;
      --gap: 14px;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      --font: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
    }
    body{ margin:0; font-family: var(--font); background: var(--bg); color: var(--text); }
    .wrap{ max-width: var(--container); margin: 18px auto; padding: 0 14px 24px; display: grid; grid-template-columns: 1.2fr .8fr; gap: var(--gap); }
    header{ max-width: var(--container); margin: 18px auto 0; padding: 0 14px; display:flex; align-items:baseline; justify-content:space-between; gap: 12px; }
    h1{ font-size: 18px; margin:0; letter-spacing: .2px; }
    .hint{ color: var(--muted); font-size: 13px; margin-top: 6px; line-height: 1.35; }
    .card{ background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: 0 18px 60px rgba(0,0,0,.35); overflow: hidden; }
    #map{ height: 70vh; min-height: 520px; }
    .panel{ padding: 12px; display:flex; flex-direction:column; gap: 10px; }
    label{ color: var(--muted); font-size: 12px; }
    textarea{
      width: 100%; min-height: 180px; resize: vertical;
      background: rgba(255,255,255,.04); border: 1px solid var(--border); border-radius: 12px;
      padding: 10px; color: var(--text); font-family: var(--mono); font-size: 12px; outline: none;
    }
    textarea:focus{ border-color: var(--primary); box-shadow: 0 0 0 4px var(--focus); }
    .row{ display:flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    button{
      appearance:none; border: 1px solid var(--border); background: rgba(255,255,255,.06);
      color: var(--text); padding: 10px 12px; border-radius: 12px; cursor: pointer; font-weight: 600;
    }
    button.primary{ border-color: rgba(110,168,254,.35); background: rgba(110,168,254,.16); }
    button:disabled{ opacity:.55; cursor:not-allowed; }
    select,input{
      background: rgba(255,255,255,.04); border: 1px solid var(--border); border-radius: 10px;
      padding: 8px 10px; color: var(--text);
    }
    .status{
      color: var(--muted); font-size: 13px; line-height: 1.35;
      padding: 8px 10px; border-radius: 12px; background: rgba(0,0,0,.18); border: 1px solid var(--border);
    }
    .status b{ color: var(--text); }
    .err{ border-color: rgba(255,100,100,.35); background: rgba(255,100,100,.10); color: #ffd1d1; }
    .ok{ border-color: rgba(120,220,160,.35); background: rgba(120,220,160,.08); }
    .small{ font-size: 12px; color: var(--muted); }
    table{ width:100%; border-collapse: collapse; font-size: 12px; }
    th,td{ padding: 6px 6px; border-bottom: 1px solid rgba(255,255,255,.08); vertical-align: top; }
    th{ color: var(--muted); font-weight: 600; text-align:left; }
    .mono{ font-family: var(--mono); }
    .pill{ display:inline-block; padding: 2px 8px; border-radius: 999px; border:1px solid var(--border); color: var(--muted); font-size: 12px; }
    .leaflet-control-attribution{ background:rgba(0,0,0,.45) !important; color:rgba(255,255,255,.75) !important; border-radius:10px !important; border:1px solid rgba(255,255,255,.12) !important;}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>{{ title }}</h1>
      <div class="hint">
        Zeichne ein Polygon/Rechteck (immer nur <b>ein</b> Feature). Lade Warnungen via WFS und exportiere als GeoJSON.
        <span class="pill">AOI: EPSG:4326</span> <span class="pill">WFS: CRS:84</span>
      </div>
    </div>
    <div class="small">API: <code>/api/warnings</code> · Health: <code>/healthz</code></div>
  </header>

  <div class="wrap">
    <div class="card"><div id="map"></div></div>

    <div class="card">
      <div class="panel">
        <div class="row">
          <button id="btn-clear">AOI löschen</button>
          <button class="primary" id="btn-load" disabled>Warnungen laden</button>
          <button id="btn-dl-warn" disabled>Warnungen downloaden</button>
          <button id="btn-dl-aoi" disabled>AOI downloaden</button>
        </div>

        <div class="row">
          <label>typeName:
            <input id="typeName" value="{{ typename_default }}" style="width: 320px;">
          </label>
          <label>Max:
            <input id="maxN" type="number" min="1" max="2000" value="{{ max_default }}" style="width: 120px;">
          </label>
        </div>

        <div id="status" class="status">Noch keine AOI.</div>

        <label>GeoJSON (aktuelles Feature, EPSG:4326)</label>
        <textarea id="geojson" spellcheck="false" placeholder="Hier erscheint das GeoJSON…"></textarea>

        <div class="small">
          Beispiele typeName:
          <span class="pill">dwd:Warnungen_Gemeinden_vereinigt</span>
          <span class="pill">dwd:Warnungen_Gemeinden</span>
          <span class="pill">dwd:Warnungen_Landkreise</span>
        </div>

        <div id="warnBox" style="display:none; margin-top:10px;">
          <div class="small">Warnungen (<span id="warnCount">0</span>):</div>
          <div style="max-height: 320px; overflow:auto; margin-top:6px;">
            <table id="warnTable">
              <thead>
                <tr>
                  <th>Kurztext</th><th>gültig bis</th><th>Severity</th><th>Gebiet</th>
                </tr>
              </thead>
              <tbody></tbody>
            </table>
          </div>
        </div>

      </div>
    </div>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js"></script>

  <script>
    const map = L.map('map', { preferCanvas:true }).setView([51.2, 10.3], 6);

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 20,
      attribution: '&copy; OpenStreetMap'
    }).addTo(map);

    const drawn = new L.FeatureGroup().addTo(map);

    const warningsLayer = L.geoJSON([], {
      style: (f) => {
        const p = (f && f.properties) ? f.properties : {};
        const sev = (p.severity || '').toString().toLowerCase();
        let weight = 2, opacity = 0.9, fillOpacity = 0.22;
        let color = '#6ea8fe';
        if(sev.includes('4') || sev.includes('extrem')) color = '#ff6b6b';
        else if(sev.includes('3') || sev.includes('unwetter')) color = '#ffb347';
        else if(sev.includes('2') || sev.includes('markant')) color = '#ffd166';
        else if(sev.includes('1')) color = '#54d18a';
        return { color, weight, opacity, fillOpacity };
      },
      onEachFeature: (feature, layer) => {
        const p = feature.properties || {};
        const t = (p.kurztext || '(ohne Kurztext)').toString();
        const vb = (p.gueltig_bis_local || p.gueltig_bis || 'n/a').toString();
        const area = (p.gebiet || '').toString();
        const sev = (p.severity || '').toString();

        const html = `
          <div style="font-family:system-ui; font-size:12px; line-height:1.35; max-width:300px;">
            <div style="font-weight:700; margin-bottom:4px;">${escapeHtml(t)}</div>
            <div style="opacity:.9;">Gültig bis: <b>${escapeHtml(vb)}</b></div>
            ${area ? `<div style="opacity:.8; margin-top:2px;">Gebiet: ${escapeHtml(area)}</div>` : ``}
            ${sev ? `<div style="opacity:.8; margin-top:2px;">Severity: ${escapeHtml(sev)}</div>` : ``}
          </div see
        `;
        layer.bindPopup(html);
      }
    }).addTo(map);

    const drawControl = new L.Control.Draw({
      draw: { polyline:false, circle:false, circlemarker:false, marker:false, polygon:{ allowIntersection:false, showArea:true }, rectangle:true },
      edit: { featureGroup: drawn, edit:true, remove:true }
    });
    map.addControl(drawControl);

    const elGeo = document.getElementById('geojson');
    const elStatus = document.getElementById('status');

    const btnClear = document.getElementById('btn-clear');
    const btnLoad = document.getElementById('btn-load');
    const btnDlWarn = document.getElementById('btn-dl-warn');
    const btnDlAoi = document.getElementById('btn-dl-aoi');

    const elTypeName = document.getElementById('typeName');
    const elMaxN = document.getElementById('maxN');

    const warnBox = document.getElementById('warnBox');
    const warnCount = document.getElementById('warnCount');
    const warnTableBody = document.querySelector('#warnTable tbody');

    let currentFeature = null;

    function setStatus(html, cls){
      elStatus.className = 'status' + (cls ? (' ' + cls) : '');
      elStatus.innerHTML = html;
    }

    function escapeHtml(s){
      return String(s)
        .replaceAll('&','&amp;')
        .replaceAll('<','&lt;')
        .replaceAll('>','&gt;')
        .replaceAll('"','&quot;')
        .replaceAll("'","&#039;");
    }

    function featureToGeoJSON(layer){
      return { type:"Feature", properties:{ epsg: 4326 }, geometry: layer.toGeoJSON().geometry };
    }

    function setButtons(){
      const hasFeature = !!currentFeature;
      btnLoad.disabled = !hasFeature;
      btnDlWarn.disabled = !hasFeature;
      btnDlAoi.disabled = !hasFeature;
    }

    function clearAll(){
      drawn.clearLayers();
      warningsLayer.clearLayers();

      currentFeature = null;
      elGeo.value = '';

      warnBox.style.display = 'none';
      warnCount.textContent = '0';
      warnTableBody.innerHTML = '';

      setButtons();
      setStatus('Noch keine AOI.', '');
    }

    map.on(L.Draw.Event.CREATED, function(e){
      drawn.clearLayers();
      warningsLayer.clearLayers();

      warnBox.style.display = 'none';
      warnCount.textContent = '0';
      warnTableBody.innerHTML = '';

      const layer = e.layer;
      drawn.addLayer(layer);
      currentFeature = layer;

      const gj = featureToGeoJSON(layer);
      elGeo.value = JSON.stringify(gj, null, 2);

      try{
        const b = L.geoJSON(gj).getBounds();
        if(b.isValid()) map.fitBounds(b.pad(0.2));
      }catch(_){}

      setButtons();
      setStatus('AOI gesetzt. Jetzt <b>Warnungen laden</b>.', 'ok');
    });

    map.on('draw:edited', function(){
      const layers = drawn.getLayers();
      if(!layers.length) return;

      currentFeature = layers[0];
      const gj = featureToGeoJSON(currentFeature);
      elGeo.value = JSON.stringify(gj, null, 2);

      warningsLayer.clearLayers();
      warnBox.style.display = 'none';
      warnCount.textContent = '0';
      warnTableBody.innerHTML = '';

      setButtons();
      setStatus('AOI geändert. Note: bitte <b>Warnungen laden</b> erneut ausführen.', 'ok');
    });

    map.on('draw:deleted', function(){
      clearAll();
    });

    btnClear.addEventListener('click', clearAll);

    async function apiJson(url, body){
      const res = await fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
      const ct = (res.headers.get('content-type') || '').toLowerCase();
      const raw = await res.text();

      if(!ct.includes('application/json') && !ct.includes('json')){
        throw new Error(`Server lieferte kein JSON (HTTP ${res.status}, Content-Type=${ct}). Antwort-Auszug: ${raw.slice(0,240)}`);
      }

      const js = raw ? JSON.parse(raw) : {};
      if(!res.ok){
        throw new Error(js && js.error ? js.error : (`HTTP ${res.status}`));
      }
      return js;
    }

    function fillWarningsTable(summary){
      warnTableBody.innerHTML = '';
      const rows = summary || [];
      warnCount.textContent = String(rows.length || 0);

      if(!rows.length){
        warnBox.style.display = 'none';
        return;
      }

      for(const w of rows){
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${escapeHtml(w.kurztext || '(ohne Kurztext)')}</td>
          <td class="mono">${escapeHtml(w.gueltig_bis_local || w.gueltig_bis || 'n/a')}</td>
          <td>${escapeHtml(w.severity ?? '')}</td>
          <td>${escapeHtml(w.gebiet ?? '')}</td>
        `;
        warnTableBody.appendChild(tr);
      }

      warnBox.style.display = 'block';
    }

    async function doLoad(){
      if(!currentFeature){
        setStatus('Bitte zuerst eine AOI zeichnen.', 'err');
        return;
      }

      let gj;
      try{ gj = JSON.parse(elGeo.value); }
      catch(_){ setStatus('GeoJSON ist ungültig.', 'err'); return; }

      const typeName = (elTypeName.value || '').trim() || '{{ typename_default }}';
      const maxN = Math.max(1, Math.min(2000, parseInt(elMaxN.value || '500', 10) || 500));

      setButtons();
      setStatus('Lade Warnungen vom DWD WFS…', '');

      const data = await apiJson('/api/warnings', { geojson: gj, typeName, max: maxN });

      warningsLayer.clearLayers();
      warningsLayer.addData(data);

      // fit to warnings if any, else fit to AOI
      try{
        const b = warningsLayer.getBounds();
        if(b && b.isValid()) map.fitBounds(b.pad(0.15));
      }catch(_){}

      const count = (data.meta && typeof data.meta.count === 'number') ? data.meta.count : (data.features || []).length;
      fillWarningsTable((data.meta && data.meta.summary) ? data.meta.summary : []);

      setStatus(`OK: <b>${count}</b> Warnungen · typeName=<span class="mono">${escapeHtml(typeName)}</span>`, 'ok');
    }

    async function doDownloadWarnings(){
      if(!currentFeature){
        setStatus('Bitte zuerst eine AOI zeichnen.', 'err');
        return;
      }

      let gj;
      try{ gj = JSON.parse(elGeo.value); }
      catch(_){ setStatus('GeoJSON ist ungültig.', 'err'); return; }

      const typeName = (elTypeName.value || '').trim() || '{{ typename_default }}';
      const maxN = Math.max(1, Math.min(2000, parseInt(elMaxN.value || '500', 10) || 500));

      setStatus('Erstelle Download…', '');

      const res = await fetch('/api/warnings', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ geojson: gj, typeName, max: maxN })
      });

      const ct = (res.headers.get('content-type') || '').toLowerCase();
      const blob = await res.blob();

      if(!res.ok){
        let msg = `HTTP ${res.status}`;
        try{
          if(ct.includes('json')){
            const txt = await blob.text();
            const js = JSON.parse(txt);
            msg = js && js.error ? js.error : msg;
          }
        }catch(_){}
        setStatus('Fehler: ' + escapeHtml(msg), 'err');
        return;
      }

      const fn = `${'{{ service_slug }}'}_${typeName.replaceAll(':','_')}.geojson`;
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = fn;
      document.body.appendChild(a);
      a.click();
      a.remove();

      setStatus('Download gestartet.', 'ok');
    }

    function doDownloadAOI(){
      if(!currentFeature){
        setStatus('Keine AOI vorhanden.', 'err');
        return;
      }
      const blob = new Blob([elGeo.value || ''], { type: 'application/geo+json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = '{{ service_slug }}_aoi_epsg4326.geojson';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setStatus('AOI Download gestartet.', 'ok');
    }

    btnLoad.addEventListener('click', () => doLoad().catch(e => setStatus('Fehler: ' + escapeHtml(e.message), 'err')));
    btnDlWarn.addEventListener('click', () => doDownloadWarnings().catch(e => setStatus('Fehler: ' + escapeHtml(e.message), 'err')));
    btnDlAoi.addEventListener('click', () => doDownloadAOI());

    clearAll();
  </script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(
        INDEX_HTML,
        title=APP_TITLE,
        service_slug=SERVICE_SLUG,
        typename_default=DWD_TYPENAME_DEFAULT,
        max_default=MAX_FEATURES_DEFAULT,
    )


# -------------------------------
# Entrypoint
# -------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
