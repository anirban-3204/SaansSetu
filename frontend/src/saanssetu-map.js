/*! SaansSetu route map.  One file, no build step.
 *
 *   <div id="map" style="height:520px"></div>
 *   <script src="saanssetu-map.js"></script>
 *   <script>
 *     SaansSetuMap.fromApi(document.getElementById('map'), { api: 'https://<your-api>', town: 'asansol' });
 *   </script>
 *
 * Or, when the dashboard already holds the two documents:
 *
 *     const view = await SaansSetuMap.mount(el, { config, run, lang: 'en' });
 *     view.setRun(nextRun);     // a newer run, or another frame of a replay
 *     view.setSlot('07:45');    // move the time slider
 *     view.destroy();
 *
 * config = GET /towns/{id}/config      run = GET /towns/{id}/run/latest (or a replay frame)
 *
 * Leaflet 1.9 is loaded from unpkg when the page has none. With a bundler, pass it in:
 *     import L from 'leaflet'; import 'leaflet/dist/leaflet.css';
 *     SaansSetuMap.mount(el, { L, config, run });
 */
(function (root) {
  'use strict';

  var LEAFLET = 'https://unpkg.com/leaflet@1.9.4/dist/';
  var TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
  var TILE_CREDIT = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

  // India AQI band colours, darkened a little so yellow and light green stay visible on a pale map.
  var BANDS = {
    good:         { en: 'Good',         hi: 'अच्छी',      color: '#2e9d48' },
    satisfactory: { en: 'Satisfactory', hi: 'संतोषजनक',   color: '#8fc31f' },
    moderate:     { en: 'Moderate',     hi: 'मध्यम',      color: '#f2c200' },
    poor:         { en: 'Poor',         hi: 'खराब',       color: '#f28c00' },
    very_poor:    { en: 'Very Poor',    hi: 'बहुत खराब',  color: '#e02d2d' },
    severe:       { en: 'Severe',       hi: 'गंभीर',      color: '#7e0023' }
  };
  var ORDER = ['good', 'satisfactory', 'moderate', 'poor', 'very_poor', 'severe'];
  var NO_DATA = '#8a8f98';
  var SUGGESTED = '#1456c8';

  var TEXT = {
    en: {
      home: 'Home area', school: 'School', station: 'Station', plant: 'Industrial area',
      approx: 'approximate location', usual: 'usual', suggested: 'suggested', windFrom: 'Wind from', noWind: 'No wind reading',
      calm: 'Still air', estimate: 'Street values are estimates from the station forecast, not measurements.',
      replay: 'Replay of', live: 'Live', issued: 'forecast made at', at: 'Air on the way to school at',
      play: 'Play the morning', pause: 'Pause', leave: 'leave', less: 'lower estimated dose than the usual',
      noRoutes: 'No routes are mapped for this town. The station forecast is shown.',
      noAdvice: 'No route advice in this run', station24: 'City badge (24 h)', latest: 'Last hour',
      downwind: 'Downwind of the industrial area right now. Direction only: an assumption in the estimate, not a measurement.',
      fromPlant: 'm from the industrial area', sample: 'SAMPLE DATA', morning: 'This morning’s call',
      sent: 'Alert sent', noAlert: 'No alert needed', held: 'Alert held', pm: 'PM2.5'
    },
    hi: {
      home: 'घर का इलाका', school: 'स्कूल', station: 'स्टेशन', plant: 'औद्योगिक क्षेत्र',
      approx: 'अनुमानित स्थान', usual: 'रोज़ का', suggested: 'सुझाया गया', windFrom: 'हवा की दिशा', noWind: 'हवा का डेटा नहीं',
      calm: 'हवा शांत', estimate: 'सड़क के आँकड़े स्टेशन के पूर्वानुमान से निकाले अनुमान हैं, माप नहीं।',
      replay: 'रीप्ले:', live: 'लाइव', issued: 'पूर्वानुमान का समय', at: 'स्कूल के रास्ते की हवा, समय',
      play: 'सुबह चलाएँ', pause: 'रोकें', leave: 'निकलें', less: 'कम अनुमानित डोज़, रोज़ के मुकाबले',
      noRoutes: 'इस शहर के रास्ते अभी दर्ज नहीं हैं। स्टेशन का पूर्वानुमान दिखाया गया है।',
      noAdvice: 'इस रन में रास्ते की सलाह नहीं है', station24: 'शहर का बैज (24 घंटे)', latest: 'पिछला घंटा',
      downwind: 'अभी औद्योगिक क्षेत्र से हवा इस ओर जा रही है। सिर्फ़ दिशा: यह अनुमान की एक मान्यता है, माप नहीं।',
      fromPlant: 'मी औद्योगिक क्षेत्र से', sample: 'नमूना डेटा', morning: 'आज सुबह का फ़ैसला',
      sent: 'अलर्ट भेजा गया', noAlert: 'अलर्ट की ज़रूरत नहीं', held: 'अलर्ट रोका गया', pm: 'PM2.5'
    }
  };

  var CSS = [
    '.ssm{position:relative;height:100%;min-height:380px;isolation:isolate;font:14px/1.35 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans","Noto Sans Devanagari",sans-serif;color:#14181f;border-radius:14px;overflow:hidden;background:#e9edf2}',
    '.ssm *{box-sizing:border-box}',
    '.ssm-map{position:absolute;inset:0;z-index:0;background:#e9edf2}',
    '.ssm .leaflet-top.leaflet-right{top:64px}',
    '.ssm-dark .ssm-map .leaflet-tile-pane{filter:invert(1) hue-rotate(180deg) brightness(.92) contrast(.9) saturate(.6)}',
    '.ssm-layer{position:absolute;z-index:1100;pointer-events:none;display:flex;gap:8px}',
    '.ssm-layer>*{pointer-events:auto}',
    '.ssm-top{top:10px;left:10px;right:10px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap}',
    '.ssm-bottom{left:10px;right:10px;bottom:22px;flex-direction:column;align-items:stretch}',
    '.ssm-wide .ssm-bottom{right:auto;width:372px}',
    '.ssm-wide .ssm-route{flex:1 1 100%}',
    '.ssm-wide .ssm-scrub label{display:none}',
    '.ssm-card{background:rgba(255,255,255,.94);border-radius:12px;box-shadow:0 2px 10px rgba(15,23,42,.18);padding:8px 12px;backdrop-filter:blur(6px)}',
    '.ssm-dark .ssm-card{background:rgba(22,27,34,.92);color:#eef2f7}',
    '.ssm-source{font-weight:600;max-width:calc(100% - 150px)}',
    '.ssm-source small{display:block;font-weight:400;opacity:.75}',
    '.ssm-tag{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:1px 7px;border-radius:999px;background:#14181f;color:#fff;margin-right:6px;vertical-align:1px}',
    '.ssm-tag.ssm-sample{background:#b42318}',
    '.ssm-wind{display:flex;align-items:center;gap:8px;white-space:nowrap}',
    '.ssm-wind svg{width:34px;height:34px;flex:none;transition:transform .5s}',
    '.ssm-wind b{display:block}',
    '.ssm-alert{font-size:15px;font-weight:600;border-left:6px solid ' + SUGGESTED + '}',
    '.ssm-alert small{display:block;font-weight:400;opacity:.8;margin-top:2px}',
    '.ssm-alert[hidden],.ssm-routes[hidden],.ssm-scrub[hidden]{display:none}',
    '.ssm-routes{display:flex;gap:8px;flex-wrap:wrap}',
    '.ssm-route{flex:1 1 180px;border:2px solid transparent;display:flex;align-items:center;gap:10px;min-width:0}',
    '.ssm-route.ssm-best{border-color:' + SUGGESTED + '}',
    '.ssm-swatch{width:14px;height:34px;border-radius:5px;flex:none;border:1px solid rgba(0,0,0,.25)}',
    '.ssm-route b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.ssm-route span{font-variant-numeric:tabular-nums}',
    '.ssm-pill{display:inline-block;font-size:11px;font-weight:700;border-radius:999px;padding:0 7px;margin-left:6px;background:#e3e8ef;color:#14181f;vertical-align:1px}',
    '.ssm-pill.ssm-s{background:' + SUGGESTED + ';color:#fff}',
    '.ssm-scrub{display:flex;align-items:center;gap:10px}',
    '.ssm-scrub button{flex:none;width:38px;height:38px;border-radius:50%;border:0;background:#14181f;color:#fff;font-size:15px;cursor:pointer}',
    '.ssm-dark .ssm-scrub button{background:#eef2f7;color:#14181f}',
    '.ssm-scrub input{flex:1;min-width:0;accent-color:#14181f;height:28px}',
    '.ssm-dark .ssm-scrub input{accent-color:#eef2f7}',
    '.ssm-scrub output{flex:none;font-weight:700;font-size:18px;font-variant-numeric:tabular-nums;min-width:52px;text-align:right}',
    '.ssm-scrub label{flex:none;font-size:12px;opacity:.75;max-width:120px}',
    '.ssm-legend{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:12px;align-items:center}',
    '.ssm-legend i{display:inline-block;width:18px;height:6px;border-radius:3px;margin-right:4px;vertical-align:2px;border:1px solid rgba(0,0,0,.25)}',
    '.ssm-legend em{font-style:normal;opacity:.75;flex:1 1 100%}',
    '.ssm-pin{display:flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:50%;background:#14181f;color:#fff;border:2px solid #fff;box-shadow:0 1px 5px rgba(0,0,0,.4)}',
    '.ssm-pin svg{width:16px;height:16px}',
    '.ssm-pin.ssm-plant{background:#5b4636;border-radius:8px}',
    '.ssm-pin.ssm-station{background:#fff;color:#14181f;border-color:#14181f}',
    '.leaflet-tooltip.ssm-tip{background:rgba(255,255,255,.95);border:0;border-radius:8px;box-shadow:0 1px 6px rgba(0,0,0,.25);color:#14181f;font:600 12px/1.3 system-ui,sans-serif;padding:3px 8px}',
    '.leaflet-tooltip.ssm-tip:before{display:none}',
    '.leaflet-tooltip.ssm-tip small{display:block;font-weight:400;opacity:.8}',
    '.ssm-msg{position:absolute;inset:0;z-index:1200;display:flex;align-items:center;justify-content:center;padding:24px;text-align:center;background:rgba(233,237,242,.92);font-weight:600}',
    '@media (max-width:560px){.ssm-source{max-width:100%}.ssm-scrub label{display:none}.ssm-card{padding:6px 9px}.ssm-alert{font-size:14px}.ssm-legend em{font-size:11px}.ssm .leaflet-top.leaflet-right{top:110px}}'
  ].join('\n');

  var ICON = {
    home: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3 2.5 11h2.6v9h5.3v-5.6h3.2V20h5.3v-9h2.6z"/></svg>',
    school: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3 1.5 8.5 12 14l8.2-4.3V16h1.8V8.5zM6 13.2v3.6c0 1.5 2.7 2.9 6 2.9s6-1.4 6-2.9v-3.6l-6 3.1z"/></svg>',
    plant: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 21V11l5 3.2V11l5 3.2V4h3.2l.8 9.5V21zM18.5 3h2l.9 18h-3.6z"/></svg>',
    station: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="2.4" fill="currentColor"/><path d="M6.3 6.3a8 8 0 0 0 0 11.4M17.7 6.3a8 8 0 0 1 0 11.4"/></svg>',
    arrow: '<svg viewBox="0 0 40 40"><circle cx="20" cy="20" r="18" fill="none" stroke="currentColor" stroke-opacity=".3" stroke-width="2"/><path d="M20 6 27 26 20 22 13 26z" fill="currentColor"/></svg>'
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function loadLeaflet(given) {
    if (given) return Promise.resolve(given);
    if (root.L && root.L.map) return Promise.resolve(root.L);
    if (loadLeaflet.pending) return loadLeaflet.pending;
    loadLeaflet.pending = new Promise(function (resolve, reject) {
      var css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = LEAFLET + 'leaflet.css';
      document.head.appendChild(css);
      var js = document.createElement('script');
      js.src = LEAFLET + 'leaflet.js';
      js.onload = function () { resolve(root.L); };
      js.onerror = function () { loadLeaflet.pending = null; reject(new Error('Could not load Leaflet from ' + LEAFLET)); };
      document.head.appendChild(js);
    });
    return loadLeaflet.pending;
  }

  function addCss() {
    if (document.getElementById('ssm-css')) return;
    var style = document.createElement('style');
    style.id = 'ssm-css';
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  // A point `metres` away from [lat, lon] on a compass bearing. Good to a few metres at town scale.
  function offset(lat, lon, bearingDeg, metres) {
    var b = bearingDeg * Math.PI / 180;
    var dLat = metres * Math.cos(b) / 111320;
    var dLon = metres * Math.sin(b) / (111320 * Math.cos(lat * Math.PI / 180));
    return [lat + dLat, lon + dLon];
  }

  function metresBetween(a, b) {
    var k = Math.PI / 180, dLat = (b[0] - a[0]) * k, dLon = (b[1] - a[1]) * k;
    var h = Math.pow(Math.sin(dLat / 2), 2) + Math.cos(a[0] * k) * Math.cos(b[0] * k) * Math.pow(Math.sin(dLon / 2), 2);
    return 6371000 * 2 * Math.asin(Math.sqrt(h));
  }

  function longDate(iso) {
    var p = String(iso).slice(0, 10).split('-');
    var months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return p.length === 3 ? Number(p[2]) + ' ' + months[Number(p[1]) - 1] + ' ' + p[0] : String(iso);
  }

  // ------------------------------------------------------------------ the view

  function View(L, el, opts) {
    addCss();
    this.L = L;
    this.el = el;
    this.opts = opts || {};
    this.lang = TEXT[this.opts.lang] ? this.opts.lang : 'en';
    this.config = this.opts.config;
    if (!this.config) throw new Error('SaansSetuMap.mount needs opts.config (GET /towns/{id}/config)');
    this.slot = 0;
    this.timer = null;
    this.lines = {};       // segment_id -> { casing, line, feature }

    var dark = this.opts.theme === 'dark' ||
      (this.opts.theme === 'auto' && root.matchMedia && root.matchMedia('(prefers-color-scheme: dark)').matches);
    var box = this.box = document.createElement('div');
    box.className = 'ssm' + (dark ? ' ssm-dark' : '');
    box.innerHTML =
      '<div class="ssm-map"></div>' +
      '<div class="ssm-layer ssm-top"><div class="ssm-card ssm-source"></div><div class="ssm-card ssm-wind"></div></div>' +
      '<div class="ssm-layer ssm-bottom">' +
        '<div class="ssm-card ssm-alert" hidden></div>' +
        '<div class="ssm-routes" hidden></div>' +
        '<div class="ssm-card ssm-scrub" hidden><button type="button"></button><label></label><input type="range" min="0" max="0" step="1" value="0"><output></output></div>' +
        '<div class="ssm-card ssm-legend"></div>' +
      '</div>';
    el.innerHTML = '';
    el.appendChild(box);
    this.$ = function (sel) { return box.querySelector(sel); };

    var map = this.map = L.map(this.$('.ssm-map'), { zoomControl: false, attributionControl: true, scrollWheelZoom: this.opts.scrollWheelZoom !== false });
    L.control.zoom({ position: 'topright' }).addTo(map);
    map.attributionControl.setPrefix(false);
    L.tileLayer(this.opts.tiles || TILES, { maxZoom: 19, attribution: this.opts.tileCredit || TILE_CREDIT }).addTo(map);
    this.under = L.layerGroup().addTo(map);     // plume wedge
    this.routesLayer = L.layerGroup().addTo(map);
    this.pins = L.layerGroup().addTo(map);

    var self = this;
    this.$('.ssm-scrub input').addEventListener('input', function (e) { self.stop(); self._slot(Number(e.target.value), true); });
    this.$('.ssm-scrub button').addEventListener('click', function () { self.timer ? self.stop() : self.play(); });

    this._drawTown();
    this._legend();
    this.setRun(this.opts.run || null, this.opts.source);
    this.fit();     // again, now that the cards have their real height
    if (root.ResizeObserver) {
      var wasWide = this.box.classList.contains('ssm-wide'), self = this;
      this.ro = new root.ResizeObserver(function () {
        map.invalidateSize();
        var before = wasWide;
        wasWide = self.layout();
        if (before !== wasWide) self.fit();     // crossing between the two layouts: frame the routes again
      });
      this.ro.observe(box);
    }
  }

  View.prototype.t = function (key) { return TEXT[this.lang][key] || TEXT.en[key] || key; };
  View.prototype.bandName = function (id) { return BANDS[id] ? BANDS[id][this.lang] : '–'; };

  View.prototype._pin = function (lat, lon, kind, title, sub, side) {
    var L = this.L;
    var icon = L.divIcon({ className: '', html: '<div class="ssm-pin ssm-' + kind + '">' + ICON[kind] + '</div>', iconSize: [30, 30], iconAnchor: [15, 15] });
    var m = L.marker([lat, lon], { icon: icon, keyboard: false, zIndexOffset: 500 }).addTo(this.pins);
    m.bindTooltip(esc(title) + (sub ? '<small>' + esc(sub) + '</small>' : ''), { permanent: true, direction: side || 'right', offset: [side === 'left' ? -16 : 16, 0], className: 'ssm-tip' });
    return m;
  };

  View.prototype._drawTown = function () {
    var L = this.L, c = this.config, self = this;
    var reach = [];
    this.routesLayer.clearLayers();
    this.pins.clearLayers();
    this.lines = {};

    ((c.segments_geojson || {}).features || []).forEach(function (f) {
      var pts = f.geometry.coordinates.map(function (xy) { return [xy[1], xy[0]]; });   // GeoJSON is [lon, lat]
      var casing = L.polyline(pts, { color: '#14181f', weight: 9, opacity: 0.55, lineCap: 'round', interactive: false }).addTo(self.routesLayer);
      var line = L.polyline(pts, { color: NO_DATA, weight: 5, opacity: 1, lineCap: 'round' }).addTo(self.routesLayer);
      line.bindTooltip('', { sticky: true, className: 'ssm-tip' });
      self.lines[f.properties.segment_id] = { casing: casing, line: line, feature: f };
      reach = reach.concat(pts);
    });

    // The plant's name is never shown: the layer says where the wind points, not who is responsible.
    if (c.home_area) { this._pin(c.home_area.lat, c.home_area.lon, 'home', this.t('home'), null, 'left'); reach.push([c.home_area.lat, c.home_area.lon]); }
    if (c.school && c.school.lat != null) { this._pin(c.school.lat, c.school.lon, 'school', this.t('school'), c.school.starts_at); reach.push([c.school.lat, c.school.lon]); }
    var centre = reach.length ? L.latLngBounds(reach).getCenter() : null;
    var near = function (p, limit) { return !centre || metresBetween([centre.lat, centre.lng], p) < limit; };
    if (c.plant) {
      this._pin(c.plant.lat, c.plant.lon, 'plant', this.t('plant'), c.plant.approximate ? this.t('approx') : null, 'left');
      if (near([c.plant.lat, c.plant.lon], 4000)) reach.push([c.plant.lat, c.plant.lon]);
    }
    if (c.station && c.station.lat != null) {
      this.stationPin = this._pin(c.station.lat, c.station.lon, 'station', this.t('station'), c.station.name);
      if (near([c.station.lat, c.station.lon], 2500)) reach.push([c.station.lat, c.station.lon]);
    }
    this.reach = reach;
    this.fit();
  };

  /** In a wide box the cards sit in a column on the left; in a narrow one they stack along the bottom. */
  View.prototype.layout = function () {
    var wide = this.box.clientWidth >= 900;
    this.box.classList.toggle('ssm-wide', wide);
    return wide;
  };

  /** Zoom to the routes, keeping them clear of the cards. */
  View.prototype.fit = function () {
    var L = this.L, reach = this.reach || [];
    var wide = this.layout();
    var top = this.$('.ssm-top').offsetHeight + 34, bottom = this.$('.ssm-bottom').offsetHeight + 44, left = 70;
    if (wide) { left = this.$('.ssm-bottom').offsetWidth + 60; bottom = 50; top = 80; }
    var room = this.box.clientHeight - top - bottom;
    if (room < 120) { top = 60; bottom = 60; }     // too short to clear the cards: centre it and let the user pan
    if (reach.length > 1) this.map.fitBounds(L.latLngBounds(reach), { paddingTopLeft: [left, top], paddingBottomRight: [70, bottom], maxZoom: 16, animate: false });
    else if (reach.length) this.map.setView(reach[0], 13);
    else this.map.setView([22.5, 82], 5);
  };

  View.prototype._legend = function () {
    var self = this;
    var html = ORDER.map(function (id) { return '<span><i style="background:' + BANDS[id].color + '"></i>' + esc(self.bandName(id)) + '</span>'; }).join('');
    if (this.config.has_routes) {
      html += '<span><i style="background:#fff;border:2px solid ' + SUGGESTED + ';height:8px"></i>' + esc(this.t('suggested')) + '</span>';
      html += '<em>' + esc(this.t('estimate')) + '</em>';
    }
    this.$('.ssm-legend').innerHTML = html;
  };

  /** Show a run document. `source` is optional: { kind: 'live' | 'morning' | 'replay' } to say where it came from. */
  View.prototype.setRun = function (run, source) {
    var self = this, c = this.config;
    var keep = this.run && this.run.slots ? this.run.slots[this.slot] : null;
    this.run = run;
    this.source = source || null;
    this.values = {};
    this.routeNames = {};
    (c.routes || []).forEach(function (r) { self.routeNames[r.route_id] = (self.lang === 'hi' && r.name_hi) || r.name; });

    var slots = (run && run.slots) || [];
    var routes = (run && run.routes) || [];
    routes.forEach(function (r) {
      (r.segments || []).forEach(function (s) { self.values[s.segment_id] = s; });
    });
    var has = routes.length > 0 && slots.length > 0;
    this.best = has && run.departures && run.departures.best ? run.departures.best : null;
    this.usual = has && run.departures && run.departures.usual ? run.departures.usual : null;

    this._header();
    this._wind();
    this._alert();
    this._station();

    var scrub = this.$('.ssm-scrub');
    scrub.hidden = !has;
    this.$('.ssm-routes').hidden = !has;
    if (!has) { this.stop(); this._paint(); return; }

    var input = this.$('.ssm-scrub input');
    input.max = String(slots.length - 1);
    var start = slots.indexOf(keep);
    if (start < 0) {   // open on the worst moment of the usual route: that is the story
      var usualRoute = routes.filter(function (r) { return self.usual && r.route_id === self.usual.route_id; })[0] || routes[0];
      start = Math.max(0, slots.indexOf(((usualRoute.summary || {}).peak || {}).time));
    }
    this._slot(start, false);
  };

  View.prototype._header = function () {
    var run = this.run, c = this.config, tag = '', sub = '';
    if (run) {
      var now = run.clock && run.clock.now ? run.clock.now : '';
      var kind = (this.source && this.source.kind) || (run.mode === 'replay' ? 'replay' : 'live');
      if (run.is_sample) tag = '<span class="ssm-tag ssm-sample">' + esc(this.t('sample')) + '</span>';
      else if (kind === 'replay' || run.mode === 'replay') tag = '<span class="ssm-tag">' + esc(this.t('replay')) + ' ' + esc(longDate((run.clock && run.clock.replay_of) || now)) + '</span>';
      else tag = '<span class="ssm-tag">' + esc(this.t('live')) + '</span>';
      sub = (kind === 'morning' ? this.t('morning') + ' · ' : '') + this.t('issued') + ' ' + esc(now.slice(11, 16));
    }
    this.$('.ssm-source').innerHTML = tag + esc(c.name) + (c.state ? ', ' + esc(c.state) : '') + (sub ? '<small>' + sub + '</small>' : '');
  };

  View.prototype._wind = function () {
    var L = this.L, c = this.config, box = this.$('.ssm-wind');
    var w = this.run && this.run.weather && this.run.weather.now;
    this.under.clearLayers();
    if (!w || w.wind_from_deg == null) { box.innerHTML = '<div><b>' + esc(this.t('noWind')) + '</b></div>'; return; }
    var to = (w.wind_from_deg + 180) % 360;
    var speed = w.wind_speed_ms == null ? '' : w.wind_speed_ms + ' m/s';
    box.innerHTML = '<span style="display:inline-flex;transform:rotate(' + to + 'deg)">' + ICON.arrow + '</span>' +
      '<div><b>' + esc(this.t('windFrom')) + ' ' + esc(w.wind_from || Math.round(w.wind_from_deg) + '°') + '</b>' +
      esc(speed) + (w.wind_speed_ms != null && w.wind_speed_ms < 1 ? ' · ' + esc(this.t('calm')) : '') + '</div>';
    // The same assumption the estimate uses: more downwind of the industrial area, fading with distance.
    if (c.plant && c.has_routes && this.opts.plume !== false) {
      var reach = 2 * ((c.exposure_params || {}).plume_decay_m || 900);
      var pts = [[c.plant.lat, c.plant.lon]];
      for (var a = -45; a <= 45; a += 9) pts.push(offset(c.plant.lat, c.plant.lon, to + a, reach));
      L.polygon(pts, { color: '#5b4636', weight: 1, dashArray: '4 5', fillColor: '#5b4636', fillOpacity: 0.13 })
        .bindTooltip(esc(this.t('downwind')), { sticky: true, className: 'ssm-tip' }).addTo(this.under);
    }
  };

  View.prototype._alert = function () {
    var box = this.$('.ssm-alert'), run = this.run;
    if (!run || this.opts.alert === false) { box.hidden = true; return; }
    var a = run.alert || {};
    var msg = a.message || (a.earlier_today && a.earlier_today.message) || (run.morning_call && run.morning_call.message);
    var text = msg ? (msg[this.lang] || msg.en) : null;
    var head = msg ? this.t('sent') : (a.decision === 'none' ? this.t('noAlert') : this.t('noAdvice'));
    var d = run.departures, extra = '';
    if (d && d.best && d.saving_vs_usual_pct > 0) extra = d.saving_vs_usual_pct + '% ' + this.t('less');
    if (!text && !this.config.has_routes) { text = this.t('noRoutes'); head = ''; }
    if (!text) text = a.reason || '';
    box.hidden = !text;
    box.innerHTML = esc(text) + '<small>' + esc([head, extra].filter(Boolean).join(' · ')) + '</small>';
  };

  View.prototype._station = function () {
    if (!this.stationPin) return;
    var st = this.run && this.run.station, c = this.config;
    var lines = [];
    if (st && st.latest) lines.push(this.t('latest') + ' ' + st.latest.pm25 + ' · ' + this.bandName(st.latest.band));
    if (st && st.official_24h && st.official_24h.pm25_avg != null) lines.push(this.t('station24') + ' ' + st.official_24h.pm25_avg + ' · ' + this.bandName(st.official_24h.band));
    if (!lines.length && c.station.name) lines.push(c.station.name);
    this.stationPin.setTooltipContent(esc(this.t('station')) + lines.map(function (s) { return '<small>' + esc(s) + '</small>'; }).join(''));
    var dot = this.stationPin.getElement() && this.stationPin.getElement().querySelector('.ssm-pin');
    if (dot) dot.style.borderColor = st && st.latest && BANDS[st.latest.band] ? BANDS[st.latest.band].color : '#14181f';
  };

  View.prototype._slot = function (k, fromUser) {
    var slots = this.run.slots;
    this.slot = Math.max(0, Math.min(slots.length - 1, k));
    this.$('.ssm-scrub input').value = String(this.slot);
    this.$('.ssm-scrub output').textContent = slots[this.slot];
    this.$('.ssm-scrub label').textContent = this.t('at');
    this.$('.ssm-scrub button').textContent = this.timer ? '❚❚' : '▶';
    this.$('.ssm-scrub button').title = this.timer ? this.t('pause') : this.t('play');
    this._paint();
    this._chips();
    if (fromUser && typeof this.opts.onSlot === 'function') this.opts.onSlot(slots[this.slot], this.slot);
  };

  View.prototype._paint = function () {
    var self = this, k = this.slot, bestRoute = this.best ? this.best.route_id : null;
    Object.keys(this.lines).forEach(function (id) {
      var item = self.lines[id], v = self.values[id], props = item.feature.properties;
      var band = v && v.band ? v.band[k] : null;
      var isBest = props.route_id === bestRoute;
      item.line.setStyle({ color: BANDS[band] ? BANDS[band].color : NO_DATA, weight: isBest ? 7 : 5 });
      item.casing.setStyle({ color: isBest ? SUGGESTED : '#14181f', weight: isBest ? 13 : 9, opacity: isBest ? 0.95 : 0.55 });
      var name = self.routeNames[props.route_id] || props.route_id;
      var tip = '<b>' + esc(name) + '</b>';
      if (v) tip += '<small>' + esc(self.t('pm')) + ' ≈ ' + v.pm25[k] + ' · ' + esc(self.bandName(band)) + ' · ' + esc(self.run.slots[k]) + '</small>';
      if (props.distance_to_plant_m != null) tip += '<small>' + props.distance_to_plant_m + ' ' + esc(self.t('fromPlant')) + '</small>';
      item.line.setTooltipContent(tip);
    });
    if (bestRoute) Object.keys(this.lines).forEach(function (id) {     // the suggested route sits on top where two overlap
      var item = self.lines[id];
      if (item.feature.properties.route_id === bestRoute) { item.casing.bringToFront(); }
    });
    if (bestRoute) Object.keys(this.lines).forEach(function (id) {
      var item = self.lines[id];
      if (item.feature.properties.route_id === bestRoute) { item.line.bringToFront(); }
    });
  };

  View.prototype._chips = function () {
    var self = this, k = this.slot, run = this.run;
    this.$('.ssm-routes').innerHTML = run.routes.map(function (r) {
      var s = r.summary || {}, band = (s.worst_band || [])[k];
      var isBest = self.best && self.best.route_id === r.route_id;
      var isUsual = self.usual && self.usual.route_id === r.route_id;
      var pills = (isUsual ? '<span class="ssm-pill">' + esc(self.t('usual')) + ' ' + esc(self.usual.leave) + '</span>' : '') +
        (isBest ? '<span class="ssm-pill ssm-s">' + esc(self.t('suggested')) + ' · ' + esc(self.t('leave')) + ' ' + esc(self.best.leave) + '</span>' : '');
      return '<div class="ssm-card ssm-route' + (isBest ? ' ssm-best' : '') + '">' +
        '<div class="ssm-swatch" style="background:' + (BANDS[band] ? BANDS[band].color : NO_DATA) + '"></div>' +
        '<div style="min-width:0"><b>' + esc(self.routeNames[r.route_id] || r.name) + pills + '</b>' +
        '<span>' + esc(self.bandName(band)) + ' · ' + esc(self.t('pm')) + ' ≈ ' + ((s.worst_pm25 || [])[k] != null ? s.worst_pm25[k] : '–') + '</span></div></div>';
    }).join('');
  };

  /** Move the time slider. Accepts an index or a slot time such as '07:45'. */
  View.prototype.setSlot = function (slot) {
    if (!this.run || !this.run.slots || !this.run.slots.length) return;
    var k = typeof slot === 'number' ? slot : this.run.slots.indexOf(slot);
    if (k >= 0) this._slot(k, false);
  };

  View.prototype.play = function (ms) {
    var self = this;
    if (!this.run || !this.run.slots || this.run.slots.length < 2) return;
    this.stop();
    this.timer = root.setInterval(function () { self._slot((self.slot + 1) % self.run.slots.length, true); }, ms || this.opts.playMs || 900);
    this._slot(this.slot, false);
  };

  View.prototype.stop = function () {
    if (this.timer) { root.clearInterval(this.timer); this.timer = null; }
    if (this.run && this.run.slots && this.run.slots.length) {
      this.$('.ssm-scrub button').textContent = '▶';
      this.$('.ssm-scrub button').title = this.t('play');
    }
  };

  View.prototype.setLang = function (lang) {
    if (!TEXT[lang]) return;
    this.lang = lang;
    var centre = this.map.getCenter(), zoom = this.map.getZoom();
    this._drawTown();
    this._legend();
    this.setRun(this.run, this.source);
    this.map.setView(centre, zoom, { animate: false });
  };

  View.prototype.destroy = function () {
    this.stop();
    if (this.poll) root.clearInterval(this.poll);
    if (this.ro) this.ro.disconnect();
    this.map.remove();
    this.el.innerHTML = '';
  };

  // ------------------------------------------------------------------ loading from the API

  function getJson(url) {
    return root.fetch(url, { cache: 'no-store' }).then(function (r) {
      if (r.status === 404) return null;
      if (!r.ok) throw new Error(url + ' answered ' + r.status);
      return r.json();
    });
  }

  function hasRoutes(run) { return !!(run && run.routes && run.routes.length && run.slots && run.slots.length); }

  /** The run worth drawing: the newest run when it carries route advice, else this morning's call,
   *  else the alert moment of the newest stored replay. Resolves to { run, source } (run may lack routes). */
  function pickRun(api, town, opts) {
    var base = api.replace(/\/+$/, '') + '/towns/' + encodeURIComponent(town);
    opts = opts || {};
    var replayFrame = function (date) {
      return getJson(base + '/replay/' + date).then(function (doc) {
        var frames = (doc && doc.frames) || [];
        var want = opts.replayTime, pick = null;
        if (want) pick = frames.filter(function (f) { return f.clock.now.slice(11, 16) === want; })[0];
        pick = pick || frames.filter(function (f) { return f.alert && f.alert.decision === 'send'; })[0];
        pick = pick || frames.filter(hasRoutes)[0];
        return pick ? { run: pick, source: { kind: 'replay', date: date } } : null;
      });
    };
    if (opts.replayDate) return replayFrame(opts.replayDate).then(function (hit) { return hit || { run: null, source: null }; });
    return getJson(base + '/run/latest').then(function (latest) {
      if (hasRoutes(latest)) return { run: latest, source: { kind: latest.mode === 'replay' ? 'replay' : 'live' } };
      return getJson(base + '/run/morning').catch(function () { return null; }).then(function (morning) {
        if (hasRoutes(morning)) return { run: morning, source: { kind: 'morning' } };
        return getJson(base + '/replay').catch(function () { return null; }).then(function (list) {
          var dates = (list && list.dates) || [];
          if (!dates.length) return { run: latest, source: null };
          return replayFrame(dates[dates.length - 1]).then(function (hit) { return hit || { run: latest, source: null }; });
        });
      });
    });
  }

  function message(el, text) {
    addCss();
    el.innerHTML = '<div class="ssm"><div class="ssm-msg">' + esc(text) + '</div></div>';
  }

  /** Fetch config and the right run from the API, draw them, and keep them fresh.
   *  opts: api (required), town (required), lang, theme, refreshMs (default 60000, 0 = never),
   *        replayDate + replayTime to pin the map to one stored replay moment (for the video). */
  function fromApi(el, opts) {
    if (!opts || !opts.api || !opts.town) return Promise.reject(new Error('SaansSetuMap.fromApi needs opts.api and opts.town'));
    var base = opts.api.replace(/\/+$/, '') + '/towns/' + encodeURIComponent(opts.town);
    return Promise.all([loadLeaflet(opts.L), getJson(base + '/config'), pickRun(opts.api, opts.town, opts)]).then(function (got) {
      if (!got[1]) throw new Error('The API has no town called "' + opts.town + '" yet. Has the first run finished?');
      var view = new View(got[0], el, Object.assign({}, opts, { config: got[1], run: got[2].run, source: got[2].source }));
      var every = opts.refreshMs == null ? 60000 : opts.refreshMs;
      if (every > 0 && !opts.replayDate) {
        view.poll = root.setInterval(function () {
          pickRun(opts.api, opts.town, opts).then(function (next) {
            var a = next.run && next.run.run_id, b = view.run && view.run.run_id;
            if (a && a !== b) view.setRun(next.run, next.source);
          }).catch(function () { /* keep showing the last good run */ });
        }, every);
      }
      return view;
    }).catch(function (e) {
      message(el, 'Map could not load: ' + e.message);
      throw e;
    });
  }

  function mount(el, opts) {
    return loadLeaflet(opts && opts.L).then(function (L) { return new View(L, el, opts); });
  }

  root.SaansSetuMap = { mount: mount, fromApi: fromApi, pickRun: pickRun, BANDS: BANDS, version: '1.0.0' };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.SaansSetuMap;
})(typeof window !== 'undefined' ? window : this);
