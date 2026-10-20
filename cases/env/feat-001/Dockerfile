# ROS 2 Humble (C++) 用の隔離検証イメージ（FEAT-001: autoware_motion_utils）。
# cases/env/ros-humble-cpp/Dockerfile と同じ構成で、ROS_PACKAGES に
# autoware_motion_utils のビルド依存（apt で入手できるもの）を追加した。
# autoware_motion_utils 自体は入れない（候補が API を参照できてしまうため）。
# BASE_IMAGEは呼び出し側がdigest固定の ros:humble-ros-base を渡す。
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

# パッケージ一覧はここに集約し、他のケースが拡張しやすいようにする。
ARG ROS_PACKAGES="\
    build-essential \
    python3-colcon-common-extensions \
    ros-humble-ament-cmake-ros \
    ros-humble-ament-lint-auto \
    ros-humble-autoware-cmake \
    ros-humble-autoware-lint-common \
    ros-humble-autoware-utils-geometry \
    ros-humble-autoware-planning-msgs \
    ros-humble-autoware-internal-planning-msgs \
    ros-humble-tf2 \
    ros-humble-tf2-geometry-msgs \
    ros-humble-geometry-msgs \
    libeigen3-dev \
    ros-humble-autoware-adapi-v1-msgs \
    ros-humble-autoware-interpolation \
    ros-humble-autoware-utils-math \
    ros-humble-autoware-utils-system \
    ros-humble-autoware-utils-visualization \
    ros-humble-autoware-vehicle-msgs \
    ros-humble-nav-msgs \
    ros-humble-visualization-msgs"

RUN apt-get update \
    && apt-get install --no-install-recommends -y ${ROS_PACKAGES} \
    && rm -rf /var/lib/apt/lists/*

# autoware_motion_utils のテストは rclcpp を初期化し、ログディレクトリを作る。
# 採点containerはルートが読み取り専用で /tmp だけ書けるため、ROS の出力先を /tmp へ向ける。
ENV ROS_HOME=/tmp/.ros \
    ROS_LOG_DIR=/tmp/.ros/log

# 非rootで実行し、workspaceを作業ディレクトリにする。
RUN useradd --create-home --uid 1000 bench
WORKDIR /ws
USER bench
