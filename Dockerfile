FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 安裝 Node.js 22、Mosquitto MQTT Broker 與基礎工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    mosquitto \
    mosquitto-clients \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1. 安裝 Python 依賴
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[web]"

# 2. 安裝前端與 Copilot Runtime 依賴並打包 React 3D 客廳
COPY web/package.json web/package-lock.json ./web/
RUN npm ci --prefix web

COPY web ./web
RUN npm run build --prefix web

# 3. 複製設定檔與啟動腳本
COPY .env.example ./
COPY config ./config
COPY mosquitto ./mosquitto
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh

# 建立非 root 使用者（相容 Hugging Face Spaces UID 1000 規範）並設定權限
RUN useradd -m -u 1000 appuser \
    && sed -i 's/\r$//' /app/scripts/docker-entrypoint.sh \
    && chmod +x /app/scripts/docker-entrypoint.sh \
    && mkdir -p /app/artifacts/runtime \
    && chown -R appuser:appuser /app

USER appuser

# Hugging Face Spaces 預設對外連接埠為 7860（可由環境變數 PORT 覆寫）
ENV PORT=7860 \
    HEMS_HOST=0.0.0.0

EXPOSE 7860

CMD ["/app/scripts/docker-entrypoint.sh"]
