FROM node:22-bookworm-slim AS build

WORKDIR /app/apps/web

ENV NEXT_TELEMETRY_DISABLED=1

COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

COPY apps/web /app/apps/web
RUN npm run build


FROM node:22-bookworm-slim AS runtime

WORKDIR /app/apps/web

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

COPY --from=build /app/apps/web /app/apps/web

EXPOSE 3000

CMD ["npm", "run", "start", "--", "--hostname", "0.0.0.0", "--port", "3000"]
