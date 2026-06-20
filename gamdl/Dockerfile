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
        cron \
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

RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir "gamdl==2.9.3" \
 && ln -s /opt/venv/bin/gamdl /usr/local/bin/gamdl

# Fix gamdl's Apple Music web-token scraper. Apple reordered the WebPlayKid
# JWT header from {"alg",...} to {"typ",...}, so the base64 now starts with
# "eyJ0" not "eyJh"; gamdl's hardcoded `(?=eyJh)` lookahead stops finding the
# token ("Token not found in index.js page"). The widened class matches any
# JWT header start, so it survives future field reorderings. Still present in
# gamdl 3.7.x, so upgrading does not fix it.
RUN F="$(/opt/venv/bin/python -c 'import gamdl.api.apple_music_api as m; print(m.__file__)')" \
 && /opt/venv/bin/python -c "p='$F'; s=open(p).read(); n=s.replace('(?=eyJh)','(?=eyJ[A-Za-z0-9_-])'); assert n!=s, 'gamdl token regex not found — upstream changed, revisit patch'; open(p,'w').write(n)" \
 && echo "patched gamdl token regex in $F"

RUN (getent group 1001 || groupadd -g 1001 gamdl) \
 && (getent passwd 1000 | grep -q '^ubuntu:' && userdel -r ubuntu || true) \
 && useradd -u 1000 -g 1001 -m -s /bin/bash gamdl \
 && mkdir -p /var/log/gamdl /downloads /config \
 && chown -R gamdl:gamdl /var/log/gamdl

COPY run.sh /usr/local/bin/run.sh
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY crontab /etc/cron.d/gamdl
RUN chmod 0755 /usr/local/bin/run.sh /usr/local/bin/entrypoint.sh \
 && chmod 0644 /etc/cron.d/gamdl \
 && touch /var/log/gamdl/cron.log /var/log/gamdl/run.log \
 && chown gamdl:gamdl /var/log/gamdl/cron.log /var/log/gamdl/run.log

CMD ["/usr/local/bin/entrypoint.sh"]
