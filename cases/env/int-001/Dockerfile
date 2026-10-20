# ROS 2 Humble (C++) 用の隔離検証イメージ（INT-001）。
# cases/env/ros-humble-cpp/Dockerfile と同じ構成に、INT-001 の依存を足したもの。
# 採点controllerがcandidate workspaceのビルドとgtest実行に使う。
# BASE_IMAGEは呼び出し側がdigest固定の ros:humble-ros-base を渡す。
# build context はこのディレクトリ。autoware_component_interface_specs/ は
# autoware_core 384bcd47e6d2fd21e5954a915153aa5303b34a53 (tag 1.10.0) の
# common/autoware_component_interface_specs をそのまま写したもの（Apache-2.0、LICENSE.autoware_core）。
# 第三者の source はリポジトリに含めない。ビルド前に、上記 revision の autoware_core から
# common/autoware_component_interface_specs をこの Dockerfile と同じディレクトリへ写してから build する。
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
    ros-humble-autoware-control-msgs \
    ros-humble-autoware-localization-msgs \
    ros-humble-autoware-map-msgs \
    ros-humble-autoware-perception-msgs \
    ros-humble-autoware-system-msgs \
    ros-humble-autoware-vehicle-msgs \
    ros-humble-nav-msgs \
    ros-humble-sensor-msgs \
    ros-humble-rclcpp-components \
    nlohmann-json3-dev"

RUN apt-get update \
    && apt-get install --no-install-recommends -y ${ROS_PACKAGES} \
    && rm -rf /var/lib/apt/lists/*

# autoware_component_interface_utils (1.10.0) は同じ revision の
# autoware_component_interface_specs を要する。apt には 1.9.0 しかないため、
# 固定 snapshot の header-only パッケージを /opt/ros/humble に入れる（テストは作らない）。
COPY autoware_component_interface_specs /opt/src/autoware_component_interface_specs
RUN . /opt/ros/humble/setup.sh \
    && cmake -S /opt/src/autoware_component_interface_specs -B /opt/src/build \
         -DCMAKE_INSTALL_PREFIX=/opt/ros/humble -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
    && cmake --build /opt/src/build \
    && cmake --install /opt/src/build \
    && rm -rf /opt/src/build

# 採点containerは --read-only で /tmp だけが書き込み可能。rcl はログを
# $ROS_HOME/log に書くため、書ける場所を指す（candidate からは変えられない）。
ENV ROS_HOME=/tmp/.ros

# 非rootで実行し、workspaceを作業ディレクトリにする。
RUN useradd --create-home --uid 1000 bench
WORKDIR /ws
USER bench
