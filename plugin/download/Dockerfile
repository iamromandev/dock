# Built by `dock plugin build dev.dock.download` (or on first isolated start).
# The binary is compiled inside the image, so no Go toolchain is needed on the host.
FROM golang:1-alpine AS build
WORKDIR /src
COPY go.mod main.go ./
RUN CGO_ENABLED=0 go build -o /out/download .

FROM alpine
COPY --from=build /out/download /plugin/download
