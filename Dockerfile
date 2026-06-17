FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssl tgt \
    && rm -rf /var/lib/apt/lists/*

COPY oceanstor_simulator.py /app/
COPY entrypoint.sh /app/

WORKDIR /app
RUN chmod +x entrypoint.sh && mkdir -p /app/volumes

EXPOSE 8088 3260

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "oceanstor_simulator.py", "--port", "8088"]
