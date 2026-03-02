# Gunicorn configuration for production
bind = "0.0.0.0:8080"
workers = 1       # Single worker — scan state lives in-memory; threads handle concurrency
threads = 4
timeout = 300
accesslog = "-"
errorlog = "-"
loglevel = "info"
