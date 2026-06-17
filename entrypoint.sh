#!/bin/sh
set -e

CERT_DIR="${CERT_DIR:-/app/certs}"
mkdir -p "$CERT_DIR"
if [ ! -f "$CERT_DIR/server.pem" ]; then
    openssl req -x509 -newkey rsa:2048 \
        -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.pem" \
        -days 3650 -nodes \
        -subj "/CN=OceanStor-Simulator/O=Simulator/C=CN"
fi

if [ "${OCEANSTOR_ENABLE_ISCSI:-true}" = "true" ]; then
    mkdir -p /app/volumes
    tgtd
    sleep 1
fi

exec "$@"
