#!/usr/bin/env bash
# Generate a SELF-SIGNED TLS certificate for the PRISM edge (local / demo only).
#
# The edge terminates TLS, so every client — browser, PRISM UI, Postman, curl — talks HTTPS and
# nothing inside the perimeter is reachable in plaintext from outside. These certs are NOT for
# production: replace them with a real CA-issued pair (or cert-manager in Kubernetes).
#
#   scripts/gen_dev_certs.sh              # writes deploy/nginx/certs/{tls.crt,tls.key}
#   scripts/gen_dev_certs.sh --force      # overwrite existing certs
#
# The cert carries Subject Alternative Names for localhost + 127.0.0.1 + the compose service name,
# so it validates for every way you reach the edge. It is a CA-signing-capable self-signed cert,
# which lets you TRUST it locally (see docs/WSL_DEPLOY.md) instead of disabling verification.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CERT_DIR="${CERT_DIR:-$ROOT/deploy/nginx/certs}"
DAYS="${DAYS:-825}"
CN="${CN:-localhost}"

if [[ -f "$CERT_DIR/tls.crt" && "${1:-}" != "--force" ]]; then
  echo "Certs already exist at $CERT_DIR (use --force to regenerate):"
  openssl x509 -in "$CERT_DIR/tls.crt" -noout -subject -dates -ext subjectAltName
  exit 0
fi

command -v openssl >/dev/null || { echo "openssl not found — install it first." >&2; exit 1; }
mkdir -p "$CERT_DIR"

# SANs must cover every host used to reach the edge, or TLS verification fails even with a
# trusted cert. Add your LAN IP / hostname here if you hit the stack from another machine.
cat >"$CERT_DIR/openssl.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions    = v3
prompt             = no

[dn]
C  = IN
O  = Evam Finance
OU = PRISM
CN = $CN

[v3]
subjectAltName   = @san
basicConstraints = critical, CA:TRUE
keyUsage         = critical, digitalSignature, keyCertSign
extendedKeyUsage = serverAuth

[san]
DNS.1 = localhost
DNS.2 = nginx
DNS.3 = prism.local
IP.1  = 127.0.0.1
IP.2  = ::1
EOF

# Reaching the stack from ANOTHER machine (e.g. Postman on the host, PRISM in a VM)? The cert
# must carry that address too, or TLS verification fails no matter what is trusted:
#   EXTRA_SANS="IP:192.168.44.128" scripts/gen_dev_certs.sh --force
#   EXTRA_SANS="IP:192.168.44.128,DNS:prism-vm" ...   # comma-separated, IP: or DNS: prefixed
dns_n=4; ip_n=3
IFS=',' read -ra _sans <<<"${EXTRA_SANS:-}"
for san in "${_sans[@]:-}"; do
  case "$san" in
    DNS:*) echo "DNS.$dns_n = ${san#DNS:}" >>"$CERT_DIR/openssl.cnf"; dns_n=$((dns_n+1));;
    IP:*)  echo "IP.$ip_n = ${san#IP:}"    >>"$CERT_DIR/openssl.cnf"; ip_n=$((ip_n+1));;
    "") ;;
    *) echo "EXTRA_SANS entries must start with DNS: or IP: (got '$san')" >&2; exit 1;;
  esac
done

openssl req -x509 -newkey rsa:2048 -sha256 -days "$DAYS" -nodes \
  -keyout "$CERT_DIR/tls.key" -out "$CERT_DIR/tls.crt" \
  -config "$CERT_DIR/openssl.cnf" 2>/dev/null

chmod 600 "$CERT_DIR/tls.key"
chmod 644 "$CERT_DIR/tls.crt"

echo "Self-signed edge certificate written to $CERT_DIR:"
openssl x509 -in "$CERT_DIR/tls.crt" -noout -subject -dates -ext subjectAltName
cat <<'EOF'

Next:
  1. docker compose -f deploy/compose/docker-compose.yml up --build
  2. Reach the edge over TLS:   https://localhost:8443/healthz
     (curl needs -k, or --cacert deploy/nginx/certs/tls.crt, until you trust the cert)
  3. Postman: Settings -> General -> "SSL certificate verification" OFF for self-signed certs.
EOF
