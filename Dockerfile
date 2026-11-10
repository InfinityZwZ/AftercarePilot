ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock.txt ./
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock.txt \
    && python -m pip check \
    && python -c "import pathlib, shutil, tzdata; shutil.copytree(pathlib.Path(tzdata.__file__).parent / 'zoneinfo', '/usr/share/zoneinfo', dirs_exist_ok=True)" \
    && groupadd --gid 10001 aftercare \
    && useradd --uid 10001 --gid 10001 --create-home aftercare \
    && mkdir /app/.local && chown aftercare:aftercare /app/.local
COPY --chown=10001:10001 src/ ./src/
COPY --chown=10001:10001 scripts/ ./scripts/
COPY --chown=10001:10001 alembic/ ./alembic/
COPY --chown=10001:10001 alembic.ini ./
USER 10001:10001
EXPOSE 8000 8501
CMD ["python", "-m", "scripts.start_api"]

FROM runtime AS verification
COPY --chown=10001:10001 tests/ ./tests/
ENV AFTERCARE_VERIFICATION=1
CMD ["python", "-m", "scripts.start_api", "--verification"]
