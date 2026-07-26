# syntax=docker/dockerfile:1

FROM ghcr.io/voidzero-dev/vite-plus:0.2.6 AS dependencies

WORKDIR /app

COPY --chown=vp:vp package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc .node-version ./
RUN vp install --frozen-lockfile

FROM dependencies AS development

COPY --chown=vp:vp . .

EXPOSE 5173

CMD ["vp", "dev", "--host", "0.0.0.0", "--port", "5173"]

FROM development AS build

ARG VITE_API_BASE_URL
RUN --mount=type=tmpfs,target=/app/node_modules/.vite-temp \
  if [ -z "${VITE_API_BASE_URL:-}" ]; then unset VITE_API_BASE_URL; fi \
  && vp build

FROM nginxinc/nginx-unprivileged:1.31.3-alpine3.24 AS runtime

COPY --chown=101:101 nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --chown=101:101 --from=build /app/dist /usr/share/nginx/html

USER 101:101

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=6 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
