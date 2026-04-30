# Build the QUIC sidecar as a static binary, then ship from scratch.
# Built image runs as `streambed-quic-sidecar`, role chosen by SIDECAR_ROLE env.

FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod ./
# go.sum will be created/updated on first `go mod tidy`; tolerate missing.
COPY go.su[m] ./
RUN go mod download || true

COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" \
    -o /out/streambed-quic-sidecar ./cmd/streambed-quic-sidecar

FROM scratch
COPY --from=build /out/streambed-quic-sidecar /streambed-quic-sidecar
EXPOSE 4433/udp 9100/tcp
ENTRYPOINT ["/streambed-quic-sidecar"]
