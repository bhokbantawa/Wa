FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-warm the TLS .so so it doesn't try to download at runtime
RUN python3 -c "from tls_requests.models.libraries import TLSLibrary; lib = TLSLibrary.load(); print('TLS lib loaded:', lib)"

# Set path so tls_requests never tries to download
ENV TLS_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/tls_requests/bin/tls-client-xgo-1.13.1-linux-amd64.so

COPY core.py .
COPY api.py .

EXPOSE 8080

CMD ["python", "api.py"]
