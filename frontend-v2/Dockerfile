ARG NODE_IMAGE=node:22-slim
ARG NGINX_IMAGE=nginx:alpine

FROM ${NODE_IMAGE} AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
# Optional: pin the build id to the image tag; otherwise the build timestamp is used.
ARG VITE_BUILD_ID
ENV VITE_BUILD_ID=${VITE_BUILD_ID}
RUN npm run build

FROM ${NGINX_IMAGE}
COPY --from=builder /app/dist /usr/share/nginx/html
# Public files retain their source permissions when Vite copies them. Make
# restrictive build contexts readable by nginx's unprivileged workers.
RUN find /usr/share/nginx/html -type d -exec chmod 755 {} + \
    && find /usr/share/nginx/html -type f -exec chmod 644 {} +
ENV BACKEND_HOST=backend:8080
COPY nginx.conf /etc/nginx/templates/default.conf.template
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
