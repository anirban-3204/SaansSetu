"""
SaansSetu: put the whole backend on AWS with one command. Safe to run again; it updates what exists.

  python deploy_aws.py --today-is 2024-12-07                  deploy everything
  python deploy_aws.py --ui ../ui/dist                        host the built dashboard on Amplify (backend untouched)
  python deploy_aws.py --api-only                             only re-upload the API code (20 seconds)
  python deploy_aws.py --status                               URLs, schedule state, newest run per town
  python deploy_aws.py --pause   /   --resume                 stop / start the 15-minute schedule (pause while recording)

Run it from the backend folder (the one holding lambda_function.py, core/, train.py, replay.py).
Before the first run:
  1. aws configure                      (or set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION)
  2. python make_evidence.py
  3. python pack_models.py --days FIRST LAST     (this script tells you which days if deploy/ does not cover them)

Docker is not needed. If Docker is running on this computer the runner image is built here; if not, the build
files go to S3 and AWS CodeBuild builds, tests and pushes the image (about 5 minutes). --build local / cloud forces one.

What it makes, all named after --name (default saanssetu):
  DynamoDB table   saanssetu             pk/sk, on demand, TTL on
  IAM role         saanssetu-lambda      logs + this one table
  Lambda           saanssetu-api         zip: lambda_function.py + core/. Public Function URL. Answers the dashboard.
  ECR repo + image saanssetu-runner      code + models + towns + replay data, lightgbm, pandas
  S3 bucket        saanssetu-build-...   only when built inside AWS: the build files
  CodeBuild        saanssetu-build       only when built inside AWS: builds, tests, pushes the image
  Lambda           saanssetu-runner      the image. One tick for every town.
  EventBridge rule saanssetu-tick        rate(15 minutes) -> runner
  Amplify app      saanssetu             only with --ui

--today-is DATE sets the clock: today behaves as that past morning, tomorrow as the day after, and so on
(SAANSSETU_CLOCK_SHIFT_DAYS). Pick a morning replay.py --suggest liked. That morning is then replayed
into the table, so the dashboard has the 06:30 alert moment even if you deploy in the afternoon.

Telegram: set TELEGRAM_TOKEN in your shell before running and both functions get it. Add --set-webhook to point
the bot at the deployed API (this stops any local polling bot from receiving messages).
"""
import argparse
import base64
import gzip
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
HERE = Path(__file__).resolve().parent
RUN_AHEAD_DAYS = 14         # how long the deployed demo should keep finding data

DOCKERFILE = """\
FROM public.ecr.aws/lambda/python:3.12
# lightgbm needs the OpenMP runtime, which the base image does not ship
RUN dnf install -y libgomp && dnf clean all
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lambda_function.py train.py ${LAMBDA_TASK_ROOT}/
COPY core/ ${LAMBDA_TASK_ROOT}/core/
COPY models/ ${LAMBDA_TASK_ROOT}/models/
COPY towns/ ${LAMBDA_TASK_ROOT}/towns/
COPY saanssetu_data/ ${LAMBDA_TASK_ROOT}/saanssetu_data/
ENV SAANSSETU_MODELS=models SAANSSETU_TOWNS=towns SAANSSETU_DATA=saanssetu_data SAANSSETU_SOURCE=archive
CMD ["lambda_function.tick_handler"]
"""

# Runs inside the built image before anything is pushed: one real tick for every town, kept in memory.
SMOKE = """\
import json, sys
import pandas as pd
from core import ingest, runner, townconfig
from core.store import MemoryStore
towns, problems = townconfig.load_towns("towns")
out = runner.run_all(MemoryStore(), towns, "models", ingest.ArchiveSource("saanssetu_data"), pd.Timestamp(sys.argv[1]),
                     mode="replay", model_set="models", replay_of=sys.argv[1][:10])
print("SMOKE " + json.dumps({"towns": out, "problems": problems}))
"""


# The same script for a build inside AWS, where the exit code decides whether the image is pushed.
SMOKE_CLOUD = SMOKE + """\
bad = [t for t, r in out.items() if r["status"] == "failed"]
if bad or not out:
    print("SMOKE-FAILED " + (", ".join(bad) or "no town ran"))
    sys.exit(1)
"""

# Runs where the image is built when this computer has no Docker: AWS CodeBuild, or AWS CloudShell by hand.
BUILD_SH = """\
#!/bin/bash
# SaansSetu runner image: build it, run one real tick for every town inside it, push it only if that worked.
set -euo pipefail
cd "$(dirname "$0")"
: "${REPO_URI:=__REPO_URI__}"
: "${TAG:=__TAG__}"
: "${SMOKE_WHEN:=__WHEN__}"
REGISTRY="${REPO_URI%%/*}"
REGION="$(echo "$REGISTRY" | cut -d. -f4)"
echo "== login to $REGISTRY"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"
echo "== build"
if docker buildx version >/dev/null 2>&1; then
  docker build --platform linux/amd64 --provenance=false -t "$REPO_URI:$TAG" .
else
  docker build -t "$REPO_URI:$TAG" .
fi
echo "== one tick for every town inside the image, at $SMOKE_WHEN"
docker run --rm --entrypoint /bin/sh "$REPO_URI:$TAG" -c '__FIND_PYTHON__' "$(cat smoke.py)" "$SMOKE_WHEN"
echo "== push"
docker push "$REPO_URI:$TAG"
echo "PUSHED $REPO_URI:$TAG"
"""

