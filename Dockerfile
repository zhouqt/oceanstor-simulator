FROM docker.io/library/python:3.12-alpine

RUN apk add  openssl

COPY oceanstor_simulator.py /app/
COPY ssl_gen.sh /app/

WORKDIR /app
RUN chmod +x ssl_gen.sh

EXPOSE 8088

ENTRYPOINT ["/app/ssl_gen.sh"]
CMD ["python3", "oceanstor_simulator.py", "--port", "8088"]
