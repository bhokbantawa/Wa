# Shopify Checker API — Robyn Edition

Converted from Flask → **Robyn** for Railway Hobby Plan.

## Railway Hobby Plan Config
| Resource | Per Replica |
|----------|------------|
| vCPU     | 8          |
| RAM      | 8 GB       |
| Replicas | Up to 5    |

## Robyn Worker Config
| Setting    | Value | Reason |
|------------|-------|--------|
| `--processes` | 7 | 8 vCPU − 1 for OS/network overhead |
| `--workers`   | 4 | 4 async workers per process |
| Total slots   | 28 | 7 × 4 simultaneous requests |

## Endpoint

### `GET /shopify`
| Param    | Required | Description |
|----------|----------|-------------|
| `site`   | ✅ | Target Shopify store URL |
| `cc`     | ✅ | Card in format `CC\|MM\|YYYY\|CVV` |
| `proxy`  | ❌ | Proxy string (optional) |
| `variant`| ❌ | Shopify variant ID (optional) |

### `GET /health`
Returns `{"status": "ok"}` — use for Railway health checks.

## Local Run
```bash
pip install -r requirements.txt
python api.py
```

## Railway Deploy
1. Push to GitHub
2. Connect repo in Railway
3. Railway auto-reads `Procfile` — no extra config needed
4. Set `PORT` env var if needed (default: 8080)
