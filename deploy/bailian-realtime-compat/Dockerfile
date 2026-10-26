FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir websockets==15.0.1
COPY bridge.py /app/bridge.py
USER 65534:65534
HEALTHCHECK --interval=20s --timeout=3s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"
CMD ["python", "-B", "bridge.py"]
