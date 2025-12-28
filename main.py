#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API:
- POST /api/warnings -> Gibt DWD-Warnungen für ein bestimmtes GeoJSON-Gebiet zurück
- GET /healthz      -> Überprüft den Zustand des Dienstes
"""
"""
fl-dwd-warnings (WFS) – DWD Warnpolygone (fixer Layer)
- Web UI: AOI zeichnen (1 Feature) -> passende Warnungen (dwd:Warnungen_Gemeinden_vereinigt) laden
         -> Warnungen auf Karte + Hover/Infobox + GeoJSON anzeigen + Download
- API: POST /api/warnings  (GeoJSON -> GeoJSON FeatureCollection)

Quelle (DWD GeoServer / WFS):
- https://maps.dwd.de/geoserver/dwd/ows  (WFS 2.0.0)

Hinweis CRS:
- Frontend zeichnet in EPSG:4326 (Leaflet)
- WFS BBOX Filter: srsName=CRS:84 (lon,lat) -> gleiche Achsenreihenfolge (lon,lat)
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

# FIX: nur dieser Layer
DWD_TYPENAME = "dwd:Warnungen_Gemeinden_vereinigt"

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "25"))  # seconds
MAX_FEATURES = int(os.getenv("MAX_FEATURES", "800"))   # bewusst simpel: kein UI-Feld, optional per ENV

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

    # Leaflet liefert EPSG:4326 (lon,lat). Für WFS nutzen wir CRS:84 (lon,lat) => identisch.
    return (min(xs), min(ys), max(xs), max(ys))


