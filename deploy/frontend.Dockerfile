FROM node:22-alpine AS build
WORKDIR /frontend
COPY web/frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY web/frontend ./
RUN npm run build
FROM caddy:2-alpine
COPY --from=build /frontend/dist /srv/web
COPY deploy/Caddyfile /etc/caddy/Caddyfile
