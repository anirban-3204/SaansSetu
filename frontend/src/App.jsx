import React, { useEffect, useState, useCallback, useRef } from "react";
import { getTowns, loadTown, getApiBase, setApiBase, apiIsBuiltIn } from "./api.js";
import RouteMap from "./RouteMap.jsx";

const BAND_COLOR = {
  good: "var(--good)", satisfactory: "var(--good)", moderate: "var(--mod)",
  poor: "var(--poor)", very_poor: "var(--poor)", severe: "var(--poor)"
};
const BAND_LABEL = {
  good: "Good", satisfactory: "Satisfactory", moderate: "Moderate",
  poor: "Poor", very_poor: "Very Poor", severe: "Severe"
};

// Times from the API are already Indian time ("2024-12-07T06:30+05:30"). Read the clock straight off the
// text, so a judge in another time zone sees 06:30 too, not their own local hour.
function hhmm(iso) {
  return iso && String(iso).length >= 16 ? String(iso).slice(11, 16) : "—";
}
function niceDate(iso) {
  const [y, m, d] = String(iso || "").slice(0, 10).split("-");
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return d ? `${Number(d)} ${months[Number(m) - 1]} ${y}` : "";
}

function ForecastChart({ hours, high }) {
  const pts = (hours || []).filter((h) => Number.isFinite(h.p50));
  if (!pts.length) return null;
  const top = (h) => (Number.isFinite(h[high]) ? h[high] : h.p50);
  const max = Math.max(...pts.map(top), 150);
  const x = (i) => (pts.length === 1 ? 300 : 20 + (i / (pts.length - 1)) * 560);
  const y = (v) => 140 - Math.min(v / max, 1) * 130;
  const p50 = pts.map((h, i) => `${x(i)},${y(h.p50)}`).join(" ");
  const band = [...pts.map((h, i) => `${x(i)},${y(top(h))}`), ...pts.map((h, i) => `${x(i)},${y(h.p50)}`).reverse()].join(" ");
  return (
    <svg viewBox="0 0 600 160" role="img" aria-label="Station PM2.5 forecast">
      <polygon points={band} fill="var(--sky)" opacity="0.12" />
      <polyline points={p50} fill="none" stroke="var(--sky)" strokeWidth="2.5" />
      {pts.map((h, i) => (
        <g key={i}>
          <circle cx={x(i)} cy={y(h.p50)} r="3" fill="var(--sky)" />
          <text x={x(i)} y={y(h.p50) - 8} fontSize="10" fill="currentColor" opacity="0.8" textAnchor="middle">{Math.round(h.p50)}</text>
          <text x={x(i)} y="156" fontSize="10" fill="currentColor" opacity="0.5" textAnchor="middle">{hhmm(h.time)}</text>
        </g>
      ))}
    </svg>
  );
}

function Pipeline({ steps }) {
  return (
    <div className="ptwrap">
      <h2>Pipeline</h2>
      <ul className="timeline">
        {(steps || []).map((s) => (
          <li key={s.step}>
            <div className="when">{s.step}</div>
            <div className="en">{s.status}{s.detail ? ` — ${s.detail}` : ""}</div>
          </li>
        ))}
      </ul>
    </div>
  );
}

// "24% lower" / "6% higher" from a skill score (1 - model error / naive error).
function skillWords(skill) {
  if (!Number.isFinite(skill)) return null;
  const pct = Math.round(Math.abs(skill) * 100);
  return skill >= 0 ? `${pct}% lower error` : `${pct}% higher error`;
}

