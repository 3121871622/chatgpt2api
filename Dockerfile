#
# 倾梦GPT绘画企业级百万号池系统 · 镜像构建
# 镜像名：qingmeng/qingmeng-gpt:<version>
#
# 构建（DNS 异常的服务器建议加 --network host，避免构建阶段解析失败）：
#   docker build --network host -t qingmeng/qingmeng-gpt:1.0.1 .
# 多架构：
#   docker buildx build --network host --platform linux/amd64,linux/arm64 \
#     -t qingmeng/qingmeng-gpt:1.0.1 --push .
#
# 说明：这里刻意不使用 `# syntax=docker/dockerfile:1` 前端镜像——部分机房拉取
# docker/dockerfile 会 TLS 超时，而本文件只用到内置语法，去掉后构建更可靠。

ARG TARGETARCH

# ---------------------------------------------------------------- 控制台构建
# 控制台产物与架构无关，只在本机构建一次。
FROM --platform=$BUILDPLATFORM node:22-alpine AS web-build

WORKDIR /app/web-vue

# npm 在部分机房需要更长重试与显式 registry；可用 --build-arg 覆盖。
ARG NPM_REGISTRY=https://registry.npmjs.org
RUN npm config set registry "${NPM_REGISTRY}" \
    && npm config set fetch-retries 5 \
    && npm config set fetch-retry-maxtimeout 120000

COPY web-vue/package.json web-vue/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY VERSION /app/VERSION
COPY web-vue ./
RUN npm run icons:check && npm run clean:dist && npm run build


# ------------------------------------------------------------ 图片缩放依赖
FROM node:22-bookworm-slim AS image-upscale-build

WORKDIR /app/scripts/image_upscale

ARG NPM_REGISTRY=https://registry.npmjs.org
RUN npm config set registry "${NPM_REGISTRY}" \
    && npm config set fetch-retries 5 \
    && npm config set fetch-retry-maxtimeout 120000

COPY scripts/image_upscale/package.json scripts/image_upscale/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force


# ------------------------------------------------------------------ 运行镜像
FROM python:3.13-slim AS app

ARG TARGETARCH
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG UV_VERSION=0.5.14

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=120 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    QINGMENG_BUILD_TYPE=release \
    DATA_DIR=/data

WORKDIR /opt/qingmeng

# libpq: PostgreSQL 客户端库；gcc: 个别依赖缺 wheel 时的兜底编译；
# gosu: 入口以 root 修正挂载目录权限后，降权到 UID 10001 运行应用。
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gosu \
    libpq-dev \
    postgresql-client \
    gcc \
    openssl \
    tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    --index-url "${PIP_INDEX_URL}"

COPY main.py ./
COPY VERSION ./
COPY CHANGELOG.md ./
COPY api ./api
COPY contracts ./contracts
COPY services ./services
COPY utils ./utils
COPY scripts ./scripts
COPY --from=image-upscale-build /usr/local/bin/node /usr/local/bin/node
COPY --from=image-upscale-build /app/scripts/image_upscale/node_modules ./scripts/image_upscale/node_modules
COPY --from=web-build /app/web-vue/dist ./web_dist
COPY deploy/docker-entrypoint.sh /usr/local/bin/qingmeng-entrypoint
RUN chmod 0755 /usr/local/bin/qingmeng-entrypoint

# 非 root 运行；入口先用 root 修正卷挂载权限，再 gosu 降权到 10001。
RUN useradd --create-home --uid 10001 qingmeng \
    && mkdir -p /data && chown -R qingmeng:qingmeng /data /opt/qingmeng

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-80}/version" >/dev/null || exit 1

ENTRYPOINT ["qingmeng-entrypoint"]
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--proxy-headers", "--forwarded-allow-ips", "*", "--access-log", "--timeout-graceful-shutdown", "10"]
