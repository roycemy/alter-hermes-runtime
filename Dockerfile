FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 hermes
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hermes.py safety.py directives.yaml ./
RUN mkdir -p /data /workspace && chown -R hermes:hermes /app /data /workspace
USER hermes
CMD ["python", "hermes.py"]
