FROM python:3.12-slim

# ffmpeg para M4A/MP3 + herramientas para instalar Deno y el provider de POT
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip git ca-certificates \
    && curl -fsSL https://deno.land/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

ENV DENO_BIN=/root/.deno/bin/deno
ENV PATH="/root/.deno/bin:${PATH}"

# bgutil POT provider (genera los tokens 'proof-of-origin' que YouTube exige
# a las IPs de datacenter como las de Render)
RUN git clone --single-branch --branch 2.0.0 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /root/bgutil-ytdlp-pot-provider \
    && cd /root/bgutil-ytdlp-pot-provider/server \
    && /root/.deno/bin/deno install --allow-scripts=npm:canvas --frozen

# Pre-compila el grafo del servidor POT en el build para que arranque al
# instante en runtime (el primer 'deno run' tardaría ~2 min transpilando).
COPY warmup_pot.sh /tmp/warmup_pot.sh
RUN chmod +x /tmp/warmup_pot.sh && /tmp/warmup_pot.sh || true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

CMD ["/app/start.sh"]