# Built by `dock plugin build dev.dock.download` (or on first isolated start).
# The binary is compiled inside the image, so no Rust toolchain is needed on the host.
FROM rust:1-slim AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked

FROM debian:stable-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/download /plugin/download
