FROM python:3.12-slim
RUN pip install --no-cache-dir meshcore==2.3.19 httpx==0.28.1 pyyaml==6.0.2 ruamel.yaml==0.19.1 aiohttp==3.14.5 tzdata pytest==8.3.4 segno==1.6.6
WORKDIR /srv
COPY app /srv/app
USER 1000:1000
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
