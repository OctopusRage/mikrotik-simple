FROM python:3.13-alpine

RUN apk add --no-cache openssh-client

WORKDIR /app
COPY server.py index.html ./

# Listen on all interfaces inside the container; keep aliases + known_hosts
# in the /data volume.
ENV BIND=0.0.0.0 \
    PORT=8088 \
    DATA_DIR=/data \
    SSH_KNOWN_HOSTS=/data/known_hosts \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8088
CMD ["python3", "server.py"]
