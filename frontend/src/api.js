// SaansSetu API client.
//
//   GET /towns                      { towns: [...] }
//   GET /towns/{id}/config          what the map draws
//   GET /towns/{id}/run/latest      newest run
//   GET /towns/{id}/run/morning     the day's call
//   GET /towns/{id}/replay[/date]   stored replay mornings
//   GET /towns/{id}/accuracy        404 until a morning has been scored
//
// Rule: once an API address is set, bundled sample data is never shown. A failed call shows as an error,
// not as made-up numbers. Samples appear only when no API address is set at all, and are labelled.

import "./saanssetu-map.js"; // also gives window.SaansSetuMap.pickRun
import sampleTowns from "./samples/towns.json";
import sampleConfig from "./samples/town_config.json";
import sampleAlert from "./samples/run_alert.json";
import sampleClear from "./samples/run_clear.json";
import sampleDegraded from "./samples/run_degraded.json";
import sampleAccuracy from "./samples/accuracy.json";

const SAMPLE_RUNS = { alert: sampleAlert, clear: sampleClear, degraded: sampleDegraded };
const clean = (url) => String(url || "").trim().replace(/\/+$/, "");

// Where the API address comes from, first match wins:
//   1. window.SAANSSETU_API      (a /config.js next to index.html; deploy_aws.py --ui writes one)
//   2. VITE_API_BASE             (put it in .env.production before `npm run build`)
//   3. the box on the page       (saved in this browser only: fine for development, useless for judges)
function builtIn() {
  const fromPage = typeof window !== "undefined" ? window.SAANSSETU_API : "";
  const fromBuild = import.meta.env ? import.meta.env.VITE_API_BASE : "";   // Vite only; harmless elsewhere
  return clean(fromPage || fromBuild);
}
export function apiIsBuiltIn() {
  return !!builtIn();
}
export function getApiBase() {
  let saved = "";
  try { saved = localStorage.getItem("saanssetu_api_base"); } catch { /* storage blocked */ }
  return builtIn() || clean(saved);
}
export function setApiBase(url) {
  try { localStorage.setItem("saanssetu_api_base", clean(url)); } catch { /* storage blocked */ }
}

// null when the API says "nothing stored yet" (404). Throws on anything else.
async function get(path) {
  let res;
  try {
    res = await fetch(getApiBase() + path, { cache: "no-store" });
  } catch {
    throw new Error("Could not reach the API");
  }
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${path} answered ${res.status}`);
  return res.json();
}

export async function getTowns() {
  if (!getApiBase()) return sampleTowns.towns;
  const doc = await get("/towns");
  return (doc && doc.towns) || [];
}

// Everything one town's page needs, fetched together so the cards and the map always show the same run.
//   run      the run to draw, or null when nothing is stored yet
//   source   { kind: 'live' | 'morning' | 'replay' | 'sample' }: says which run that is
//
// The newest run has route advice only between about 05:00 and 07:35. Outside that window the page shows
// this morning's call, and failing that the alert moment of the newest stored replay. pickRun does that.
export async function loadTown(townId, sampleKey = "alert") {
  const base = getApiBase();
  if (!base) {
    return { run: SAMPLE_RUNS[sampleKey], source: { kind: "sample" }, config: sampleConfig, accuracy: sampleAccuracy };
  }
  const id = encodeURIComponent(townId);
  const [picked, config, accuracy] = await Promise.all([
    window.SaansSetuMap.pickRun(base, townId).catch((e) => {
      throw new Error(/Failed to fetch|NetworkError|Load failed/i.test(e.message) ? "Could not reach the API" : e.message);
    }),
    get(`/towns/${id}/config`),
    get(`/towns/${id}/accuracy`).catch(() => null), // the accuracy panel must never cost the page
  ]);
  return { run: picked.run, source: picked.source, config, accuracy };
}
