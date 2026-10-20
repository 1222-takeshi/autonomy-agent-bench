# ROS 2 Humble (C++) 用の隔離検証イメージ。
# 採点controllerがcandidate workspaceのビルドとgtest実行に使う。
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
    ros-humble-autoware-adapi-v1-msgs \
    ros-humble-autoware-vehicle-msgs \
    ros-humble-autoware-interpolation \
    ros-humble-autoware-utils-math \
    ros-humble-autoware-utils-system \
    ros-humble-autoware-utils-visualization \
    ros-humble-nav-msgs \
    ros-humble-visualization-msgs \
    ros-humble-tf2 \
    ros-humble-tf2-geometry-msgs \
    ros-humble-geometry-msgs \
    libeigen3-dev"

RUN apt-get update \
    && apt-get install --no-install-recommends -y ${ROS_PACKAGES} \
    && rm -rf /var/lib/apt/lists/*

# rclcpp::init()を呼ぶテストはlogファイルを書く。採点containerはroot FSが読み取り専用で、
# 書き込めるのは/tmpだけなので、logの置き場を/tmpへ移す。
ENV ROS_LOG_DIR=/tmp/ros_log

# 非rootで実行し、workspaceを作業ディレクトリにする。
RUN useradd --create-home --uid 1000 bench
WORKDIR /ws
USER bench