BUILDSPEC = "version: 0.2\nphases:\n  build:\n    commands:\n      - bash build.sh\n"
BUILD_KEY = "runner-context/context.zip"


# sh -c FIND_PYTHON <script> <time>: runs the script with whichever Python the image has ($0 is the script, $1 the time).
FIND_PYTHON = 'for p in python3 python /var/lang/bin/python3 /var/lang/bin/python3.12; do if command -v "$p" >/dev/null 2>&1; then exec "$p" -c "$0" "$1"; fi; done; echo "no python found in the image"; exit 127'


class Stop(Exception):
    """Something the user has to fix. Printed without a traceback."""


def say(text=""):
    print(text, flush=True)


def step(n, text):
    say(f"\n[{n}] {text}")


# ---------------------------------------------------------------- local checks

def need_files(folder, image):
    wanted = ["lambda_function.py", "core"] + (["train.py", "deploy/models", "deploy/towns", "deploy/saanssetu_data"] if image else [])
    missing = [w for w in wanted if not (folder / w).exists()]
    if any(m.startswith("deploy") for m in missing):
        raise Stop("deploy/ is missing or incomplete. Run:\n    python make_evidence.py\n    python pack_models.py --days FIRST LAST")
    if missing:
        raise Stop(f"Not found in {folder}: {', '.join(missing)}.\nRun this script from the backend folder (the one with lambda_function.py, core/ and train.py).")


