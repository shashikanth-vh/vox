# The ATLAS UI as a shippable image. Build context = deploy/ (see the compose file);
# the CONTENT lives in deploy/ui/ and this folder holds only the build plumbing — so
# replacing/wiping deploy/ui/ with a new UI build can never break the image build.
#
#   docker compose build ui && docker compose up -d ui        # rebuild + roll the UI
#
# CI equivalent:  docker build -f deploy/ui-image/Dockerfile deploy -t <registry>/prism-ui:<tag>
FROM nginx:1.27-alpine

COPY ui-image/ui-nginx.conf /etc/nginx/conf.d/default.conf
COPY ui/ /usr/share/nginx/html/ui/

# Folder docs have no business being servable.
RUN rm -f /usr/share/nginx/html/ui/README.md
