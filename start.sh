#!/bin/bash
# Pre-download TLS library if needed
python -m tls_requests.models.libraries 2>/dev/null || true
# Start the server
exec python3 api_litestar.py
