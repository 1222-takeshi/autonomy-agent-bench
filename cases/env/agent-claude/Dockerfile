# 隔離されたコンテナで動作する採点対象エージェント（Claude Code CLI）用のイメージ。
# BASE_IMAGEはローカルの採点イメージのタグ。ビルド前にタグが catalog の image ID を指すことを確かめる。
# エージェントは --internal ネットワークで動き、外へは egress ゲートウェイ経由でしか出られない。
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root

# Node.js を公式タールボールで取得し、チェックサム検証後に配置する。
ARG NODE_VERSION=22.17.1
ARG NODE_SHA256=ff04bc7c3ed7699ceb708dbaaf3580d899ff8bf67f17114f979e83aa74fc5a49

# Node.js をダウンロード・検証・展開し、不要なファイルを除外する。
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --exclude=CHANGELOG.md --exclude=README.md --exclude=LICENSE \
    && rm /tmp/node.tar.xz

# 採点対象の Claude Code CLI をグローバルにインストールする。
ARG CLAUDE_CODE_VERSION=2.1.293
RUN npm install -g --no-audit --no-fund @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && npm cache clean --force

# 外部通信を無効化し、エージェントの動作を隔離する。
ENV DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# 非rootで実行し、作業ディレクトリにする。
USER bench
WORKDIR /work
