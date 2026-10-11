# SaansSetu

**Air alerts for the walk to school, one route at a time.**

*SaansSetu* was built for Environmental Hacks (AWS × WeMakeDevs Bharat Builds
Tour, 8–11 October 2026), Air track, by team Axiom: Manideep(dashboard and deployment) and  Anirban (ML and backend).

## The problem

The AQI badge for an Indian town is a 24-hour average from one monitoring station. A child's walk to school is
half an hour, along particular roads, at the worst time of the morning. The badge can read "Moderate" while the
same station's reading for that one hour is far worse, and it says nothing about which road to take.

Parents do not need another number. They need to know two things before 7 AM: which way to go, and when to leave.

## What it does

Every 15 minutes, for each town, SaansSetu:

1. **Forecasts** the station's PM2.5 for the next four hours.
2. **Spreads that forecast onto the street**, segment by segment (about 100 m each), along each mapped route.
3. **Compares every route and leaving time** by dose: concentration × minutes, summed along the walk.
4. **Sends at most one message** per morning, and only when it would change what the family does:

   > Main Road will likely turn Very Poor around 7:45. Take Colony Road, and leave by 7:00.
   >
   > मेन रोड पर लगभग 7:45 बजे हवा बहुत खराब होने की संभावना है। कॉलोनी रोड से जाएँ, और 7:00 तक निकलें।

   (The wording is a fixed Hindi and English template. The road names above are only an example of the format.)
5. **Flags the school**: move PT and assembly indoors, or carry on as usual.
6. **Checks itself** the next day: the morning's forecast against what the station then recorded.

The dashboard shows the same thing as a map with a time slider, so you can watch a road change colour through the
morning.

## What is real and what is an estimate

We would rather say this plainly than have someone find it.

| Thing | What it is |
|---|---|
| Station readings | Real. CPCB continuous monitoring stations, hourly. |
| Weather | Real. Open-Meteo's ERA5 archive: wind, mixing height and so on, on a grid of about 25 km. |
| Station forecast | A trained model, scored on a winter it never saw (numbers below). |
| Street-segment values | **An estimate.** The station forecast multiplied by a formula: a bump for rush-hour traffic (bigger on main roads) and a bump for being downwind of the industrial area (fading with distance). The settings are visible in each town file. Nobody measured those streets. |
| The "% lower dose" figure | What that formula implies for one route and time against another. It is not a measured reduction in anyone's exposure. |
| Routes | Real walking paths from OpenStreetMap. The home and school points are illustrative. |
| "Live" | **It is a replay.** See the next section. |

We make no health claims. The tool compares estimated exposure between options; it does not say what that
exposure does to a child.

## Why the deployed system is a replay

On 7 October we checked for a live feed of these stations (`probe_live.py`). OpenAQ carries all four, but its
newest readings were five days old. The archive we trained on is a snapshot with no live endpoint.

So the deployment runs the real pipeline on a shifted clock: 11 October 2026 behaves as 7 December 2024, the next
day as 8 December, and so on, at the real time of day. Readings are handed to the system only once the hour they
cover has ended, exactly as a live feed would. Every run says `mode: replay`, and the dashboard shows a REPLAY tag
with the date.

The feed adapter is one small class (`core/ingest.py`). When a live source exists, nothing else changes.

One timing detail matters for honesty. A reading stamped 06:00 is the average from 06:00 to 07:00, so it does not
exist until 07:00. An alert sent at 6 AM can only use the reading for 5 to 6 AM. All the scores below are for that
case, because that is the alert a family can act on.

## How good is the forecast

Models: LightGBM. For each town and each hour ahead (+1 to +4) there is a "likely" model (median), a "bad case"
model (95th percentile) and calibrated chance models for Moderate, Poor and Very Poor, wherever a town has enough
hours on both sides of the line to train one. The level models predict
the *change* from the latest reading, because a tree model that predicts the value itself can never forecast a
morning worse than any it was trained on.

The test: train on everything before the test winter, then forecast 1 October to 31 December 2024 (where our
archive ends) without ever having seen it. The baseline is "repeat the last reading", which is hard to beat one
or two hours ahead.

Error for 7–9 AM, from a 6 AM alert that uses the 5-to-6 AM reading, compared with the baseline:

| Town (station) | Training hours | Without a weather forecast (what we deploy) | With weather for the target hour (upper bound) |
|---|---:|---:|---:|
| Asansol (Court Area) | 39,017 | 20.2% lower | 21.5% lower |
| Rourkela (Raghunathpali) | 15,032 | 24.9% lower | 27.8% lower |
| Visakhapatnam (GVM Corporation) | 40,187 | 17.6% lower | 19.7% lower |
| Angul (Hakimapada) | 7,763 | 29.8% lower | 30.5% lower |

Notes on reading this:

- "With weather" uses the recorded weather at the target hour as a stand-in for a perfect forecast. Real skill
  sits between the two columns. We deploy the left one.
- Angul has under a year of training data. Treat its number as provisional.
- The "bad case" line is not a guarantee. In the backtest (fresh 6 AM reading, with weather) the real value stayed
  under it roughly 87% to 95% of the time, depending on the town. We call it the bad case, not a 95% bound.
- The full scores for every case, including the chance models, are in `out_v3/summary.csv` and
  `out_v3/<station>/metrics.json`. The model weights are not in the repository (about 300 MB); `train.py`
  recreates them.

