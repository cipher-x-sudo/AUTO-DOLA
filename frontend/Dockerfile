FROM node:22-alpine AS build
WORKDIR /app
ARG VITE_API_BASE=http://localhost:8000
ARG VITE_OUTPUT_LABEL=Downloads/AUTO-DOLA
ENV VITE_API_BASE=$VITE_API_BASE
ENV VITE_OUTPUT_LABEL=$VITE_OUTPUT_LABEL
COPY package*.json ./
RUN npm ci --no-audit --fetch-retries=5 --fetch-retry-mintimeout=20000 --fetch-retry-maxtimeout=120000
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
