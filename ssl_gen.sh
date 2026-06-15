#!/bin/sh
set -e

CERT_DIR="/app/certs"
mkdir -p "$CERT_DIR"

if [ ! -f "$CERT_DIR/server.pem" ] || [ ! -f "$CERT_DIR/server.key" ]; then
    echo "Generating self-signed SSL certificate..."
    openssl req -x509 -newkey rsa:2048 \
        -keyout "$CERT_DIR/server.key" \
        -out "$CERT_DIR/server.pem" \
        -days 3650 -nodes \
        -subj "/CN=OceanStor-Simulator/O=Simulator/C=CN" \
        2>/dev/null
    echo "SSL certificate generated."
fi

exec "$@"
