FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:22-bookworm-slim AS runtime
ENV NODE_ENV=production PORT=8082 HOSTNAME=0.0.0.0
RUN groupadd --system console && useradd --system --gid console --home /app console
WORKDIR /app
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
USER console
EXPOSE 8082
CMD ["node", "server.js"]
