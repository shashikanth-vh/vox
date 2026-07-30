# Edge TLS certificates

This directory is mounted read-only into the NGINX edge at `/etc/nginx/certs`. It must contain:

    tls.crt   # certificate chain
    tls.key   # private key (never committed — see ../../../.gitignore)

Generate a self-signed pair for local / demo use:

    scripts/gen_dev_certs.sh

nginx will refuse to start without these files. For production, mount a CA-issued pair at the same
two paths (or let cert-manager provide them in Kubernetes) — no nginx.conf change is needed.
