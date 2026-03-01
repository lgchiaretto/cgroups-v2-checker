# Gunicorn configuration for production
bind = "0.0.0.0:8080"
workers = 2
threads = 4
timeout = 300
accesslog = "-"
errorlog = "-"
loglevel = "info"
