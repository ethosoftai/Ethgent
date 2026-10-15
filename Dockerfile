# Ethgent as a headless worker (python -m MarketingApp.worker): polls the agent_jobs queue and serves MCP.
# Build: docker build -t ethgent .    See docs/backend.md for running it next to your services.
#
# Layers go from "never changes" to "changes often": system packages -> Chromium -> Python packages -> code.
FROM python:3.12-slim
WORKDIR /app

# A virtual screen: pyautogui/mss want a display even when nobody looks at it.
RUN apt-get update && apt-get install -y --no-install-recommends xvfb xauth scrot python3-tk python3-dev git \
    && rm -rf /var/lib/apt/lists/*

# Chromium depends only on the playwright version, so it gets its own layer. Keep it in step with requirements.txt.
ARG PLAYWRIGHT_VERSION=1.58.0
RUN pip install --no-cache-dir playwright==${PLAYWRIGHT_VERSION} && playwright install --with-deps chromium

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import importlib.metadata as m, sys; v=m.version('playwright'); sys.exit(0 if v=='${PLAYWRIGHT_VERSION}' else 'requirements.txt pins playwright '+v+', but Chromium was installed for ${PLAYWRIGHT_VERSION}')"

COPY . .
# "default" follows SUBMODEL_MODEL_NAME from the environment instead of a model name one provider may reject.
RUN sed -i 's/^  model: gemma-4-26b-a4b-it$/  model: default/' MarketingApp/config/agents.yaml

# Workspace and config live on /data so installed packs and history survive a new container.
# Packages that installed packs need go to /data/site-packages (on PYTHONPATH; the sandbox reads it too).
ENV PYTHONUNBUFFERED=1 ETHGENT_WORKSPACE_DIR=/data/workspace ETHGENT_CONFIG_DIR=/data/config \
    PYTHONPATH=/data/site-packages DISPLAY=:99 AGENT_WORKER_MCP_HOST=0.0.0.0
EXPOSE 8091 8092
# The image's default config is copied to /data/config on first start; files already there are kept.
# A container stopped while Chrome ran leaves a profile lock; Chrome then refuses to start, so it is removed.
CMD ["sh", "-c", "mkdir -p /data/config && cp -rn /app/MarketingApp/config/. /data/config/; rm -f /tmp/.X99-lock /data/workspace/chrome_profile/Singleton*; Xvfb :99 -screen 0 1280x800x24 -nolisten tcp & sleep 2; exec python -m MarketingApp.worker"]