## Where the dirty air comes from

`make_evidence.py` counts, over every winter morning in the archive, how often each wind direction came with one
of the dirtiest hours. It is plain counting, with the dirtiest hours judged against the same month and hour so a
dirty month's usual wind does not look like a source.

- **Visakhapatnam:** wind from the west to north-west came with 13% of the dirtiest hours but blows only 8% of
  the time. That is 1.75 times as often (range 1.34 to 2.16), and the same sector came out on top in every resample.
- **Asansol, Rourkela, Angul:** no direction stands out. At Rourkela and Angul, 62% and 58% of the dirtiest hours
  came in nearly still air, when pollution stays where it is made.

This shows a direction, not proof, and it names no one. The API serves the table at `/towns/{id}/evidence`; the
dashboard does not draw it yet.

## Built on AWS

| Service | What it does here |
|---|---|
| Lambda (container image) | The runner: one tick for every town. Code, models, town files and replay data are in the image. |
| EventBridge | Calls the runner every 15 minutes. |
| DynamoDB | One table for everything: readings, runs, the day's call, alerts sent, subscribers, accuracy. |
| Lambda (zip) + Function URL | The API the dashboard reads. It answers from the table and computes nothing, so it stays small and starts fast. |
| ECR | Holds the runner image. |
| S3 | Holds the build files for the image. |
| Amplify Hosting | Serves the dashboard. |

One command sets all of it up and can be run again safely:

    python deploy_aws.py --today-is 2024-12-07

Before the image is pushed, the script runs one real tick for every town *inside* it. If a model fails to load or
a town fails, nothing is pushed.

A note on how the image gets built. Our AWS account was created for this event and was still under AWS's
new-account verification on the last day, which blocks CodeBuild and CloudShell, and Docker was not installed on
the laptop we deployed from. So the image is built by a GitHub Actions workflow (`.github/workflows/build-runner.yml`) that runs the
same build script and pushes to ECR. `python deploy_aws.py --status` shows what is deployed and whether the
schedule is on.

Alerts go out on Telegram. SMS to Indian numbers needs TRAI DLT registration, which is the production path and
not something a four-day event allows.

## Live

- Dashboard:  https://main.dad6cxz1027iv.amplifyapp.com
- API: https://kdjfx4q26zcoy7xgb63c5wge3m0ywzsu.lambda-url.us-east-1.on.aws/towns

The dashboard always has a real morning to show: 7 December 2024 was replayed into the table through the same code
path the scheduled Lambda runs. The scheduled runner adds new runs on top of that every 15 minutes.

Outside roughly 5:00 to 7:35 AM there is no route advice to give, so the dashboard shows that morning's call
instead and says so.

## Run it yourself

    pip install lightgbm pandas pytest boto3 "moto[dynamodb]"
    python -m pytest                                   # 126 tests; those that need data skip without it

    python fetch_data.py                               # download the archive (options at the top of the file)
    python train.py --station site_1418 site_5675 site_260 site_5939 --final
    python make_towns.py

    python replay.py --town towns/asansol.json --suggest            # mornings worth looking at
    python replay.py --town towns/asansol.json --at "2024-12-07 06:30"
    python serve.py --day 2024-12-07                   # the API on http://localhost:8000

    cd frontend
    npm install
    npm run dev                                        # paste http://localhost:8000 into the API box

`BACKEND.md` describes the pipeline, the API and the run document in detail.

## What is in the repository

    fetch_data.py  check_data.py  train.py     data and training
    core/                                      the pipeline: forecast, exposure, departure, alert, store, API
    towns/  route_points.json                  one file per town; auto_routes.py turns points into walking routes
    replay.py  serve.py                        replay a past morning; local API for dashboard work
    make_towns.py  make_routes.py  make_evidence.py  probe_live.py
    lambda_function.py  Dockerfile  pack_models.py  deploy_aws.py     AWS
    frontend/                                  dashboard: React, Vite, Leaflet
    tests/                                     126 tests
    out_v3/                                    backtest scores and model settings (weights left out)

## What was done before the event

We asked the organisers what was allowed and followed their answer: work before 8 October is fine as long as
nothing is committed and no repository exists before then.

- **Before 8 October:** downloading the data, training the models (`train.py` is in the repository), and writing
  and testing a first version of the backend pipeline.
- **During the event:** real walking routes from OpenStreetMap, Telegram alerts end to end, the route map and the
  dashboard, the AWS deployment, and this write-up.

Every commit in this repository was made inside the event window.

## Known limits

- One station per town. Everything street-level is an estimate on top of it.
- The data is hourly. India's AQI bands are defined on 24-hour averages; we apply them to hourly values as a guide.
- When the 5-to-6 AM reading is missing, the town shows no advice that morning instead of guessing. In the packed
  two weeks that happens on a few days at Rourkela and Visakhapatnam.
- The wind is area wind from a weather reanalysis, not an instrument at the station.
- Only Asansol has mapped routes. The other three towns get the forecast and the school flag.
- The replay has data for about two weeks from the day it was deployed.

## Data and credits

- Pollutant readings: CPCB CAAQM network, through the India Air Quality Database by XKDR Forum (CC BY 4.0).
- Weather: Open-Meteo (CC BY 4.0).
- Map and routes: © OpenStreetMap contributors. Walking routes from the public OSRM foot-routing server.
