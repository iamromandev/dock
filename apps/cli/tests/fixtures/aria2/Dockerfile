FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends aria2 \
 && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["aria2c"]