def _pick(props: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in props and props.get(k) not in (None, ""):
            return props.get(k)
    return None


def _normalize_feature_properties(props: Dict[str, Any]) -> Dict[str, Any]:
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

    # ein paar häufig nützliche IDs, falls vorhanden
    for k in ["WARNCELLID", "warncellid", "ID", "id", "EC_II", "EC_GROUP", "EVENT", "STATUS", "MSGTYPE"]:
        if k in props and props.get(k) not in (None, ""):
            out[k] = props.get(k)

    return out


def _http_get(url: str, params: Dict[str, Any], accept: str) -> requests.Response:
    try:
        return requests.get(
            url,
            params=params,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": accept},
        )
    except Exception as e:
        raise RuntimeError(f"DWD Request fehlgeschlagen: {e}")


def _http_get_json(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    r = _http_get(url, params=params, accept="application/geo+json,application/json,*/*")
    ct = (r.headers.get("Content-Type") or "").lower()
    txt = r.text or ""

    if not r.ok:
        raise RuntimeError(f"DWD WFS Upstream HTTP {r.status_code}. Auszug: {txt[:900]}")

    try:
        js = r.json()
    except Exception as e:
        raise RuntimeError(f"DWD WFS JSON Parse Error: {e}. Content-Type={ct}. Auszug: {txt[:300]}")

    if not isinstance(js, dict):
        raise RuntimeError("DWD WFS lieferte kein JSON-Objekt.")

    return js


# -------------------------------
# In-memory cache (GetFeature)
# -------------------------------

@dataclass
class CacheEntry:
    ts: float
    data: Any


_feature_cache: Dict[str, CacheEntry] = {}


def _feature_cache_cleanup() -> None:
    try:
        now = _now_ts()
        dead = [k for k, v in _feature_cache.items() if (now - v.ts) > CACHE_TTL_SECONDS]
        for k in dead:
            _feature_cache.pop(k, None)

        if len(_feature_cache) > MAX_CACHE_ITEMS:
            items = sorted(_feature_cache.items(), key=lambda kv: kv[1].ts, reverse=True)
            keep = dict(items[:MAX_CACHE_ITEMS])
            _feature_cache.clear()
            _feature_cache.update(keep)
    except Exception:
        pass


def _feature_cache_key(bbox: Tuple[float, float, float, float], max_features: int) -> str:
    return f"{DWD_TYPENAME}|{bbox[0]:.6f},{bbox[1]:.6f},{bbox[2]:.6f},{bbox[3]:.6f}|{max_features}"


def _fetch_dwd_warnings_geojson(
    bbox: Tuple[float, float, float, float],
    max_features: int,
) -> Dict[str, Any]:
    _feature_cache_cleanup()

    key = _feature_cache_key(bbox, max_features)
    now = _now_ts()

    hit = _feature_cache.get(key)
    if hit and (now - hit.ts) <= CACHE_TTL_SECONDS:
        return hit.data  # type: ignore[return-value]

    minx, miny, maxx, maxy = bbox

    # WFS 2.0.0: "typeNames" ist korrekt (GeoServer akzeptiert meist auch "typeName")
    params = {
        "service": "WFS",
        "version": DWD_WFS_VERSION,
        "request": "GetFeature",
        "typeNames": DWD_TYPENAME,
        "outputFormat": "application/json",
        "srsName": "CRS:84",
        "bbox": f"{minx},{miny},{maxx},{maxy},CRS:84",
        "count": str(int(max_features)),
    }

    js = _http_get_json(DWD_WFS_BASE, params=params)

    if js.get("type") != "FeatureCollection":
        raise RuntimeError("DWD WFS lieferte keine GeoJSON FeatureCollection.")

    _feature_cache[key] = CacheEntry(ts=now, data=js)
    return js


def _build_featurecollection(
    raw_fc: Dict[str, Any],
    bbox: Tuple[float, float, float, float],
) -> Dict[str, Any]:
    feats = raw_fc.get("features") or []
    out_feats: List[Dict[str, Any]] = []
    summary: List[Dict[str, Any]] = []

    for f in feats:
        if not isinstance(f, dict) or f.get("type") != "Feature":
            continue

        props_raw = f.get("properties") or {}
        if not isinstance(props_raw, dict):
            props_raw = {}

        norm = _normalize_feature_properties(props_raw)

        # properties: norm + raw (damit UI "alle Informationen" anzeigen kann)
        out_props = dict(norm)
        out_props["properties_raw"] = props_raw

        out_feats.append({
            "type": "Feature",
            "geometry": f.get("geometry"),
            "properties": out_props,
        })

        summary.append({
            "kurztext": norm.get("kurztext"),
            "gueltig_ab_local": norm.get("gueltig_ab_local") or norm.get("gueltig_ab"),
            "gueltig_bis_local": norm.get("gueltig_bis_local") or norm.get("gueltig_bis"),
            "severity": norm.get("severity"),
            "gebiet": norm.get("gebiet"),
        })

    return {
        "type": "FeatureCollection",
        "features": out_feats,
        "meta": {
            "source": "DWD WFS",
            "endpoint": DWD_WFS_BASE,
            "typeName": DWD_TYPENAME,
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

        # akzeptiere: {geojson: ...} oder {aoi: ...} oder direkt GeoJSON
        payload = body.get("geojson") if "geojson" in body else (body.get("aoi") if "aoi" in body else body)
        gj = _parse_geojson(payload)

        feature = _extract_single_feature_geojson(gj)
        bbox = _geojson_feature_to_bbox_crs84(feature)

        # optional: API darf max übergeben (ohne UI), bleibt aber eingeschränkt
        max_n = int(body.get("max", MAX_FEATURES))
        max_n = max(1, min(max_n, 2000))

        raw_fc = _fetch_dwd_warnings_geojson(bbox=bbox, max_features=max_n)
        out_fc = _build_featurecollection(raw_fc, bbox=bbox)

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
      width: 100%; min-height: 160px; resize: vertical;
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
    .status{
      color: var(--muted); font-size: 13px; line-height: 1.35;
      padding: 8px 10px; border-radius: 12px; background: rgba(0,0,0,.18); border: 1px solid var(--border);
    }
    .status b{ color: var(--text); }
    .err{ border-color: rgba(255,100,100,.35); background: rgba(255,100,100,.10); color: #ffd1d1; }
    .ok{ border-color: rgba(120,220,160,.35); background: rgba(120,220,160,.08); }
    .small{ font-size: 12px; color: var(--muted); }
    .pill{ display:inline-block; padding: 2px 8px; border-radius: 999px; border:1px solid var(--border); color: var(--muted); font-size: 12px; }
    .mono{ font-family: var(--mono); }
    table{ width:100%; border-collapse: collapse; font-size: 12px; }
    th,td{ padding: 6px 6px; border-bottom: 1px solid rgba(255,255,255,.08); vertical-align: top; }
    th{ color: var(--muted); font-weight: 600; text-align:left; }
    .leaflet-control-attribution{ background:rgba(0,0,0,.45) !important; color:rgba(255,255,255,.75) !important; border-radius:10px !important; border:1px solid rgba(255,255,255,.12) !important;}
    .infobox{
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 10px;
      background: rgba(255,255,255,.03);
    }
    .kv{ display:grid; grid-template-columns: 140px 1fr; gap: 6px 10px; }
    .k{ color: var(--muted); }
    .v{ color: var(--text); overflow-wrap:anywhere; }
    .divider{ height: 1px; background: rgba(255,255,255,.10); margin: 8px 0; }
  </style>

  <script>
    const FIX_TYPENAME = {{ typename_json|safe }};
    const SERVICE_SLUG = {{ service_slug_json|safe }};
  </script>
</head>
<body>
  <header>
    <div>
      <h1>{{ title }}</h1>
      <div class="hint">
        Zeichne ein Polygon/Rechteck (immer nur <b>ein</b> Feature). Danach <b>Warnungen laden</b> (Layer fix: <span class="mono">{{ typename }}</span>).
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

        <div id="status" class="status">Noch keine AOI.</div>

        <div class="infobox" id="infoBox">
          <div style="display:flex; justify-content:space-between; gap:10px; align-items:baseline;">
            <div style="font-weight:700;">Info</div>
            <div class="small">Hover über eine Warnfläche</div>
          </div>
          <div class="divider"></div>
          <div class="small" id="infoHint">Noch keine Warnung geladen.</div>
          <div id="infoContent" style="display:none;"></div>
        </div>

        <label>AOI GeoJSON (EPSG:4326)</label>
        <textarea id="geojsonAoi" spellcheck="false" placeholder="Hier erscheint das AOI-GeoJSON…"></textarea>

        <label>Warnungen GeoJSON (letzter Abruf)</label>
        <textarea id="geojsonWarn" spellcheck="false" placeholder="Hier erscheint das Warnungen-GeoJSON…"></textarea>

        <div id="warnBox" style="display:none; margin-top:6px;">
          <div class="small">Treffer (<span id="warnCount">0</span>) – klick in Tabelle zoomt zur Warnung:</div>
          <div style="max-height: 240px; overflow:auto; margin-top:6px;">
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

        <div class="small">
          Layer ist fest verdrahtet: <span class="mono">{{ typename }}</span>.
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

    // ---- UI refs
    const elAoi = document.getElementById('geojsonAoi');
    const elWarn = document.getElementById('geojsonWarn');
    const elStatus = document.getElementById('status');

    const btnClear = document.getElementById('btn-clear');
    const btnLoad = document.getElementById('btn-load');
    const btnDlWarn = document.getElementById('btn-dl-warn');
    const btnDlAoi = document.getElementById('btn-dl-aoi');

    const warnBox = document.getElementById('warnBox');
    const warnCount = document.getElementById('warnCount');
    const warnTableBody = document.querySelector('#warnTable tbody');

    const infoBox = document.getElementById('infoBox');
    const infoHint = document.getElementById('infoHint');
    const infoContent = document.getElementById('infoContent');

    // ---- state
    let currentAOILayer = null;
    let lastWarningsFC = null;
    let featureIndex = []; // [{id, layer, props, bounds}...]

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

    function setButtons(){
      const hasAOI = !!currentAOILayer;
      btnLoad.disabled = !hasAOI;
      btnDlWarn.disabled = !lastWarningsFC;
      btnDlAoi.disabled = !hasAOI;
    }

    function featureToGeoJSON(layer){
      return { type:"Feature", properties:{ epsg: 4326 }, geometry: layer.toGeoJSON().geometry };
    }

    function clearWarningsUI(){
      warningsLayer.clearLayers();
      lastWarningsFC = null;
      featureIndex = [];
      elWarn.value = '';
      warnBox.style.display = 'none';
      warnCount.textContent = '0';
      warnTableBody.innerHTML = '';
      infoHint.textContent = 'Noch keine Warnung geladen.';
      infoContent.style.display = 'none';
      infoContent.innerHTML = '';
      setButtons();
    }

    function clearAll(){
      drawn.clearLayers();
      currentAOILayer = null;
      elAoi.value = '';
      clearWarningsUI();
      setStatus('Noch keine AOI.', '');
      setButtons();
    }

    btnClear.addEventListener('click', clearAll);

    // ---- warnings layer with hover -> right infobox
    function baseStyle(p){
      const sev = (p && p.severity ? String(p.severity) : '').toLowerCase();
      let weight = 2, opacity = 0.9, fillOpacity = 0.22;
      let color = '#6ea8fe';
      if(sev.includes('4') || sev.includes('extrem')) color = '#ff6b6b';
      else if(sev.includes('3') || sev.includes('unwetter')) color = '#ffb347';
      else if(sev.includes('2') || sev.includes('markant')) color = '#ffd166';
      else if(sev.includes('1')) color = '#54d18a';
      return { color, weight, opacity, fillOpacity };
    }

    const warningsLayer = L.geoJSON([], {
      style: (f) => baseStyle((f && f.properties) ? f.properties : {}),
      onEachFeature: (feature, layer) => {
        const p = feature.properties || {};
        const t = (p.kurztext || '(ohne Kurztext)').toString();
        const vb = (p.gueltig_bis_local || p.gueltig_bis || 'n/a').toString();
        const area = (p.gebiet || '').toString();
        const sev = (p.severity || '').toString();

        const popup = `
          <div style="font-family:system-ui; font-size:12px; line-height:1.35; max-width:320px;">
            <div style="font-weight:700; margin-bottom:4px;">${escapeHtml(t)}</div>
            <div style="opacity:.9;">Gültig bis: <b>${escapeHtml(vb)}</b></div>
            ${area ? `<div style="opacity:.8; margin-top:2px;">Gebiet: ${escapeHtml(area)}</div>` : ``}
            ${sev ? `<div style="opacity:.8; margin-top:2px;">Severity: ${escapeHtml(sev)}</div>` : ``}
          </div>
        `;
        layer.bindPopup(popup);

        layer.on('mouseover', () => {
          try{
            layer.setStyle({ weight: 4, fillOpacity: 0.30 });
            if(!L.Browser.ie && !L.Browser.opera && !L.Browser.edge) layer.bringToFront();
          }catch(_){}
          renderInfo(p);
        });

        layer.on('mouseout', () => {
          try{
            layer.setStyle(baseStyle(p));
          }catch(_){}
        });

        layer.on('click', () => renderInfo(p));
      }
    }).addTo(map);

    function renderInfo(p){
      if(!p){
        infoHint.textContent = 'Keine Informationen.';
        infoContent.style.display = 'none';
        infoContent.innerHTML = '';
        return;
      }
      infoHint.textContent = '';
      const raw = p.properties_raw || {};
      const keysRaw = Object.keys(raw || {}).sort((a,b)=>a.localeCompare(b));

      const head = `
        <div class="kv">
          <div class="k">Kurztext</div><div class="v"><b>${escapeHtml(p.kurztext || '(ohne)')}</b></div>
          <div class="k">Gültig ab</div><div class="v">${escapeHtml(p.gueltig_ab_local || p.gueltig_ab || 'n/a')}</div>
          <div class="k">Gültig bis</div><div class="v">${escapeHtml(p.gueltig_bis_local || p.gueltig_bis || 'n/a')}</div>
          <div class="k">Severity</div><div class="v">${escapeHtml(p.severity ?? '')}</div>
          <div class="k">Gebiet</div><div class="v">${escapeHtml(p.gebiet ?? '')}</div>
        </div>
        <div class="divider"></div>
        <div class="small" style="margin-bottom:6px;">Alle Attribute (raw):</div>
      `;

      let rawHtml = `<div class="kv">`;
      for(const k of keysRaw){
        const v = raw[k];
        rawHtml += `<div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(typeof v === 'string' ? v : JSON.stringify(v))}</div>`;
      }
      rawHtml += `</div>`;

      infoContent.innerHTML = head + rawHtml;
      infoContent.style.display = 'block';
    }

    // ---- draw control
    const drawControl = new L.Control.Draw({
      draw: { polyline:false, circle:false, circlemarker:false, marker:false,
              polygon:{ allowIntersection:false, showArea:true }, rectangle:true },
      edit: { featureGroup: drawn, edit:true, remove:true }
    });
    map.addControl(drawControl);

    map.on(L.Draw.Event.CREATED, function(e){
      drawn.clearLayers();
      currentAOILayer = e.layer;
      drawn.addLayer(currentAOILayer);

      const gj = featureToGeoJSON(currentAOILayer);
      elAoi.value = JSON.stringify(gj, null, 2);

      clearWarningsUI();

      try{
        const b = L.geoJSON(gj).getBounds();
        if(b.isValid()) map.fitBounds(b.pad(0.2));
      }catch(_){}

      setStatus('AOI gesetzt. Jetzt <b>Warnungen laden</b>.', 'ok');
      setButtons();
    });

    map.on('draw:edited', function(){
      const layers = drawn.getLayers();
      if(!layers.length) return;

      currentAOILayer = layers[0];
      const gj = featureToGeoJSON(currentAOILayer);
      elAoi.value = JSON.stringify(gj, null, 2);

      clearWarningsUI();
      setStatus('AOI geändert. Bitte <b>Warnungen laden</b> erneut ausführen.', 'ok');
      setButtons();
    });

    map.on('draw:deleted', function(){
      clearAll();
    });

    async function apiPostJson(url, body){
      const res = await fetch(url, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(body)
      });
      const raw = await res.text();
      let js = null;
      try { js = raw ? JSON.parse(raw) : {}; } catch(_){ js = null; }

      if(!res.ok){
        const msg = (js && js.error) ? js.error : (`HTTP ${res.status}. ${raw.slice(0,240)}`);
        throw new Error(msg);
      }
      if(js === null) throw new Error(`Server lieferte kein JSON (HTTP ${res.status}). Antwort-Auszug: ${raw.slice(0,240)}`);
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

      for(let i=0; i<rows.length; i++){
        const w = rows[i] || {};
        const tr = document.createElement('tr');
        tr.style.cursor = 'pointer';
        tr.innerHTML = `
          <td>${escapeHtml(w.kurztext || '(ohne Kurztext)')}</td>
          <td class="mono">${escapeHtml(w.gueltig_bis_local || 'n/a')}</td>
          <td>${escapeHtml(w.severity ?? '')}</td>
          <td>${escapeHtml(w.gebiet ?? '')}</td>
        `;
        tr.addEventListener('click', () => {
          // best-effort: zoom to the i-th feature
          try{
            const f = (lastWarningsFC && lastWarningsFC.features) ? lastWarningsFC.features[i] : null;
            if(f){
              const b = L.geoJSON(f).getBounds();
              if(b && b.isValid()) map.fitBounds(b.pad(0.15));
              renderInfo((f.properties || {}));
            }
          }catch(_){}
        });
        warnTableBody.appendChild(tr);
      }

      warnBox.style.display = 'block';
    }

    async function doLoad(){
      if(!currentAOILayer){
        setStatus('Bitte zuerst eine AOI zeichnen.', 'err');
        return;
      }

      let gj;
      try { gj = JSON.parse(elAoi.value); }
      catch(_){ setStatus('AOI GeoJSON ist ungültig.', 'err'); return; }

      setStatus(`Lade Warnungen vom DWD WFS… <span class="mono">${escapeHtml(FIX_TYPENAME)}</span>`, '');
      btnLoad.disabled = true;

      const data = await apiPostJson('/api/warnings', { geojson: gj });

      lastWarningsFC = data;
      elWarn.value = JSON.stringify(data, null, 2);

      warningsLayer.clearLayers();
      warningsLayer.addData(data);

      try{
        const b = warningsLayer.getBounds();
        if(b && b.isValid()) map.fitBounds(b.pad(0.15));
      }catch(_){}

      const count = (data.meta && typeof data.meta.count === 'number') ? data.meta.count : (data.features || []).length;
      fillWarningsTable((data.meta && data.meta.summary) ? data.meta.summary : []);

      if(count === 0){
        infoHint.textContent = 'Keine Warnungen im BBOX der AOI gefunden.';
        infoContent.style.display = 'none';
        infoContent.innerHTML = '';
        setStatus(`OK: <b>0</b> Treffer · ggf. AOI anpassen oder später erneut prüfen.`, 'ok');
      } else {
        infoHint.textContent = 'Hover über eine Warnfläche, um Details zu sehen.';
        setStatus(`OK: <b>${count}</b> Treffer · Layer=<span class="mono">${escapeHtml(FIX_TYPENAME)}</span>`, 'ok');
      }

      setButtons();
      btnLoad.disabled = false;
    }

    async function doDownloadWarnings(){
      if(!lastWarningsFC){
        setStatus('Noch keine Warnungen geladen.', 'err');
        return;
      }
      const blob = new Blob([JSON.stringify(lastWarningsFC, null, 2)], { type: 'application/geo+json' });
      const fn = `${SERVICE_SLUG}_${FIX_TYPENAME.replaceAll(':','_')}.geojson`;
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = fn;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setStatus('Warnungen-Download gestartet.', 'ok');
    }

    function doDownloadAOI(){
      if(!currentAOILayer){
        setStatus('Keine AOI vorhanden.', 'err');
        return;
      }
      const blob = new Blob([elAoi.value || ''], { type: 'application/geo+json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `${SERVICE_SLUG}_aoi_epsg4326.geojson`;
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
        service_slug_json=json.dumps(SERVICE_SLUG),
        typename=DWD_TYPENAME,
        typename_json=json.dumps(DWD_TYPENAME),
    )


# -------------------------------
# Entrypoint
# -------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
