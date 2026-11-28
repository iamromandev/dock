FROM python:3.15.2-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY main.py plugin.toml /plugin/
COPY transcribecore/ /plugin/transcribecore/
COPY dock_plugin/ /plugin/dock_plugin/
