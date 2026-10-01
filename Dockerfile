FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py metronome.py bigfiles.py ./

# A failing test fails the image build, so nothing gets deployed
FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
RUN python -m pytest -q tests && touch /tests-passed

FROM base
COPY --from=test /tests-passed /tests-passed
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 120 main:app
