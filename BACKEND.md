# SaansSetu backend

Every 15 minutes, for each town: pull the feed, forecast, work out route advice, send at most one alert,
store the result. The dashboard reads what was stored.

    feed -> store -> [ ingest -> forecast -> exposure -> departure -> alert ] -> store -> API
                       one run of core.pipeline.run_once()

A replay is the same code on a past day: the archive stands in for the feed and the clock is set by hand.

## What is in the box

    train.py  fetch_data.py  check_data.py      (you have these)
    saanssetu_data/  out_v3/                     (you have these)

    core/pipeline.py      one run: history + models + town file -> run document
    core/forecasting.py   the models, kept in memory
    core/exposure.py  departure.py  alert.py  bands.py  geo.py
    core/townconfig.py    town files, with or without routes
    core/store.py         where things are kept: in memory, or one DynamoDB table
    core/ingest.py        feed -> store, and store -> the frame the model reads
    core/runner.py        one tick for every town; also replays a whole day
    core/check.py         nightly check and the accuracy panel
    core/evidence.py      which wind direction the dirtiest mornings come from
    core/telegram.py      subscribe, and send the alert
    core/api.py           the HTTP API
    lambda_function.py    AWS entry points

    replay.py        replay one moment (--at), a whole morning (--day), or list good mornings (--suggest)
    serve.py         the API on localhost, fed by a replayed morning. For dashboard work.
    make_towns.py    a town file for every station whose model passed
    make_routes.py   real routes from a drawing on geojson.io
    make_evidence.py wind-direction table for every town, from the full archive
    pack_models.py   collect what the Lambda needs into deploy/
    probe_live.py    check the live feeds (run once, on your machine)
    towns/           asansol.json, plus whatever make_towns.py writes
    tests/

## Commands

    pip install pytest boto3 "moto[dynamodb]"
    python -m pytest

    python make_towns.py                                            town files for Rourkela, Visakhapatnam, Angul
    python replay.py --town towns/asansol.json --suggest            mornings worth showing
    python replay.py --town towns/asansol.json --at "2024-12-07 06:30"
    python replay.py --towns towns --day 2024-12-07                 the whole morning, every town, through the live path
    python serve.py --day 2024-12-07                                same, then serve it on http://localhost:8000
    python make_routes.py towns/asansol.json map.geojson            real routes (instructions at the top of the file)
    python make_evidence.py                                         wind-direction evidence for every town
    python pack_models.py --days 2024-12-01 2024-12-20              build deploy/ (run make_evidence.py first)
    python probe_live.py                                            check the live feeds

Tests that need data look for `./saanssetu_data`, the newest `./out*/` and `./towns`, and skip without them.
The DynamoDB tests run against a local fake (moto) and skip without it.

## Towns

A town file with only a `station` is a forecast-only town: it gets the forecast and the school flag.
A town file with `plant`, `school`, `routes`, `segments_geojson` and `usual_departure` also gets route advice
and the parent alert. `make_towns.py` writes the first kind for every station that passed and never touches
a file that exists. `make_routes.py` turns one into the second kind.

Each town runs on its own. If one town's feed is down, its models are missing or its file is broken, that
town shows `failed` or `degraded` and the others carry on.

`alert_label` in a town file picks the chance label behind the school flag. `make_towns.py` sets it to
`poor` wherever the Poor model passed its own test, so the flag means the same thing in every town.

## What an hourly reading means

A reading stamped 06:00 is the average over 06:00 to 07:00. It does not exist until 07:00. This was checked on
7 Oct 2026 against OpenAQ's copy of the same CPCB stations: three stations out of three line up with the archive
when stamped by the start of the hour (`probe_live.py`). So:

- A run uses only readings whose hour has ended. At 06:30 the newest one is stamped 05:00.
- `age_minutes` counts from the end of that hour. A reading can be 0 to 59 minutes old with a perfect feed.
- Forecast hours follow the same rule: the value at 08:00 is the forecast for 08:00 to 09:00.
- The 6 AM alert therefore rests on the reading for 05:00 to 06:00. `train.py` scored exactly that case and calls
  it `lagged_5am`. Its `fresh_6am` case needs the 06:00 to 07:00 reading, which arrives at 07:00, too late for a
  7 AM walk. Verdicts and backtest numbers shown by the backend come from `lagged_5am`. Quote those, not `fresh_6am`.
- When route values are worked out, each hourly value is placed at the middle of its hour.

`replay.py --lag N` adds a publishing delay of N minutes on top, to see the system behind a slow feed.

## One day, in order

- **Any time:** forecast for the next 4 hours. Stale feed (newest reading ended more than `stale_after_minutes` ago) -> `degraded`.
- **Before the forecast reaches school time:** `held / before_morning_window`. Not an error.
- **From about 05:00:** route advice is worked out. A "send" before `alert_rule.earliest` (06:00) waits: `none / too_early`.
- **First run at or after 06:00 that has a school-hours forecast:** stored as the day's call (`/run/morning`).
  This is the forecast the nightly check scores. It is fixed in advance, never picked afterwards.