def data_span(folder):
    """{station file: (first day, last day)} of the packed pollutant files."""
    spans = {}
    for path in sorted((folder / "deploy" / "saanssetu_data").glob("aq_*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            rows = [line.split(",", 1)[0] for line in f if line[:1].isdigit()]
        if rows:
            spans[path.name] = (rows[0][:10], rows[-1][:10])
    return spans


def check_data(folder, today_is):
    """The packed archive must hold the days the clock will land on, plus the history a forecast looks back on."""
    spans = data_span(folder)
    if not spans:
        raise Stop("deploy/saanssetu_data/ has no aq_*.csv.gz files. Run pack_models.py with --days.")
    # The model's inputs look back 7 days. pack_models.py packs 17, but a station with a gap at the start of that
    # window still works, so only a file that clearly cannot serve the day stops the deploy.
    first_needed = (today_is - timedelta(days=8)).isoformat()
    want_last = (today_is + timedelta(days=RUN_AHEAD_DAYS)).isoformat()
    fix = f"    python pack_models.py --days {today_is.isoformat()} {want_last}"
    for name, (first, last) in spans.items():
        if first > first_needed or last < today_is.isoformat():
            raise Stop(f"{name} holds {first} to {last}, which does not cover {today_is} and the week before it.\nRe-pack, then run this again:\n{fix}")
    runs_until = min(last for _, last in spans.values())
    return runs_until, fix


def check_towns(folder):
    """The Lambda runs the town files that were copied into deploy/ when pack_models.py last ran. If towns/ has
    been edited since (new routes, say), the Lambda would run the old ones: that stops the deploy.
    Returns notes about town files that still say their geometry is a placeholder."""
    def read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    packed = {t.get("town_id"): t for t in map(read, sorted((folder / "deploy" / "towns").glob("*.json"))) if t}
    for path in sorted((folder / "towns").glob("*.json")):
        town = read(path)
        if town and town.get("town_id") in packed and packed[town["town_id"]] != town:
            raise Stop(f"towns/{path.name} has changed since deploy/ was packed, so AWS would run the old version.\n"
                       f"Run pack_models.py again (same --days), then this script.")
    notes = []
    for town_id, town in packed.items():
        marked = [k for k in ("school", "home_area") if (town.get(k) or {}).get("placeholder")]
        if "placeholder" in str(town.get("geometry_note", "")).lower():
            marked.append("routes")
        if marked:
            notes.append(f"{town_id}: the town file still marks its {', '.join(marked)} as placeholder. "
                         f"Do not quote route savings from placeholder geometry.")
    return notes


def lightgbm_pin(folder):
    """The lightgbm version that trained the models, from config.json. numpy is held below 2 for older lightgbm."""
    versions = set()
    for cfg in (folder / "deploy" / "models").glob("*/config.json"):
        v = json.loads(cfg.read_text(encoding="utf-8")).get("lightgbm")
        if v:
            versions.add(v)
    if len(versions) != 1:
        return ["lightgbm", "pandas", "numpy"], f"no single lightgbm version recorded ({sorted(versions) or 'none'}); using the newest"
    v = versions.pop()
    try:
        old = tuple(int(x) for x in v.split(".")[:2]) < (4, 4)
    except ValueError:
        old = False
    return [f"lightgbm=={v}", "pandas<2.3" if old else "pandas", "numpy<2" if old else "numpy"], f"lightgbm {v}, as trained"


def docker(*args, stdin=None, capture=False):
    """Runs docker. Returns stdout when capture is set. Raises Stop with the last lines of output on failure."""
    try:
        r = subprocess.run(["docker", *args], input=stdin, text=True, encoding="utf-8", errors="replace",
                           stdout=subprocess.PIPE if capture else None, stderr=subprocess.STDOUT if capture else None)
    except FileNotFoundError:
        raise Stop("Docker is not installed or not on PATH. Run again without --build local and the image is built inside AWS instead.")
    if r.returncode != 0:
        tail = "\n".join((r.stdout or "").strip().splitlines()[-15:])
        raise Stop(f"docker {args[0]} failed.\n{tail}")
    return r.stdout or ""


def docker_available():
    """True when a Docker daemon answers on this computer."""
    try:
        return subprocess.run(["docker", "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def build_context(folder, pins):
    ctx = folder / ".aws_build"
    if ctx.exists():
        shutil.rmtree(ctx)
    ctx.mkdir()
    for name in ("lambda_function.py", "train.py"):
        shutil.copy(folder / name, ctx / name)
    shutil.copytree(folder / "core", ctx / "core", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("models", "towns", "saanssetu_data"):
        shutil.copytree(folder / "deploy" / name, ctx / name)
    # Written as bytes so the line endings stay Unix on Windows too: a shell script with Windows endings does not run.
    (ctx / "requirements.txt").write_bytes(("\n".join(pins) + "\n").encode("utf-8"))
    (ctx / "Dockerfile").write_bytes(DOCKERFILE.encode("utf-8"))
    return ctx


def zip_context(ctx, repo_uri, tag, when):
    """The build folder as one zip, with the build script and the in-image test added."""
    script = (BUILD_SH.replace("__REPO_URI__", repo_uri).replace("__TAG__", tag).replace("__WHEN__", when)
              .replace("__FIND_PYTHON__", FIND_PYTHON))
    (ctx / "build.sh").write_bytes(script.encode("utf-8"))
    (ctx / "smoke.py").write_bytes(SMOKE_CLOUD.encode("utf-8"))
    path = ctx.parent / ".aws_build.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(q for q in ctx.rglob("*") if q.is_file()):
            z.write(f, f.relative_to(ctx).as_posix())
    return path


def build_image(ctx, tag):
    args = ["build", "--platform", "linux/amd64", "-t", tag, str(ctx)]
    try:
        docker(args[0], "--provenance=false", *args[1:])     # Lambda refuses images that carry build attestations
    except Stop as e:
        if "provenance" not in str(e):
            raise
        docker(*args)                                        # an older docker without that flag does not add them anyway


def smoke_test(tag, when):
    """One tick for every town inside the image, in memory. Stops the deploy if any town fails."""
    out = docker("run", "--rm", "--platform", "linux/amd64", "--entrypoint", "/bin/sh", tag, "-c", FIND_PYTHON, SMOKE, when, capture=True)
    line = next((l for l in out.splitlines() if l.startswith("SMOKE ")), None)
    if line is None:
        raise Stop("The image built but could not run a tick:\n" + "\n".join(out.strip().splitlines()[-15:]))
    result = json.loads(line[6:])
    for town_id, r in result["towns"].items():
        say(f"      {town_id:15} {r['status']:9} alert {r['alert']} ({r['reason']})")
    for name, why in result["problems"].items():
        say(f"      town file {name}: {why}")
    bad = [t for t, r in result["towns"].items() if r["status"] == "failed"]
    if bad or not result["towns"]:
        what = f"{', '.join(bad)} failed" if bad else "no town ran (check deploy/towns/)"
        raise Stop(f"Inside the image, {what}. Fix that before deploying; nothing was pushed.")
    return result


def api_zip(folder):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(folder / "lambda_function.py", "lambda_function.py")
        for path in sorted((folder / "core").rglob("*.py")):
            z.write(path, path.relative_to(folder).as_posix())
    return buf.getvalue()


# ---------------------------------------------------------------- AWS pieces

def clients(region):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise Stop("boto3 is not installed. Run: pip install -U boto3")
    session = boto3.session.Session(region_name=region)
    region = session.region_name or "ap-south-1"
    cfg = Config(region_name=region, read_timeout=330, retries={"max_attempts": 3})
    make = lambda name: session.client(name, config=cfg)
    try:
        account = make("sts").get_caller_identity()["Account"]
    except Exception as e:
        raise Stop(f"AWS credentials are not working ({type(e).__name__}: {str(e)[:160]}).\nRun `aws configure` with an access key, then try again.")
    return {"region": region, "account": account, "session": session, "config": cfg,
            **{n: make(n) for n in ("dynamodb", "iam", "lambda", "ecr", "events", "amplify", "s3", "codebuild", "logs")}}


def ensure_table(aws, table):
    db = aws["dynamodb"]
    try:
        db.describe_table(TableName=table)
        return "exists"
    except db.exceptions.ResourceNotFoundException:
        pass
    db.create_table(TableName=table, BillingMode="PAY_PER_REQUEST",
                    KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                    AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}])
    db.get_waiter("table_exists").wait(TableName=table)
    db.update_time_to_live(TableName=table, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})
    return "created"


def ensure_role(aws, name, table):
    iam = aws["iam"]
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        made = False
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Description="SaansSetu Lambdas: logs and one DynamoDB table")["Role"]["Arn"]
        made = True
    iam.attach_role_policy(RoleName=name, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    policy = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow",
        "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query",
                   "dynamodb:BatchWriteItem", "dynamodb:DescribeTable"],
        "Resource": f"arn:aws:dynamodb:{aws['region']}:{aws['account']}:table/{table}"}]}
    iam.put_role_policy(RoleName=name, PolicyName="table", PolicyDocument=json.dumps(policy))
    return arn, made


def function_env(aws, name):
    try:
        return aws["lambda"].get_function_configuration(FunctionName=name).get("Environment", {}).get("Variables", {})
    except aws["lambda"].exceptions.ResourceNotFoundException:
        return None


