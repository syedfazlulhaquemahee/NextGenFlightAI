web: gunicorn app:app --bind 0.0.0.0:$PORT --worker-class gthread --workers 2 --threads 4 --timeout 300 --keep-alive 5
voice: uvicorn voice_service.main:app --host 0.0.0.0 --port ${VOICE_PROXY_PORT:-8781}
