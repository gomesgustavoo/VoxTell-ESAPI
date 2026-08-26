# VoxTell landing — static nginx, no build step.
#
#   sudo docker build -t voxtell/landing:0.1.0 landing
#   sudo docker save voxtell/landing:0.1.0 | sudo k3s ctr images import -
#
# BUMP THE TAG ON EVERY REBUILD. imagePullPolicy is IfNotPresent and these images
# are local-only, so the kubelet keeps serving the cached layer under a reused tag.
#
# There is deliberately no Node toolchain in here. The whole point of a separate
# landing image is that marketing copy can change without an npm install, a Vite
# build, or a rollout of the signed-in SPA.
#
# The generator scripts (gen-hero-contours.py) and the token source are NOT copied:
# their outputs live in assets/ and are committed, so the image stays a pure
# artifact of what is actually served.

FROM nginx:1.27-alpine AS serve

COPY nginx.conf /etc/nginx/nginx.conf

COPY index.html install.html privacy.html terms.html /usr/share/nginx/html/
COPY assets/ /usr/share/nginx/html/assets/
COPY download/ /usr/share/nginx/html/download/

# Fail the build rather than the deploy if a generated asset was never produced —
# the page degrades silently otherwise (the hero just never draws, the vocabulary
# search never populates), which is the hardest kind of regression to notice.
RUN set -eu; \
    for f in assets/tokens.v2.css assets/styles.v2.css assets/main.v2.js \
             assets/hero.v2.js assets/hero-contours.v2.json assets/prompts.v1.json \
             assets/hero-slice.v2.webp; do \
      test -s "/usr/share/nginx/html/$f" || { echo "MISSING OR EMPTY: $f" >&2; exit 1; }; \
    done; \
    for f in Geist-latin.woff2 GeistMono-latin.woff2; do \
      test -s "/usr/share/nginx/html/assets/fonts/$f" \
        || { echo "MISSING FONT: $f" >&2; exit 1; }; \
    done; \
    nginx -t -c /etc/nginx/nginx.conf 2>&1 | tail -2

EXPOSE 8080
USER 101
CMD ["nginx", "-g", "daemon off;"]
