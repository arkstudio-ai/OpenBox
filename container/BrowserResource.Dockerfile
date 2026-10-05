FROM python:3.12-slim

# This image intentionally contains no Action Server, Bash API, extension
# relay, workspace tools, provider credentials or native-desktop client.
RUN apt-get update && apt-get install -y --no-install-recommends chromium chromium-sandbox
RUN pip install --no-cache-dir 'fastapi>=0.115.0' 'uvicorn[standard]>=0.32.0'
RUN useradd -M -U -u 1100 -s /usr/sbin/nologin obx-browser && mkdir -p /data/browser-resource
COPY browser_resource.py browser_pipe.py browser_pipe_launcher.py browser_isolation.py /opt/browser_resource/

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
ENTRYPOINT ["python", "/opt/browser_resource/browser_resource.py"]
# Resource identity, automation owner, distinct browser UID/GID, and the
# backend-only API key must be supplied by the private-runtime provisioner.
