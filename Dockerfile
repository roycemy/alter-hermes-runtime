FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 hermes
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hermes.py safety.py directives.yaml me.md projects.md decisions.md proposals.md ./
COPY inbox ./inbox
RUN mkdir -p /data && chown -R hermes:hermes /app /data
USER hermes
CMD ["python", "hermes.py"]
