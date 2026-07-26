FROM node:24.13.1-bookworm-slim@sha256:a81a03dd965b4052269a57fac857004022b522a4bf06e7a739e25e18bce45af2 AS base

ENV PNPM_HOME=/pnpm
ENV PATH="$PNPM_HOME:$PATH"
WORKDIR /app
RUN apt-get update \
  && apt-get install --yes --no-install-recommends openssl \
  && rm -rf /var/lib/apt/lists/* \
  && corepack enable

FROM base AS dependencies
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm/store \
  corepack pnpm@11.17.0 install --store-dir /pnpm/store --frozen-lockfile --ignore-scripts

FROM dependencies AS build
COPY . .
RUN DATABASE_URL=postgresql://build:build@127.0.0.1:5432/build ./node_modules/.bin/prisma generate
RUN ./node_modules/.bin/nest build

FROM dependencies AS development
ENV NODE_ENV=development
COPY --chown=node:node . .
RUN DATABASE_URL=postgresql://build:build@127.0.0.1:5432/build ./node_modules/.bin/prisma generate \
  && mkdir -p /app/dist \
  && chown node:node /app \
  && chown -R node:node /app/src/generated /app/dist
USER node
CMD ["./node_modules/.bin/nest", "start", "--watch"]

FROM dependencies AS test
COPY . .
RUN DATABASE_URL=postgresql://build:build@127.0.0.1:5432/build ./node_modules/.bin/prisma generate
CMD ["./node_modules/.bin/vitest", "run"]

FROM build AS migrate
ENV HOME=/tmp
USER node
CMD ["./node_modules/.bin/prisma", "migrate", "deploy"]

FROM base AS production-dependencies
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm/store \
  corepack pnpm@11.17.0 install --store-dir /pnpm/store --prod --frozen-lockfile --ignore-scripts

FROM base AS production
ENV NODE_ENV=production
COPY --from=production-dependencies /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY package.json ./
USER node
EXPOSE 3000
CMD ["node", "dist/main.js"]
