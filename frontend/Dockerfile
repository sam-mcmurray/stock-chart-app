# frontend/Dockerfile
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
# Ensure native esbuild matches the JS wrapper (fixes "Host version ... does not match binary version ...")
RUN npm rebuild esbuild

FROM node:22-alpine AS dev
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
EXPOSE 3000
# default dev command (can be overridden by compose)
CMD ["npm","run","dev","--","--host","0.0.0.0","--port","3000"]