- **Alert:** at most one per town per morning. The run that sends first claims the day in the store, so two
  overlapping runs cannot both send. Later runs say `none / already_sent_today` and carry the sent message
  in `alert.earlier_today`.
- **After `departure_window.latest` (07:35):** `held / after_departure_window`. The school flag is still given.
- **First tick after midnight:** yesterday is scored and the accuracy panel rebuilt. A day the system was not
  running is not scored.

## API

    GET  /health
    GET  /towns                          one line per town, towns with routes first
    GET  /towns/{id}/config              station, bands, labels; with routes: school, plant, routes, map geometry
    GET  /towns/{id}/run/latest          newest run
    GET  /towns/{id}/run/morning         the day's call. ?date=YYYY-MM-DD
    GET  /towns/{id}/accuracy            yesterday vs actual, last two weeks, backtest
    GET  /towns/{id}/evidence            wind-direction table for the polar plot
    GET  /towns/{id}/replay              dates with a stored replay
    GET  /towns/{id}/replay/{date}       every run of that morning, in order: {times: [...], frames: [...]}
    POST /telegram/webhook               for Telegram

All answers are JSON with CORS open. Unknown town or nothing stored yet -> 404 with `{"error": ...}`.

## Evidence: where the dirty air comes from

`make_evidence.py` counts, over every winter morning in the archive (October to February, the hours starting
6 to 9 AM), how often each wind direction and speed came with one of the dirtiest hours. It is the conditional
probability method (CBPF): plain counting, no model. It saves `evidence.json` beside each station's models; the
runner publishes it on every tick, and `pack_models.py` carries it into the bundle.

How it stays honest:

- **"Dirtiest" is judged against the same month and the same hour.** January is dirtier than October and 8 AM
  dirtier than 6 AM whatever the wind does. Without this, a dirty month's usual wind would look like a source.
- **Still air gets no direction.** Hours under 1 m/s are counted and reported, but left out of the direction verdict.
- **A direction "stands out" only if it survives resampling.** Whole days are redrawn 400 times; the sector must
  stay on top in 80% of them and be at least 1.2 times as common among dirty hours at the low end of its range.
  Otherwise the verdict is `no_clear_direction`, and the sentence says exactly that.
- **No company is named, ever.** The plant is "the industrial area", and it is mentioned only when the town file
  holds a real location. With the placeholder plant nothing is said about it.
- **The wind is area wind** from a weather reanalysis (about a 25 km grid), not an instrument at the station.
- **Known limit:** days are resampled whole, but weather often holds for several days, so the stated range is
  somewhat narrower than it should be. On made-up winters with no source it claimed a direction 0 times in 90;
  with a source making the air 1.3 times dirtier it found it 30 times in 30. Real winds are less tidy than those.

What the dashboard gets at `/towns/{id}/evidence`:

- `verdict`: `stands_out`, `no_clear_direction`, `mostly_still_air` or `not_enough_data`
- `polar`: `probability[speed band][direction]` and `count[...]`, 5 speed bands by 8 directions (N, NE, ... NW,
  each 45 degrees, centred on the compass point). Grey out cells where `count` is under `min_count`.
- `top_sector`: the 90-degree sector on top, with `share_of_high_hours`, `share_of_all_hours`, `lift`,
  `lift_ci95` and `stable_in`. Present even when the verdict is `no_clear_direction`; do not highlight it then.
- `calm`: share of the dirtiest hours that came in still air
- `plant`: direction of the plant from the station, `location_is_placeholder`, `inside_top_sector`; `null` for
  towns without a plant
- `summary.en` / `summary.hi` and `caveats.en` / `caveats.hi`: fixed sentences. Show the caveats with the plot.

This differs from the old sample `evidence.json`: 8 directions of 45 degrees (was 12 of 30), `threshold` has
`pm25_typical` / `pm25_lowest` / `pm25_highest`, and `plant` is an object.

## Changes the dashboard needs to know (schema 0.4)

Nothing was removed from the run document.

New in 0.4, after the stamp check:

- **Every hourly time is the start of the hour it covers.** `station.latest.time`, `station.history_24h[].time` and
  `forecast.hours[].time` all mean "the hour starting then". Draw each value across its hour or at its middle,
  not as a point on the hour. `station.reading_minutes` is 60 and `station.reading_note` says this in words.
- `station.age_minutes` now counts from the end of the reading's hour. At 06:30 the newest reading is the one at
  `05:00` and it is 30 minutes old.
- `accuracy.backtest.case` is `lagged_5am`, and `towns[].model.level` is judged on that case.

Added in 0.3:

- `has_routes` on every run. When false: `routes` is `[]`, `departures` is `null`, the exposure and departure
  steps are `skipped`, and `alert.reason_code` is `no_routes`. The forecast and `alert.school_flag` are real.
