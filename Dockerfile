FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/srv

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .
COPY scripts ./scripts

RUN useradd --create-home appuser && mkdir /data && chown appuser /data
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
