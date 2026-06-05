# --- Stage 1: build the React PWA -------------------------------------------
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
# Deterministic install from the lockfile — fails loudly if it drifts, instead
# of silently resolving newer (possibly breaking) deps and killing the build.
RUN npm ci
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
# Tesseract OCR: the independent reader that gates the photo/scan lab-import path
# (server/app/services/lab_vision.py). Without it, image lab uploads degrade visibly to
# 'image_unparsed' rather than being trusted; with it, a vision value is staged only if
# Tesseract also saw it. --no-install-recommends + cleanup keeps the layer lean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
# requirements-vision.txt = pytesseract + pypdfium2 (render scanned PDFs to images for OCR).
COPY server/requirements.txt server/requirements-vision.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-vision.txt

COPY server/app ./app
# Repo-defined workflows, action recipes, and central prompt store.
COPY workflows ./workflows
COPY actions ./actions
COPY prompts.yaml ./prompts.yaml
ENV JBRAIN_WORKFLOWS_DIR=/app/workflows
ENV JBRAIN_ACTIONS_DIR=/app/actions
ENV JBRAIN_PROMPTS_FILE=/app/prompts.yaml
# Built front-end goes where FastAPI serves static files from.
COPY --from=web /web/dist ./static

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
