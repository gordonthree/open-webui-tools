FROM python:3.12-slim
# Only the Playwright *client* is needed: it attaches to the already-running Chromium over CDP, so no browser download.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 8890
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8890", "--log-level", "warning"]
