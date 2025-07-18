#!/bin/bash
# Copyright (c) 2022-2025, NVIDIA CORPORATION.

set -euo pipefail

rapids-configure-conda-channels

source rapids-configure-sccache

source rapids-date-string

export CMAKE_GENERATOR=Ninja

rapids-print-env

CPP_CHANNEL=$(rapids-download-conda-from-s3 cpp)

rapids-generate-version > ./VERSION

sccache --zero-stats

# TODO: Remove `--no-test` flags once importing on a CPU
# node works correctly
rapids-logger "Begin pylibwholegraph build"
RAPIDS_PACKAGE_VERSION=$(head -1 ./VERSION) rapids-conda-retry build \
  --no-test \
  --channel "${CPP_CHANNEL}" \
  conda/recipes/pylibwholegraph

sccache --show-adv-stats

RAPIDS_PACKAGE_VERSION=$(head -1 ./VERSION) rapids-conda-retry build \
  --no-test \
  --channel "${CPP_CHANNEL}" \
  conda/recipes/cugraph-pyg

RAPIDS_PACKAGE_VERSION=$(head -1 ./VERSION) rapids-conda-retry build \
  --no-test \
  --channel "${CPP_CHANNEL}" \
  conda/recipes/cugraph-dgl

rapids-upload-conda-to-s3 python
