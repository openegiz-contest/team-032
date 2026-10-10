FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt \
    && addgroup --system mine-safety \
    && adduser --system --ingroup mine-safety --home /app mine-safety

COPY --chown=mine-safety:mine-safety app.py model.py setup_twins.py twins.json ./
COPY --chown=mine-safety:mine-safety static/ ./static/

USER mine-safety

EXPOSE 8091

CMD ["python", "app.py"]