def ensure_function(aws, name, role, code, env, **settings):
    """Creates the function or updates its code and settings. Environment variables already on the function are kept
    unless this run sets them. A brand-new role can take a few seconds before Lambda may use it, so creation retries."""
    lam = aws["lambda"]
    existing = function_env(aws, name)
    if existing is None:
        package = {"PackageType": "Image"} if "ImageUri" in code else {"Runtime": "python3.12", "Handler": settings.pop("Handler")}
        settings.pop("Handler", None)
        for attempt in range(12):
            try:
                lam.create_function(FunctionName=name, Role=role, Code=code, Environment={"Variables": env},
                                    Architectures=["x86_64"], **package, **settings)
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "assume" not in str(e).lower() or attempt == 11:
                    raise
                time.sleep(5)
        lam.get_waiter("function_active_v2").wait(FunctionName=name)
        return "created"
    settings.pop("Handler", None)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    lam.update_function_code(FunctionName=name, **code)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    lam.update_function_configuration(FunctionName=name, Role=role, Environment={"Variables": {**existing, **env}}, **settings)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    return "updated"


def allow(aws, function, statement, **permission):
    lam = aws["lambda"]
    try:
        lam.add_permission(FunctionName=function, StatementId=statement, **permission)
    except lam.exceptions.ResourceConflictException:
        pass   # already there


def ensure_url(aws, function):
    """A public Function URL. CORS is NOT switched on here: the API code already sends the CORS headers, and
    sending them twice makes browsers refuse the answer."""
    lam = aws["lambda"]
    try:
        url = lam.get_function_url_config(FunctionName=function)["FunctionUrl"]
    except lam.exceptions.ResourceNotFoundException:
        url = lam.create_function_url_config(FunctionName=function, AuthType="NONE")["FunctionUrl"]
    allow(aws, function, "PublicUrl", Action="lambda:InvokeFunctionUrl", Principal="*", FunctionUrlAuthType="NONE")
    try:   # since October 2025 a public Function URL needs this second permission as well
        allow(aws, function, "PublicUrlInvoke", Action="lambda:InvokeFunction", Principal="*", InvokedViaFunctionUrl=True)
    except Exception as e:
        if "InvokedViaFunctionUrl" not in str(e):
            raise
        raise Stop("Your boto3 is too old to make a public Function URL. Run: pip install -U boto3   then run this again.")
    return url.rstrip("/")


