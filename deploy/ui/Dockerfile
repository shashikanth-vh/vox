# The ATLAS UI as a shippable image. Build context = deploy/ui — whatever is in this
# folder (the SPA build output, or the single-file prototype as index.html) is baked
# into an nginx that serves it under /ui, mirroring the edge layout exactly, so the
# edge proxies /ui/ here without any path rewriting.
#
#   docker compose build ui && docker compose up -d ui        # rebuild + roll the UI
#
# CI can equally build/tag/push this image and the compose file can point at the tag.
FROM nginx:1.27-alpine

COPY ui-nginx.conf /etc/nginx/conf.d/default.conf
COPY . /usr/share/nginx/html/ui

# The build plumbing has no business being servable.
RUN rm -f /usr/share/nginx/html/ui/Dockerfile \
          /usr/share/nginx/html/ui/ui-nginx.conf \
          /usr/share/nginx/html/ui/.dockerignore
