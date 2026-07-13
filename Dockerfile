FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sendoff ./sendoff

ENV DB_PATH=/data/sendoff.db
VOLUME ["/data"]

# Read-only dashboard + /keep endpoint (see WEB_PORT).
EXPOSE 8623

CMD ["python", "-m", "sendoff.main"]
