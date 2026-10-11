# Runner Lambda image. A starting point: this file has not been built or run yet.
# Build after `python pack_models.py --days FIRST LAST` has made deploy/.
FROM public.ecr.aws/lambda/python:3.12

# lightgbm needs the OpenMP runtime, which the base image does not ship
RUN dnf install -y libgomp && dnf clean all

COPY requirements-lambda.txt .
RUN pip install --no-cache-dir -r requirements-lambda.txt

COPY lambda_function.py train.py ${LAMBDA_TASK_ROOT}/
COPY core/ ${LAMBDA_TASK_ROOT}/core/
COPY deploy/models/ ${LAMBDA_TASK_ROOT}/models/
COPY deploy/towns/ ${LAMBDA_TASK_ROOT}/towns/
COPY deploy/saanssetu_data/ ${LAMBDA_TASK_ROOT}/saanssetu_data/

ENV SAANSSETU_MODELS=models SAANSSETU_TOWNS=towns SAANSSETU_DATA=saanssetu_data SAANSSETU_SOURCE=archive

CMD ["lambda_function.tick_handler"]
