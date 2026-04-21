FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ARG MODE=worker

RUN apt-get update && apt-get install -y curl git && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY lib/gemini_webapi ./gemini_webapi/
RUN uv sync

COPY app/main.py ./main.py
COPY app/worker.py ./worker.py
COPY app/slot.py ./slot.py
COPY app/raw_capture_tracer.py ./raw_capture_tracer.py
COPY app/worker_events.py ./worker_events.py
COPY app/parsers ./parsers/

RUN mkdir -p /app/state/worker-events /app/envs

ENV PYTHONPATH=/app
ENV ONECLICK_MODE=${MODE}
ENV ONECLICK_STATE_DIR=/app/state

EXPOSE 8000 7860

CMD ["sh", "-c", "if [ \"$ONECLICK_MODE\" = \"worker\" ]; then exec uv run uvicorn worker:app --host 0.0.0.0 --port 7860; fi; exec uv run uvicorn main:app --host 0.0.0.0 --port 8000"]
