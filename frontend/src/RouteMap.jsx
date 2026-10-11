import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "./saanssetu-map.js"; // sets window.SaansSetuMap

// The route map. Give it the town's config and the run to draw; it never fetches on its own,
// so it always shows the same run as the cards around it.
export default function RouteMap({ config, run, source, lang = "en", height = 580 }) {
  const box = useRef(null);
  const view = useRef(null);
  const shown = useRef(null);
  const latest = useRef({ run, source });
  latest.current = { run, source };

  // A new town (or language): build the map again.
  useEffect(() => {
    let gone = false;
    window.SaansSetuMap.mount(box.current, { L, config, lang, theme: "auto", ...latest.current }).then((v) => {
      if (gone) return v.destroy();
      view.current = v;
      shown.current = latest.current.run;
      if (v.run !== latest.current.run) v.setRun(latest.current.run, latest.current.source);
    });
    return () => {
      gone = true;
      if (view.current) view.current.destroy();
      view.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config?.town_id, lang]);

  // A different run for the same town: repaint, keep the zoom. An unchanged run is left alone,
  // so the 30-second refresh does not reset the time slider.
  useEffect(() => {
    const was = shown.current;
    const same = was === run || (was && run && was.run_id === run.run_id && was.status === run.status && was.alert?.decision === run.alert?.decision);
    if (view.current && !same) {
      view.current.setRun(run, source);
      shown.current = run;
    }
  }, [run, source]);

  return <div ref={box} className="routemap" style={{ height }} />;
}
