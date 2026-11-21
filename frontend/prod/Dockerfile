# --- build stage ---
FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build   # creates /app/dist

# --- serve stage ---
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
# minimal nginx config for single-page app routing (optional)
RUN printf 'server { \
  listen 80; \
  server_name _; \
  root /usr/share/nginx/html; \
  location / { try_files $uri /index.html; } \
}\n' > /etc/nginx/conf.d/default.conf
EXPOSE 80


# YAML ADD ON

# frontend:
#     build:
#       context: ./frontend
#       dockerfile: Dockerfile
#     container_name: stockapp_frontend
#     ports:
#       - "3000:80"              # http://localhost:3000
#     depends_on:
#       - api
#     restart: unless-stopped