# Draait op een Raspberry Pi 5 (arm64) en op amd64 voor het testen.
FROM python:3.12-slim

# Draaien als niet-root: de centrale heeft nergens root voor nodig, en dit is
# een dienst die permanent aan het netwerk hangt.
RUN useradd --create-home --uid 1000 ajax

WORKDIR /app

# Eerst alleen de metadata kopiëren, zodat een codewijziging niet elke keer
# alle afhankelijkheden opnieuw laat bouwen — op een Pi scheelt dat minuten.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config.example.yaml ./
COPY tools ./tools
COPY tests/fake_hub.py ./tests/fake_hub.py

RUN mkdir -p /app/data && chown -R ajax:ajax /app
USER ajax

# 10000: SIA DC-09 van de hub. 8080: het dashboard.
EXPOSE 10000/tcp 10000/udp 8080/tcp

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "ajaxcentral.main"]
