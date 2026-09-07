# 🎧 MusicPowerBot

Bot de Telegram para buscar y descargar **música de cualquier artista, país y género del mundo** con la mejor calidad de audio posible.

- 🔎 **Búsqueda global**: consulta la iTunes Store de más de 20 países y, si no hay resultados, cae en MusicBrainz (catálogo abierto mundial). Encuentra desde lo más mainstream hasta artistas locales/underground de cualquier rincón.
- 📥 **Descarga por enlace**: pásale un enlace de **YouTube** (o Spotify, SoundCloud, Deezer, Vimeo y miles de webs más vía yt-dlp) y extrae el audio.
- ⚡ **Calidad máxima**: baja el audio nativo de YouTube (M4A/AAC) sin perder calidad, o convierte a **MP3 320 kbps**.
- 🎨 **Interfaz bonita**: botones, paginación de resultados, selección de calidad, vista previa de 30 s, carátulas y estadísticas.
- ▶️ Vista previa de 30 segundos de Apple Music.
- 🖼 Carátula del álbum en cada canción descargada.

## ⚠️ Importante: el token mostrado en el chat

El token que compartiste quedó expuesto en esta conversación. **Revócalo ya** y genera uno nuevo:

1. Abre [@BotFather](https://t.me/BotFather) en Telegram.
2. Envía `/revoke` y elige tu bot → invalida el token actual.
3. Envía `/token` y copia el **nuevo** token en `.env`.

## 🚀 Instalación (local)

```bash
cd music_bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Necesitas ffmpeg en el sistema
sudo apt install -y ffmpeg        # Debian/Ubuntu

# Crea tu archivo de configuración con el token NUEVO
cp .env.example .env
# edita .env → pon BOT_TOKEN=…

.venv/bin/python bot.py
```

## 🐳 Con Docker

```bash
docker build -t musicpowerbot .
docker run -d --restart unless-stopped \
  -v "$(pwd)/.env:/app/.env" \
  musicpowerbot
```

## 📚 Cómo se usa

| Acción | Mensaje |
|---|---|
| Buscar por artista/canción | `/search Bad Bunny Monaco` o escribe directamente el nombre |
| Descargar un enlace | `/link https://www.youtube.com/watch?v=…` |
| Ayuda | `/help` |
| Estadísticas | `/stats` |

Dentro de una búsqueda puedes elegir **Mejor calidad (M4A)**, **MP3 320 kbps** o escuchar una **vista previa**.

## 🛠 Configuración (`.env`)

| Variable | Descripción | Default |
|---|---|---|
| `BOT_TOKEN` | Token de BotFather | — (obligatorio) |
| `OWNER_ID` | Tu ID de Telegram (opcional) | `0` |
| `WORKERS` | Descargas simultáneas | `2` |
| `SEARCH_RESULTS` | Resultados por búsqueda | `12` |
| `MAX_UPLOAD_MB` | Máx. por canción (límite Telegram ~50 MB) | `45` |
| `ITUNES_COUNTRIES` | Tiendas iTunes consultadas (catálogo mundial) | lista larga |

## ⚖️ Nota legal

Úsalo solo para música que tengas derecho a descargar o para contenido con licencia abierta. El bot describe el contenido y respeta los límites de cada plataforma.