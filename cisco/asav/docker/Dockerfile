FROM ghcr.io/srl-labs/vrnetlab-base:0.2.1

RUN apt-get update -qy \
 && apt-get install -y \
    python3 \
 && rm -rf /var/lib/apt/lists/*

ARG VERSION
ENV VERSION=${VERSION}
ARG IMAGE
COPY $IMAGE* /
COPY *.py /

EXPOSE 22 161/udp 830 5000 10000-10099

HEALTHCHECK CMD ["/healthcheck.py"]
ENTRYPOINT ["/launch.py"]