- `alert.reason_code` can also be `too_early`, `before_morning_window`, `no_routes`, `run_failed`.
- `alert.earlier_today` `{message, sent_at, recipients, reason}` on runs after the alert went out. Show this
  message, because `alert.message` is `null` on those runs.
- `morning_call` `{run_id, issued_at, school_flag, alert_decision, alert_reason, message}`: the day's call,
  on every run once it is made, `null` before. Use it for the PT flag after 10:00, when the live flag is `unknown`.
- `feed` `{source, aq: {new_rows, error}, wx: {...}}`: what the feed delivered on this run.
- `status: "failed"` runs have `station`, `weather`, `forecast` all `null`.
- `towns[]` lines gained `school_flag`, `mode`, `updated_at`.
- Accuracy: `forecast_p95` / `within_p95` / `p95_coverage` (was p90). `was_poor` is now `was_bad`, with `label`
  and `threshold` saying which level it means. New `flagged` (school flag said move indoors); `outcome`
  compares `flagged` with `was_bad`. `outcome` can also be `no_forecast` or `no_readings`.

## Things to know

- **Honest replays.** `--day` and `--at` use the backtest models, which never saw Oct 2024 to Feb 2025.
  `--final` uses the deployment models, which did, and says so.
- **The store path equals the direct path.** A test checks that a run through feed -> store -> history gives
  the same forecast, routes and advice as `run_once` on the training frame. History from the store is
  cleaned with the same `train.clean` the training files went through.
- **Nothing unknowable is used.** The archive source hands over a reading only once its hour has ended, and
  `run_once` applies the same rule again on whatever frame it is given.
- **Segment values are estimates** from a formula with visible settings. They are not measurements, and with
  the placeholder routes the saving percentage means nothing. Draw real routes before quoting it.
- **Serving uses the `now` models** (current weather only).

## The live pollutant feed: checked on 7 Oct 2026, not available

`probe_live.py` was run for all four towns.

- **OpenAQ carries all four stations but was not live.** Its newest readings were from 1 and 2 Oct, five days
  behind. Three stations stopped at the same minute, which looks like a stalled import, so it may come back.
- **Its values are the archive's values.** Same raw ug/m3, hour by hour (correlation 0.92 to 0.99 on three stations).
- **Stamps mark the start of the hour.** See "What an hourly reading means".
- **Asansol could not be compared.** OpenAQ lists its PM2.5 sensor twice and the probe picked the empty one.
  The probe now tries every PM2.5 sensor at a station. The stamp rule rests on the other three stations, which
  come from the same CPCB network through the same archive.
- **Live weather works**, mixing height included (Open-Meteo forecast API).
- The XKDR archive has no live endpoint. It is a snapshot.

So during the event the system runs on the archive with a shifted clock (`SAANSSETU_SOURCE=archive`), and says
`mode: replay` on every run. Label it as a replay on screen.

If OpenAQ comes back: the adapter is one small class with a `pollutants(station, since, until)` method
(see `core/ingest.py`). Take PM2.5 and PM10 only at first; OpenAQ reports the gases in ppb, the archive does not.
Everything behind the adapter is done and tested with a late, failing and empty feed.

## Deploying

`python pack_models.py --days FIRST LAST` builds `deploy/` with `models/`, `towns/` and `saanssetu_data/`.

DynamoDB: one table, partition key `pk` (String), sort key `sk` (String), on-demand, TTL attribute `ttl`.
`core.store.create_table(name)` makes it.

Two functions from the same code is the robust layout:

| Function | Handler | Needs | Trigger |
|---|---|---|---|
| API | `lambda_function.http_handler` | zip of `lambda_function.py` + `core/`. No pandas, no models. | Function URL or HTTP API |
| Runner | `lambda_function.tick_handler` | container image: code, `train.py`, `deploy/`, lightgbm, pandas | EventBridge, every 15 min |

One function with `lambda_function.handler` also works; the API is then as slow to start as the image.

Environment variables are listed at the top of `lambda_function.py`. With the archive source,
`SAANSSETU_CLOCK_SHIFT_DAYS` sets how far back the clock runs. 670 makes 8 Oct 2026 behave as 7 Dec 2024,
9 Oct as 8 Dec, and so on, so each hackathon day replays a new December morning at the real time of day and
the accuracy panel fills with real scored mornings. Pick the shift so the days land on mornings with data
(`pack_models.py` prints how many days have the 5-to-6 AM reading the alert needs).

To load one morning into the deployed table for the video:

    python replay.py --towns towns --day 2024-12-07 --park 06:30 --table saanssetu

Telegram: create a bot with @BotFather, set `TELEGRAM_TOKEN` and `TELEGRAM_SECRET`, then point the bot at
`https://<api>/telegram/webhook` with `setWebhook` and the same `secret_token`. People join with
`/start asansol`. To test a real send from your laptop: `--send-to <your chat id>` on `replay.py --day`.

`Dockerfile` and `requirements-lambda.txt` are a starting point for the runner image. They have not been
built or run.
