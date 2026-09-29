FROM python:3.12-slim
WORKDIR /app
RUN groupadd --gid 10001 bot && useradd --uid 10001 --gid bot --no-create-home bot \
    && mkdir /app/data && chown bot:bot /app/data
COPY --chown=bot:bot bot.py diagnosis.py content.json /app/
USER bot
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
