# --- Stage 1: build the React PWA -------------------------------------------
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install
COPY web/ ./
RUN npm run build

# --- Stage 2: Python API serving the API + built PWA ------------------------
FROM python:3.12-slim AS api
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# The git commit this image was built from (for update detection). Passed by
# install.sh / update.sh / the updater via the GIT_SHA build arg.
ARG GIT_SHA=""
ENV JBRAIN_BUILD_REF=$GIT_SHA

WORKDIR /app
COPY server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server/app ./app
# Repo-defined workflows, ingested into the DB on boot.
COPY workflows ./workflows
ENV JBRAIN_WORKFLOWS_DIR=/app/workflows
# Built front-end goes where FastAPI serves static files from.
COPY --from=web /web/dist ./static

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
