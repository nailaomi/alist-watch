FROM python:3.12-alpine
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/app/data PORT=8088
COPY app.py /app/app.py
COPY icon.py /app/icon.py
# 各网盘官方图标（App Store / 官网抓取，256px 或原尺寸）
COPY logos /app/logos
RUN mkdir -p /app/data && chmod 700 /app/data
EXPOSE 8088
HEALTHCHECK --interval=60s --timeout=6s --start-period=40s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8088/healthz',timeout=4)"
CMD ["python3", "/app/app.py"]
