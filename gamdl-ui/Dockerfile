FROM ubuntu:24.04

ENV TZ=Europe/London \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends software-properties-common \
 && add-apt-repository -y universe \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        ffmpeg \
        gpac \
        tzdata \
        wget \
        unzip \
        ca-certificates \
 && ln -sf /usr/share/zoneinfo/${TZ} /etc/localtime \
 && echo "${TZ}" > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

RUN wget -qO /tmp/bento4.zip "https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip" \
 && unzip -q /tmp/bento4.zip -d /opt \
 && mv /opt/Bento4-SDK-* /opt/bento4 \
 && ln -s /opt/bento4/bin/mp4decrypt /usr/local/bin/mp4decrypt \
 && rm /tmp/bento4.zip

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 && ln -s /opt/venv/bin/gamdl /usr/local/bin/gamdl \
 && ln -s /opt/venv/bin/uvicorn /usr/local/bin/uvicorn

# Fix gamdl's Apple Music web-token scraper (the runner shells out to this
# gamdl install). Apple reordered the WebPlayKid JWT header from {"alg",...}
# to {"typ",...}, so the base64 now starts with "eyJ0" not "eyJh" and gamdl's
# hardcoded `(?=eyJh)` lookahead stops finding the token ("Token not found in
# index.js page"). The widened class matches any JWT header start. See the
# matching patch in ../gamdl/Dockerfile.
RUN F="$(/opt/venv/bin/python -c 'import gamdl.api.apple_music_api as m; print(m.__file__)')" \
 && /opt/venv/bin/python -c "p='$F'; s=open(p).read(); n=s.replace('(?=eyJh)','(?=eyJ[A-Za-z0-9_-])'); assert n!=s, 'gamdl token regex not found — upstream changed, revisit patch'; open(p,'w').write(n)" \
 && echo "patched gamdl token regex in $F"

RUN (getent group 1001 || groupadd -g 1001 app) \
 && (getent passwd 1000 | grep -q '^ubuntu:' && userdel -r ubuntu || true) \
 && useradd -u 1000 -g 1001 -m -s /bin/bash app \
 && mkdir -p /ui-data /tmp/gamdl \
 && chown -R app:app /ui-data /tmp/gamdl

WORKDIR /srv
COPY --chown=app:app app/ /srv/app/

USER app
EXPOSE 4150
CMD ["/opt/venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4150", "--proxy-headers"]
