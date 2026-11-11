# Development image for the web client (Vite dev server). Multi-arch (arm64/amd64).
FROM node:24-alpine
WORKDIR /web
# Direct dependencies are pinned exactly in package.json. If package-lock.json is
# present (recommended: commit it), `npm ci` gives a fully locked install.
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; \
    else npm install --no-audit --no-fund; fi
COPY . .
RUN npx tsc --noEmit
EXPOSE 5173
CMD ["npm", "run", "dev"]
