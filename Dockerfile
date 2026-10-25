# Touchline web service: fan app, overlay, match API, live channels and the verified crew.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY engine/pyproject.toml engine/pyproject.toml
COPY engine/touchline_engine engine/touchline_engine
COPY services/live/pyproject.toml services/live/pyproject.toml
COPY services/live/touchline_live services/live/touchline_live
RUN pip install ./engine ./services/live

COPY web web
COPY broadcasts broadcasts
COPY schema schema
# The showcase matches ship as generated, so recorded broadcasts line up frame for frame.
# Any seed listed here that is missing gets generated during the build.
COPY data/matches data/matches
ARG SEEDS="7 8 11"
RUN for s in $SEEDS; do d=data/matches/TL-$(printf %06d $s); \
      [ -f "$d/tracking.npz" ] || python -m touchline_engine generate --seed $s --out data/matches; done

ENV TOUCHLINE_DATA=/app/data/matches TOUCHLINE_BROADCASTS=/app/broadcasts TOUCHLINE_WEB=/app/web
# Live voice stays off unless TOUCHLINE_VOICE=1 is set on the app. Its clips are cached here.
ENV TOUCHLINE_VOICE_CACHE=/app/data/voice
EXPOSE 8080
CMD ["uvicorn", "touchline_live.server:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
