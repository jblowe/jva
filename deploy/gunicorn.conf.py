# gunicorn settings for the search app on the web server (used by deploy/jva.service).
# Apache (deploy/jva.johnblowe.com.conf) proxies to this address; pick another port if
# 8007 is taken on the server:  sudo ss -ltnp | grep 8007
bind = "127.0.0.1:8007"
workers = 2                 # each keeps its own cache of counts; 2 is plenty for a small site
worker_class = "gthread"
threads = 4
timeout = 60                # the first, uncached search over everything can take a few seconds
graceful_timeout = 20
accesslog = None            # Apache logs the requests
errorlog = "-"              # to the journal: journalctl -u jva
