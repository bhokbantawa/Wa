FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download TLS library during build
RUN python -m tls_requests.models.libraries || true

COPY . .

EXPOSE 8080

CMD ["python3", "api_litestar.py"]