export default function App() {
  const [towns, setTowns] = useState([]);
  const [townId, setTownId] = useState(null);
  const [data, setData] = useState(null);          // { run, source, config, accuracy } for the town on screen
  const [error, setError] = useState(null);
  const [ready, setReady] = useState(false);       // first load finished (well or badly)
  const [sampleKey, setSampleKey] = useState("alert"); // demo scenario when no API is set
  const [apiBaseInput, setApiBaseInput] = useState(getApiBase());
  const asked = useRef(0);

  const connected = !!getApiBase();

  // Loads a town. Only the first load shows "Loading…"; refreshes swap the data in place, and a failed
  // refresh keeps the last good screen and says so.
  const load = useCallback(async (id, key) => {
    const mine = ++asked.current;
    try {
      const next = await loadTown(id, key);
      if (mine !== asked.current) return;          // a newer request has started: drop this answer
      setData({ ...next, townId: id });
      setError(null);
    } catch (e) {
      if (mine !== asked.current) return;
      setError(e.message);
    } finally {
      if (mine === asked.current) setReady(true);
    }
  }, []);

  // The town list. If the API is not answering yet, keep asking every 30 seconds.
  useEffect(() => {
    let timer = null, gone = false;
    const ask = () => getTowns()
      .then((t) => {
        if (gone) return;
        if (!t.length) throw new Error("The API has no towns yet. Has the first run finished?");
        setTowns(t);
        setTownId((now) => now || t[0].town_id);   // the API lists towns with routes first
      })
      .catch((e) => {
        if (gone) return;
        setError(e.message.replace(/\.$/, ""));
        setReady(true);
        timer = setTimeout(ask, 30000);
      });
    ask();
    return () => { gone = true; clearTimeout(timer); };
  }, []);

  useEffect(() => {
    if (townId) load(townId, sampleKey);
  }, [townId, sampleKey, load]);

  useEffect(() => {
    if (!townId || !connected) return;
    const t = setInterval(() => load(townId, sampleKey), 30000);
    return () => clearInterval(t);
  }, [townId, sampleKey, load, connected]);

  const fresh = data && data.townId === townId ? data : null;   // never show one town's run under another's name
  const run = fresh?.run || null;
  const source = fresh?.source || null;

  const header = (statusLine) => (
    <>
      <div className="topline">
        <div className="brand">
          <h1>SaansSetu</h1>
          <p>Air-route alerts for the school morning commute.</p>
          <div className="status">{statusLine}</div>
        </div>
        <div className="panelbar">
          {towns.length > 0 && (
            <select value={townId || ""} onChange={(e) => setTownId(e.target.value)} aria-label="Town">
              {towns.map((t) => (
                <option key={t.town_id} value={t.town_id}>{t.name}{t.has_routes === false ? " (forecast only)" : ""}</option>
              ))}
            </select>
          )}
          {!connected && (
            <div className="modebtns">
              {["alert", "clear", "degraded"].map((k) => (
                <button key={k} className={sampleKey === k ? "on" : ""} onClick={() => setSampleKey(k)}>{k}</button>
              ))}
            </div>
          )}
        </div>
      </div>
      {!apiIsBuiltIn() && (
        <div className="cfg">
          <span className="lbl">API base URL</span>
          <input value={apiBaseInput} onChange={(e) => setApiBaseInput(e.target.value)} placeholder="https://xxxxxxxx.lambda-url.ap-south-1.on.aws" />
          <button className="go" onClick={() => { setApiBase(apiBaseInput); window.location.reload(); }}>{connected ? "Change" : "Connect"}</button>
        </div>
      )}
      {error && <div className="banner" role="alert">{error}. {run ? "Showing the last run that loaded." : ""} Retrying every 30 seconds.</div>}
    </>
  );

  if (!ready) return <div className="wrap"><p>Loading…</p></div>;
  if (!run) {
    return (
      <div className="wrap">
        {header(<span>{connected ? "Connected to the API" : "No API connected"}</span>)}
        {!error && <div className="hero"><div className="who">{fresh ? "No run is stored for this town yet. The next tick will make one." : "Loading…"}</div></div>}
      </div>
    );
  }

  // ---- what kind of data is on screen. Said plainly, once, at the top.
  const isSample = source?.kind === "sample" || run.is_sample === true;
  const isReplay = run.mode === "replay";
  const made = hhmm(run.clock?.now);
  let statusLine;
  if (isSample) {
    statusLine = <span><span className="tag sample">Sample data</span> made-up numbers, no API connected</span>;
  } else if (isReplay) {
    statusLine = (
      <span>
        <span className="tag">Replay</span> {niceDate(run.clock?.replay_of || run.clock?.now)}, the live pipeline on a past morning
        {source?.kind === "morning" ? ` · showing this morning's call, made at ${made}` : ` · this run was made at ${made}`}
      </span>
    );
  } else {
    statusLine = <span><span className="pulse" /> Live{source?.kind === "morning" ? ` · showing this morning's call, made at ${made}` : ` · updated ${made}`}</span>;
  }

  // ---- pieces of the run. A failed run has no station, weather or forecast at all.
  const station = run.station || null;
  const routes = run.routes || [];
  const steps = run.steps || [];
  const alert = run.alert || {};
  const ok = run.status === "ok" && !!station;
  const nameOf = (id) => routes.find((r) => r.route_id === id)?.name || id;
  const worstRoute = routes.slice().sort((a, b) => (b.summary?.peak?.pm25 || 0) - (a.summary?.peak?.pm25 || 0))[0];
  const cityBand = station?.official_24h?.band;
  const cityAvg = station?.official_24h?.pm25_avg;
  const streetBand = worstRoute?.summary?.peak?.band;

  // After the alert goes out, later runs carry it in earlier_today (their own message is null).
  // After 10:00 the live school flag is "unknown"; the morning's call still holds it.
  const message = alert.message || alert.earlier_today?.message || run.morning_call?.message || null;
  const sentAt = alert.sent_at || alert.earlier_today?.sent_at || null;
  const recipients = alert.message ? alert.recipients : alert.earlier_today?.recipients;
  const liveFlag = alert.school_flag;
  const flag = liveFlag && liveFlag.outdoor_pt !== "unknown" ? liveFlag : run.morning_call?.school_flag || liveFlag || { outdoor_pt: "unknown" };

  const dep = run.departures;
  const high = run.forecast?.bad_case || (run.forecast?.hours?.[0]?.p95 != null ? "p95" : "p90");
  const fcHours = run.forecast?.hours || [];

  // ---- accuracy. The held-out winter is the number to quote; a handful of live mornings is not.
  const acc = fresh.accuracy;
  const backtest = acc?.backtest?.level;
  const running = acc?.running;
  const scored = running?.mornings_scored;
  const liveSkill = running && running.mae_persistence > 0 && Number.isFinite(running.mae_model) ? 1 - running.mae_model / running.mae_persistence : null;

  return (
    <div className="wrap">
      {header(<>{statusLine}{run.status !== "ok" && run.status_message?.en && <span className="err"> · {run.status_message.en}</span>}</>)}

      {!ok ? (
        <div className="hero">
          <div><span className="big" style={{ fontSize: 28 }}>{run.status === "failed" ? "This run could not finish" : "No route advice this morning"}</span></div>
          <div className="who">{run.status_message?.en || alert.reason}</div>
        </div>
      ) : (
        <div className="hero">
          <div><span className="big">{Number.isFinite(cityAvg) ? cityAvg.toFixed(0) : "—"}</span><span className="unit">µg/m³ city average (24h)</span></div>
          <div className="who">
            <span className="statusdot" style={{ background: BAND_COLOR[cityBand] }} />City badge says <b>{BAND_LABEL[cityBand] || "—"}</b>
            {worstRoute?.summary?.peak && <> — but <b>{worstRoute.name}</b> peaks at <span style={{ color: BAND_COLOR[streetBand] }}>{BAND_LABEL[streetBand]}</span> ({worstRoute.summary.peak.pm25} µg/m³, {worstRoute.summary.peak.time})</>}
          </div>
        </div>
      )}

      {fresh.config && run.status !== "failed" && (
        <div className="mapwrap">
          <RouteMap config={fresh.config} run={run} source={source} />
        </div>
      )}

      <div className="minis">
        <div className="mini"><div className="k">Forecast inputs</div><div className="v">{!run.forecast ? "—" : run.forecast.feature_set === "fut" ? "With weather forecast" : "Current weather only"}</div></div>
        {backtest ? (
          <div className="mini" title={acc.backtest.note}>
            <div className="k">Against repeating the last reading, held-out winter ({backtest.hours_scored} school-morning hours)</div>
            <div className="v">{skillWords(backtest.skill_vs_persistence) || "—"}</div>
          </div>
        ) : (
          <div className="mini">
            <div className="k">Against repeating the last reading, {running?.window_days || 14}-day window</div>
            <div className="v">{skillWords(liveSkill) || "Not scored yet"}</div>
          </div>
        )}
        {backtest && (
          <div className="mini" title={running?.note || ""}>
            <div className="k">Checked against what happened{scored ? `, ${scored} morning${scored === 1 ? "" : "s"} so far` : ""}</div>
            <div className="v">{scored && Number.isFinite(running.mae_model) ? `off by ${running.mae_model} µg/m³ (naive: ${running.mae_persistence})` : "No morning scored yet"}</div>
          </div>
        )}
        <div className="mini"><div className="k">Alert this morning</div><div className="v">{message ? "Sent" : alert.decision === "held" ? "Held" : "Not needed"}</div></div>
      </div>

      {ok ? (
        <div className="cols">
          <div>
            {routes.length > 0 && (
              <div className="routes">
                {routes.map((r) => (
                  <div className="route" key={r.route_id}>
                    <div className="top">
                      <span className="name">{r.name}</span>
                      <span className="read">{r.summary.peak.pm25} µg/m³ peak</span>
                    </div>
                    <div className="word" style={{ color: BAND_COLOR[r.summary.peak.band] }}>{BAND_LABEL[r.summary.peak.band]} around {r.summary.peak.time}</div>
                    <div className="track"><div className="fill" style={{ width: `${Math.min(100, r.summary.peak.pm25 / 180 * 100)}%`, background: BAND_COLOR[r.summary.peak.band] }} /></div>
                  </div>
                ))}
              </div>
            )}

            {fcHours.length > 0 && (
              <div className="chart">
                <ForecastChart hours={fcHours} high={high} />
                <div className="cap">Station forecast for the next {fcHours.length} hours. Each point is the average over the hour starting then. Line: likely value. Shaded: up to the bad case ({high}).</div>
              </div>
            )}

            {dep && dep.usual && (
              <div className="chart" style={{ marginTop: 22 }}>
                <h2 style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>Best vs. usual departure</h2>
                <div className="route">
                  <div className="top"><span className="name">Usual — {nameOf(dep.usual.route_id)}, leave {dep.usual.leave}</span><span className="read">{dep.usual.peak_pm25} µg/m³ peak</span></div>
                </div>
                {dep.best ? (
                  <div className="route">
                    <div className="top"><span className="name">Best — {nameOf(dep.best.route_id)}, leave {dep.best.leave}</span><span className="read">{dep.best.peak_pm25} µg/m³ peak</span></div>
                    {Number.isFinite(dep.saving_vs_usual_pct) && (
                      <div className="word" style={{ color: dep.saving_vs_usual_pct > 0 ? "var(--good)" : "var(--sub)" }}>
                        {dep.saving_vs_usual_pct > 0 ? `${dep.saving_vs_usual_pct}% lower estimated dose` : "No better option than the usual one"}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="route"><div className="word" style={{ color: "var(--sub)" }}>No route and leaving time reaches school before it starts.</div></div>
                )}
              </div>
            )}
          </div>

          <div>
            <div className="alerts">
              <h2>Parent alert</h2>
              {message ? (
                <ul className="timeline">
                  <li>
                    <div className="when">{sentAt ? `${hhmm(sentAt)} · ` : ""}via Telegram{recipients > 0 ? ` · ${recipients} recipient${recipients === 1 ? "" : "s"}` : ""}</div>
                    <div className="en">{message.en}</div>
                    <div className="hi" lang="hi">{message.hi}</div>
                  </li>
                </ul>
              ) : (
                <p className="timeline" style={{ borderLeft: "none", paddingLeft: 0 }}>
                  <span className="empty">{alert.decision === "held" ? "Held: " : "No alert needed: "}{alert.reason}</span>
                </p>
              )}
            </div>
            <div className="ptwrap">
              <h2>School flag</h2>
              <div className="ptrow">
                <span className="pticon">{flag.outdoor_pt === "move_indoors" ? "🚫" : flag.outdoor_pt === "unknown" ? "❓" : "🌤️"}</span>
                <div>
                  <div className="pv">{flag.outdoor_pt === "move_indoors" ? "Move PT and assembly indoors" : flag.outdoor_pt === "unknown" ? "Status unknown" : "Outdoor PT as usual"}</div>
                  <div className="ps">{flag.reason}</div>
                </div>
              </div>
            </div>
            <Pipeline steps={steps} />
          </div>
        </div>
      ) : (
        <Pipeline steps={steps} />
      )}

      <footer>
        Station: {station?.latest?.pm25 != null ? `${station.latest.pm25} µg/m³ over the last finished hour, ${station.age_minutes} min old` : "no recent reading"}.{" "}
        {isSample
          ? `This is bundled sample data ("${sampleKey}" scenario) with made-up numbers.`
          : isReplay
            ? `Data from the SaansSetu API: real station readings from ${niceDate(run.clock?.replay_of || run.clock?.now)}, run through the live pipeline on a shifted clock.`
            : "Data is live from the SaansSetu API."}{" "}
        Route and segment values are estimates from the station forecast, not measurements.
        {fresh.config?.credits?.length > 0 && <div className="credits">Data: {fresh.config.credits.join(" · ")}</div>}
      </footer>
    </div>
  );
}
