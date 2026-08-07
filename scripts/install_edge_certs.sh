#!/usr/bin/env bash
# Install a REAL certificate pair on the PRISM edge (compose deployment) and reload it.
#
#   scripts/install_edge_certs.sh <fullchain.pem> <privkey.pem>
#
# Works with any CA-issued pair: Let's Encrypt (certbot's fullchain.pem/privkey.pem),
# a corporate CA, or a commercial cert. The files are copied to deploy/nginx/certs/
# as tls.crt / tls.key (the paths the edge nginx reads) and the running edge container
# is reloaded with zero downtime. Re-run after every renewal — or call this from
# certbot's --deploy-hook so renewals apply themselves.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CERT_DIR="$ROOT/deploy/nginx/certs"

[[ $# -eq 2 ]] || { echo "usage: $0 <fullchain.pem> <privkey.pem>" >&2; exit 1; }
if [[ ! -f "$1" || ! -f "$2" ]]; then
  # /etc/letsencrypt/live is root-only: without sudo the files LOOK absent even when
  # they exist. Distinguish the two failures instead of guessing.
  if [[ $EUID -ne 0 && ( "$1" == /etc/letsencrypt/* || "$2" == /etc/letsencrypt/* ) ]]; then
    echo "cert or key not readable — /etc/letsencrypt is root-only; re-run with sudo:" >&2
    echo "  sudo $0 $1 $2" >&2
  else
    echo "cert or key file not found: check the paths (has certbot issued the cert yet?)" >&2
  fi
  exit 1
fi

# Sanity: the key must match the certificate, and the cert should not be expired.
# Compare PUBLIC KEYS, not RSA moduli — Let's Encrypt issues ECDSA keys by default
# now, and an RSA-only check refuses those perfectly valid pairs.
crt_pub="$(openssl x509 -pubkey -noout -in "$1" 2>/dev/null | openssl md5)"
key_pub="$(openssl pkey -pubout -in "$2" 2>/dev/null | openssl md5 || true)"
if [[ -z "$crt_pub" ]]; then
  echo "REFUSING: $1 does not parse as an X.509 certificate." >&2; exit 1
fi
if [[ -n "$key_pub" && "$crt_pub" != "$key_pub" ]]; then
  echo "REFUSING: that key does not match that certificate." >&2; exit 1
fi
openssl x509 -checkend 86400 -noout -in "$1" \
  || echo "WARNING: this certificate expires within 24h (or already has)." >&2

mkdir -p "$CERT_DIR"
install -m 644 "$1" "$CERT_DIR/tls.crt"
install -m 600 "$2" "$CERT_DIR/tls.key"
echo "Installed:"
openssl x509 -in "$CERT_DIR/tls.crt" -noout -subject -dates -ext subjectAltName || true

# Reload the running edge (the certs volume is a bind mount, so the files are already
# inside the container — nginx just needs to re-read them).
if docker ps --format '{{.Names}}' | grep -q 'nginx'; then
  docker exec "$(docker ps --format '{{.Names}}' | grep nginx | head -1)" nginx -s reload \
    && echo "Edge reloaded — the new certificate is live."
else
  echo "Edge container not running — the cert will be picked up on next start."
fi