def ensure_repo(aws, name):
    ecr = aws["ecr"]
    try:
        uri = ecr.describe_repositories(repositoryNames=[name])["repositories"][0]["repositoryUri"]
    except ecr.exceptions.RepositoryNotFoundException:
        uri = ecr.create_repository(repositoryName=name, imageTagMutability="MUTABLE")["repository"]["repositoryUri"]
    try:    # Lambda must be allowed to pull the image. It usually adds this itself; setting it here removes the doubt.
        ecr.set_repository_policy(repositoryName=name, policyText=json.dumps({"Version": "2012-10-17", "Statement": [{
            "Sid": "LambdaECRImageRetrievalPolicy", "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]}]}))
    except Exception:
        pass
    return uri


def image_uri(aws, repo_name, repo_uri, tag):
    """repo@digest for a pushed tag, or None while it is not there yet."""
    ecr = aws["ecr"]
    try:
        digest = ecr.describe_images(repositoryName=repo_name, imageIds=[{"imageTag": tag}])["imageDetails"][0]["imageDigest"]
    except ecr.exceptions.ImageNotFoundException:
        return None
    return f"{repo_uri}@{digest}"


def newest_image(aws, repo_name, repo_uri):
    images = [i for page in aws["ecr"].get_paginator("describe_images").paginate(repositoryName=repo_name) for i in page["imageDetails"]]
    if not images:
        raise Stop("--skip-build: there is no image in the repository yet. Run once without --skip-build.")
    return f"{repo_uri}@{max(images, key=lambda i: i['imagePushedAt'])['imageDigest']}"


def ensure_bucket(aws, bucket):
    s3 = aws["s3"]
    try:
        s3.head_bucket(Bucket=bucket)
        return
    except Exception as e:
        if getattr(e, "response", {}).get("Error", {}).get("Code") not in ("404", "NoSuchBucket", "NotFound"):
            raise
    if aws["region"] == "us-east-1":
        s3.create_bucket(Bucket=bucket)
    else:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": aws["region"]})


def ensure_build_role(aws, name, bucket, repo_name):
    """The role CodeBuild runs as: write its own logs, read the uploaded build files, push to the one repository."""
    iam = aws["iam"]
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "codebuild.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        made = False
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Description="SaansSetu: builds the runner image")["Role"]["Arn"]
        made = True
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"], "Resource": f"arn:aws:s3:::{bucket}/*"},
        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"], "Resource": f"arn:aws:s3:::{bucket}"},
        {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
        {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
                                         "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
         "Resource": f"arn:aws:ecr:{aws['region']}:{aws['account']}:repository/{repo_name}"}]}
    iam.put_role_policy(RoleName=name, PolicyName="build", PolicyDocument=json.dumps(policy))
    return arn, made


def start_codebuild(aws, project, bucket, role_arn, env):
    """Makes the build project if it is not there, and starts one build. Returns the build id."""
    cb = aws["codebuild"]
    if not cb.batch_get_projects(names=[project])["projects"]:
        settings = dict(name=project, description="SaansSetu: builds, tests and pushes the runner image",
                        source={"type": "S3", "location": f"{bucket}/{BUILD_KEY}", "buildspec": BUILDSPEC},
                        artifacts={"type": "NO_ARTIFACTS"},
                        environment={"type": "LINUX_CONTAINER", "image": "aws/codebuild/amazonlinux2-x86_64-standard:5.0",
                                     "computeType": "BUILD_GENERAL1_MEDIUM", "privilegedMode": True},
                        serviceRole=role_arn, timeoutInMinutes=30)
        for attempt in range(12):       # a brand-new role takes a few seconds before CodeBuild may use it
            try:
                cb.create_project(**settings)
                break
            except cb.exceptions.InvalidInputException as e:
                if attempt == 11 or not any(w in str(e).lower() for w in ("assume", "authorized", "role")):
                    raise
                time.sleep(5)
    overrides = [{"name": k, "value": v, "type": "PLAINTEXT"} for k, v in env.items()]
    return cb.start_build(projectName=project, environmentVariablesOverride=overrides)["build"]["id"]


def build_log(aws, build):
    """Every line the build printed, or [] when the log cannot be read. Never raises."""
    logs = build.get("logs") or {}
    group, stream = logs.get("groupName"), logs.get("streamName")
    if not group or not stream:
        return []
    lines, token = [], None
    try:
        for _ in range(200):
            args = {"logGroupName": group, "logStreamName": stream, "startFromHead": True}
            if token:
                args["nextToken"] = token
            page = aws["logs"].get_log_events(**args)
            lines += [e["message"].rstrip("\r\n") for e in page["events"]]
            if not page["events"] or page.get("nextForwardToken") == token:
                break
            token = page["nextForwardToken"]
    except Exception:
        pass
    return lines


def show_smoke(lines):
    """Prints each town's result from the in-image test. Returns True when the test line was found."""
    line = next((l for l in lines if l.startswith("SMOKE {")), None)
    if line is None:
        return False
    result = json.loads(line[6:])
    for town_id, r in result["towns"].items():
        say(f"      {town_id:15} {r['status']:9} alert {r['alert']} ({r['reason']})")
    for name, why in result["problems"].items():
        say(f"      town file {name}: {why}")
    return True


def watch_codebuild(aws, project, build_id):
    """Waits for the build, saying which phase it is in. Stops with the end of the log if it fails."""
    cb, phase, build = aws["codebuild"], None, {}
    for _ in range(240):                                 # 40 minutes at most
        build = cb.batch_get_builds(ids=[build_id])["builds"][0]
        if build.get("currentPhase") != phase:
            phase = build.get("currentPhase")
            say(f"      {phase.lower().replace('_', ' ') if phase else 'waiting'} ...")
        if build["buildStatus"] != "IN_PROGRESS":
            break
        time.sleep(10)
    lines = build_log(aws, build)
    say("    one tick for every town inside the image:")
    if not show_smoke(lines) and build["buildStatus"] == "SUCCEEDED":
        say("      (the build log could not be read here; the build only succeeds when every town ran)")
    if build["buildStatus"] != "SUCCEEDED":
        tail = "\n".join("      " + l for l in lines[-25:]) or "      (no log lines could be read)"
        where = f"CodeBuild console > Build projects > {project} > newest build"
        raise Stop(f"The build inside AWS ended as {build['buildStatus']}. Nothing was pushed unless the log ends with PUSHED.\n{tail}\nFull log: {where}")


def cloudshell_build(aws, repo_name, repo_uri, tag, bucket):
    """When CodeBuild cannot be used: the same build, started by hand in AWS CloudShell. Waits for the image to appear."""
    say(f"""
    Do this once, in the browser:
      1. Open the AWS console, region {aws['region']}, and click the CloudShell icon (>_) in the top bar.
      2. Paste this one line and press Enter:

aws s3 cp s3://{bucket}/{BUILD_KEY} ctx.zip && rm -rf ctx && unzip -q ctx.zip -d ctx && bash ctx/build.sh

      3. Leave this window open. It carries on by itself when the line 'PUSHED ...' appears there.
""")
    for i in range(360):                                 # an hour at most
        uri = image_uri(aws, repo_name, repo_uri, tag)
        if uri:
            return uri
        if i and i % 12 == 0:
            say(f"      still waiting for the image ({i // 6} min) ...")
        time.sleep(10)
    raise Stop("No image arrived within an hour. When the CloudShell build has printed PUSHED, run this again with --skip-build.")


def cloud_build(aws, n, ctx, repo_uri, tag, when):
    """Builds, tests and pushes the runner image inside AWS. Returns repo@digest."""
    bucket = f"{n['table']}-build-{aws['account']}-{aws['region']}"
    say("    packing the build files ...")
    path = zip_context(ctx, repo_uri, tag, when)
    say(f"    uploading {path.stat().st_size / 1e6:.0f} MB to s3://{bucket}/{BUILD_KEY} ...")
    ensure_bucket(aws, bucket)
    aws["s3"].upload_file(str(path), bucket, BUILD_KEY)
    try:
        role_arn, made = ensure_build_role(aws, n["build_role"], bucket, n["repo"])
        if made:
            time.sleep(10)
        build_id = start_codebuild(aws, n["build"], bucket, role_arn, {"REPO_URI": repo_uri, "TAG": tag, "SMOKE_WHEN": when})
    except Exception as e:
        say(f"    CodeBuild could not be started here ({type(e).__name__}: {str(e)[:160]}).")
        return cloudshell_build(aws, n["repo"], repo_uri, tag, bucket)
    say(f"    building in AWS CodeBuild (project {n['build']}); this takes about 5 minutes:")
    watch_codebuild(aws, n["build"], build_id)
    uri = image_uri(aws, n["repo"], repo_uri, tag)
    if not uri:
        raise Stop("The build reported success but the image is not in the repository. Run this again.")
    return uri


def push_image(aws, repo_name, repo_uri, local_tag, tag):
    ecr = aws["ecr"]
    auth = ecr.get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    docker("login", "--username", user, "--password-stdin", repo_uri.split("/")[0], stdin=password, capture=True)
    docker("tag", local_tag, f"{repo_uri}:{tag}")
    docker("push", f"{repo_uri}:{tag}")
    return image_uri(aws, repo_name, repo_uri, tag)


def ensure_schedule(aws, rule, function_arn, function):
    ev = aws["events"]
    arn = ev.put_rule(Name=rule, ScheduleExpression="rate(15 minutes)", State="ENABLED", Description="SaansSetu: one tick for every town")["RuleArn"]
    ev.put_targets(Rule=rule, Targets=[{"Id": "runner", "Arn": function_arn}])
    allow(aws, function, "EventBridgeTick", Action="lambda:InvokeFunction", Principal="events.amazonaws.com", SourceArn=arn)
    return arn


def invoke_tick(aws, function, payload=None):
    r = aws["lambda"].invoke(FunctionName=function, Payload=json.dumps(payload or {}).encode())
    body = json.loads(r["Payload"].read().decode("utf-8") or "null")
    if r.get("FunctionError"):
        raise Stop(f"The runner Lambda raised an error:\n{json.dumps(body, indent=1)[:1500]}\nLogs: CloudWatch > Log groups > /aws/lambda/{function}")
    return body


def get_json(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "saanssetu-deploy"}), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def seed_morning(folder, table, day, region):
    """Replays the clock's own morning into the table through the live path, so the day's call and the
    06:30 alert moment exist even when the deploy happens later in the day."""
    cmd = [sys.executable, "replay.py", "--towns", "towns", "--day", day, "--park", "06:30", "--table", table]
    env = {**os.environ, "AWS_DEFAULT_REGION": region, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(cmd, cwd=folder, env=env)
    return r.returncode == 0, " ".join(cmd[1:])


def set_webhook(token, url, secret):
    q = urllib.parse.urlencode({"url": f"{url}/telegram/webhook", "secret_token": secret})
    return get_json(f"https://api.telegram.org/bot{token}/setWebhook?{q}")


def with_config_tag(html):
    """index.html with <script src="/config.js"> placed before every other script. Left alone if already there."""
    if "config.js" in html:
        return html
    tag = '<script src="/config.js"></script>'
    low = html.lower()
    at = low.find("<head")
    if at >= 0:
        at = low.find(">", at) + 1
        return html[:at] + "\n    " + tag + html[at:]
    at = low.find("<script")
    return html[:at] + tag + "\n" + html[at:] if at >= 0 else tag + "\n" + html


def host_ui(aws, name, ui_dir, api_url):
    """Uploads a built static site to Amplify Hosting. A config.js holding the API address is added to the upload
    and index.html is made to load it, so the hosted page reads window.SAANSSETU_API. Your files are not changed."""
    amp, ui_dir = aws["amplify"], Path(ui_dir)
    if not (ui_dir / "index.html").exists():
        raise Stop(f"{ui_dir} has no index.html. Point --ui at the built site (for Vite that is dist/, for CRA build/).")
    apps = [a for page in amp.get_paginator("list_apps").paginate() for a in page["apps"] if a["name"] == name]
    app = apps[0] if apps else amp.create_app(name=name, platform="WEB", customRules=[
        {"source": "</^[^.]+$|\\.(?!(css|gif|ico|jpg|js|png|txt|svg|woff|woff2|ttf|map|json|webp|geojson)$)([^.]+$)/>", "target": "/index.html", "status": "200"}])["app"]
    try:
        amp.get_branch(appId=app["appId"], branchName="main")
    except amp.exceptions.NotFoundException:
        amp.create_branch(appId=app["appId"], branchName="main")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(p for p in ui_dir.rglob("*") if p.is_file()):
            if path.relative_to(ui_dir).as_posix() not in ("config.js", "index.html"):
                z.write(path, path.relative_to(ui_dir).as_posix())
        z.writestr("config.js", f"window.SAANSSETU_API = {json.dumps(api_url)};\n")
        z.writestr("index.html", with_config_tag((ui_dir / "index.html").read_text(encoding="utf-8")))
    job = amp.create_deployment(appId=app["appId"], branchName="main")
    req = urllib.request.Request(job["zipUploadUrl"], data=buf.getvalue(), method="PUT", headers={"Content-Type": "application/zip"})
    urllib.request.urlopen(req, timeout=300).read()
    amp.start_deployment(appId=app["appId"], branchName="main", jobId=job["jobId"])
    for _ in range(90):
        status = amp.get_job(appId=app["appId"], branchName="main", jobId=job["jobId"])["job"]["summary"]["status"]
        if status in ("SUCCEED", "FAILED", "CANCELLED"):
            break
        time.sleep(4)
    if status != "SUCCEED":
        raise Stop(f"Amplify deployment ended as {status}. Open the Amplify console for the reason.")
    return f"https://main.{app['defaultDomain']}"


# ---------------------------------------------------------------- commands

def names(prefix):
    return {"table": prefix, "role": f"{prefix}-lambda", "api": f"{prefix}-api", "runner": f"{prefix}-runner",
            "repo": f"{prefix}-runner", "rule": f"{prefix}-tick", "app": prefix,
            "build": f"{prefix}-build", "build_role": f"{prefix}-codebuild"}


def status(aws, n):
    lam, ev = aws["lambda"], aws["events"]
    say(f"Account {aws['account']}, region {aws['region']}")
    try:
        url = lam.get_function_url_config(FunctionName=n["api"])["FunctionUrl"].rstrip("/")
    except lam.exceptions.ResourceNotFoundException:
        say("Not deployed yet (no API function URL).")
        return
    say(f"API       {url}")
    try:
        rule = ev.describe_rule(Name=n["rule"])
        say(f"Schedule  {rule['ScheduleExpression']}  {rule['State']}")
    except ev.exceptions.ResourceNotFoundException:
        say("Schedule  not created")
    env = function_env(aws, n["runner"]) or {}
    shift = env.get("SAANSSETU_CLOCK_SHIFT_DAYS")
    if shift:
        say(f"Clock     {shift} days back: today behaves as {(datetime.now(IST).date() - timedelta(days=int(shift))).isoformat()}")
    try:
        for t in get_json(url + "/towns")["towns"]:
            now = t.get("now") or {}
            say(f"  {t['town_id']:15} {t['status']:9} updated {t['updated_at'][:16]}  PM2.5 {now.get('pm25')}  PT {t.get('school_flag')}  alert today: {t.get('alert_today')}")
    except Exception as e:
        say(f"  /towns did not answer: {type(e).__name__}: {str(e)[:200]}")


def deploy_ui_only(args):
    """--ui with no clock given and a backend already up: host the dashboard and touch nothing else."""
    n = names(args.name)
    step(1, "AWS account")
    aws = clients(args.region)
    say(f"    account {aws['account']}, region {aws['region']}")
    try:
        url = aws["lambda"].get_function_url_config(FunctionName=n["api"])["FunctionUrl"].rstrip("/")
    except aws["lambda"].exceptions.ResourceNotFoundException:
        return False
    say(f"    backend already deployed ({url}); leaving it as it is")
    step(2, "Dashboard on Amplify Hosting")
    site = host_ui(aws, n["app"], args.ui, url)
    out = HERE / "aws_outputs.json"
    saved = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"api": url, "region": aws["region"], "table": n["table"]}
    out.write_text(json.dumps({**saved, "api": url, "site": site}, indent=1), encoding="utf-8")
    say(f"\nDashboard:  {site}\nAPI:        {url}")
    return True


def deploy(args):
    folder = HERE
    n = names(args.name)
    if args.ui and not (args.today_is or args.shift or args.api_only or args.skip_build) and deploy_ui_only(args):
        return
    image = not args.api_only
    need_files(folder, image)
    step(1, "AWS account")
    aws = clients(args.region)
    say(f"    account {aws['account']}, region {aws['region']}")

    old_env = function_env(aws, n["runner"]) or {}
    shift = today_is = None
    if image:
        real_today = datetime.now(IST).date()
        if args.today_is:
            today_is = date.fromisoformat(args.today_is)
            shift = (real_today - today_is).days
        elif args.shift:
            shift = args.shift
        elif old_env.get("SAANSSETU_CLOCK_SHIFT_DAYS"):
            shift = int(old_env["SAANSSETU_CLOCK_SHIFT_DAYS"])
        else:
            raise Stop("Say which past morning today should behave as: --today-is 2024-12-07   (pick one from `python replay.py --town towns/asansol.json --suggest`)")
        if shift <= 0:
            raise Stop("--today-is must be a past date.")
        today_is = real_today - timedelta(days=shift)
        for note in check_towns(folder):
            say(f"    NOTE {note}")
        runs_until, fix = check_data(folder, today_is)
        left = (date.fromisoformat(runs_until) - today_is).days
        say(f"    clock: today behaves as {today_is} ({shift} days back). Packed data runs to {runs_until}: the live demo keeps finding readings for {left} more day(s).")
        if left < 3:
            say(f"    NOTE: that is short if judges open the site later this week. To extend:\n{fix}")

    token = os.environ.get("TELEGRAM_TOKEN") or (function_env(aws, n["api"]) or {}).get("TELEGRAM_TOKEN")
    secret = os.environ.get("TELEGRAM_SECRET") or (function_env(aws, n["api"]) or {}).get("TELEGRAM_SECRET")
    if token and not secret:
        secret = secrets.token_urlsafe(24)
    telegram = {k: v for k, v in (("TELEGRAM_TOKEN", token), ("TELEGRAM_SECRET", secret)) if v}

    step(2, "DynamoDB table and IAM role")
    say(f"    table {n['table']}: {ensure_table(aws, n['table'])}")
    role, made = ensure_role(aws, n["role"], n["table"])
    say(f"    role {n['role']}: {'created' if made else 'exists'}")
    if made:
        time.sleep(10)   # a new role is not usable for a few seconds

    step(3, "API Lambda (zip) and its public URL")
    how = ensure_function(aws, n["api"], role, {"ZipFile": api_zip(folder)}, {"SAANSSETU_TABLE": n["table"], **telegram},
                          Handler="lambda_function.http_handler", Timeout=20, MemorySize=512)
    url = ensure_url(aws, n["api"])
    say(f"    {n['api']}: {how}\n    {url}")

    if image:
        step(4, "Runner image: build, test one tick inside it, push")
        pins, why = lightgbm_pin(folder)
        say(f"    {why}")
        where = args.build if args.build != "auto" else ("local" if docker_available() else "cloud")
        tag, when = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"), f"{today_is.isoformat()}T06:30"
        if where == "local":
            ctx = build_context(folder, pins)
            local_tag = f"{n['repo']}:local"
            if not args.skip_build:
                docker("version", capture=True)
                build_image(ctx, local_tag)
            say(f"    one tick inside the image at {today_is} 06:30:")
            smoke_test(local_tag, when)
            repo_uri = ensure_repo(aws, n["repo"])
            runner_image = push_image(aws, n["repo"], repo_uri, local_tag, tag)
        else:
            repo_uri = ensure_repo(aws, n["repo"])
            if args.skip_build:
                runner_image = newest_image(aws, n["repo"], repo_uri)
            else:
                if args.build == "auto":
                    say("    Docker is not running on this computer, so the image is built inside AWS. Nothing to install.")
                runner_image = cloud_build(aws, n, build_context(folder, pins), repo_uri, tag, when)
        say(f"    image {runner_image}")

        step(5, "Runner Lambda and the 15-minute schedule")
        env = {"SAANSSETU_TABLE": n["table"], "SAANSSETU_CLOCK_SHIFT_DAYS": str(shift), "SAANSSETU_SOURCE": "archive", **telegram}
        how = ensure_function(aws, n["runner"], role, {"ImageUri": runner_image}, env, Timeout=300, MemorySize=2048)
        arn = aws["lambda"].get_function_configuration(FunctionName=n["runner"])["FunctionArn"]
        ensure_schedule(aws, n["rule"], arn, n["runner"])
        say(f"    {n['runner']}: {how}; {n['rule']}: every 15 minutes")

        step(6, "First tick on AWS (the first one loads two weeks of readings, so it can take a minute)")
        result = invoke_tick(aws, n["runner"])
        say(f"    clock {result['now']}, mode {result['mode']}")
        for town_id, r in result["towns"].items():
            say(f"      {town_id:15} {r['status']:9} alert {r['alert']} ({r['reason']})")
        if any(r["status"] == "failed" for r in result["towns"].values()):
            say(f"    A town failed on AWS though it ran inside the image. Logs: CloudWatch > /aws/lambda/{n['runner']}")

        if not args.no_seed:
            step(7, f"Replay the morning of {today_is} into the table (the 06:30 alert moment for the dashboard; a few minutes)")
            ok, cmd = seed_morning(folder, n["table"], today_is.isoformat(), aws["region"])
            if not ok:
                say(f"    That did not finish. The deploy itself is fine; run it by hand:\n      python {cmd}")

    if token and args.set_webhook:
        step(8, "Telegram webhook")
        say(f"    {set_webhook(token, url, secret).get('description')}")

    site = None
    if args.ui:
        step(9, "Dashboard on Amplify Hosting")
        site = host_ui(aws, n["app"], args.ui, url)
        say(f"    {site}")

    step("done", "Check")
    try:
        towns = get_json(url + "/towns")["towns"]
        say(f"    GET {url}/towns -> {len(towns)} town(s): " + ", ".join(f"{t['town_id']} ({t['status']})" for t in towns))
    except Exception as e:
        say(f"    GET {url}/towns failed: {type(e).__name__}: {str(e)[:200]}")
    (folder / "aws_outputs.json").write_text(json.dumps({"api": url, "site": site, "region": aws["region"], "table": n["table"],
                                                          "today_is": today_is.isoformat() if today_is else None}, indent=1), encoding="utf-8")
    say(f"\nAPI URL for the dashboard:  {url}")
    if site:
        say(f"Dashboard:                  {site}")
    say("Saved to aws_outputs.json.  Before recording: python deploy_aws.py --pause   (and --resume after)")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Deploy SaansSetu to AWS.")
    ap.add_argument("--today-is", help="the past date today should behave as, e.g. 2024-12-07")
    ap.add_argument("--shift", type=int, help="or give the clock shift in days directly")
    ap.add_argument("--region", default=None, help="AWS region (default: your configured one, else ap-south-1)")
    ap.add_argument("--name", default="saanssetu", help="prefix for everything created")
    ap.add_argument("--ui", help="folder of the built dashboard (holds index.html) to host on Amplify. On its own it only hosts the dashboard.")
    ap.add_argument("--api-only", action="store_true", help="only update the API Lambda")
    ap.add_argument("--skip-build", action="store_true", help="reuse the image built last time")
    ap.add_argument("--build", choices=["auto", "local", "cloud"], default="auto",
                    help="where the runner image is built: this computer's Docker, or inside AWS (CodeBuild). Default: Docker if it is running, else AWS.")
    ap.add_argument("--no-seed", action="store_true", help="do not replay the morning into the table")
    ap.add_argument("--set-webhook", action="store_true", help="point the Telegram bot at the deployed API")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--pause", action="store_true", help="stop the 15-minute schedule")
    ap.add_argument("--resume", action="store_true", help="start it again")
    args = ap.parse_args()
    try:
        if args.status or args.pause or args.resume:
            aws, n = clients(args.region), names(args.name)
            if args.pause:
                aws["events"].disable_rule(Name=n["rule"])
                say("Schedule paused. The table keeps showing the last run until you --resume.")
            if args.resume:
                aws["events"].enable_rule(Name=n["rule"])
                say("Schedule running again: a tick every 15 minutes.")
            if args.status:
                status(aws, n)
            return
        deploy(args)
    except Stop as e:
        sys.exit(f"\nSTOPPED: {e}")


if __name__ == "__main__":
    main()
