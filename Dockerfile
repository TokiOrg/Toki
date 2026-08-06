FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY team_energy_service ./team_energy_service

RUN python -m pip install --no-cache-dir .

CMD ["python", "-m", "team_energy_service"]
