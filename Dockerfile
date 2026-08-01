# SPDX-License-Identifier: GPL-3.0-or-later
# Image autonome de l'outil « Caméras tourelles ». Tout ce dont l'outil a besoin vit ICI
# (isolation du runtime=docker) : requests pour parler aux CGI des caméras (Panasonic AW),
# la stdlib suffit pour le reste (VISCA sera de l'UDP brut).
FROM python:3.13-slim

RUN pip install --no-cache-dir requests

WORKDIR /app
COPY server.py fleet.py panel_fleet.py /app/
COPY drivers /app/drivers
COPY panels /app/panels

# Parc, identités relevées et noms de mémoires locaux persistés dans un volume monté par
# l'app sur /data (cf. docker.volume du plugin.json).
VOLUME ["/data"]

# Port HTTP interne — DOIT correspondre à docker.port du plugin.json. L'app publie ce port
# sur 127.0.0.1:<aléatoire> et proxifie /api/tools/ptz/* vers lui.
EXPOSE 8080

CMD ["python", "-u", "/app/server.py"]
