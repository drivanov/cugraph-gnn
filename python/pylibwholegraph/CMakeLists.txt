# =============================================================================
# Copyright (c) 2018-2025, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
# in compliance with the License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License
# is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing permissions and limitations under
# the License.
# =============================================================================

cmake_minimum_required(VERSION 3.30.4 FATAL_ERROR)

include(../../rapids_config.cmake)
include(rapids-cmake)
include(rapids-cuda)
include(rapids-cpm)

rapids_cuda_init_architectures(PYLIBWHOLEGRAPH)

project(
  PYLIBWHOLEGRAPH
  VERSION "${RAPIDS_VERSION}"
  LANGUAGES CXX CUDA
)

# ##################################################################################################
# * User Options  ------------------------------------------------------------

option(WHOLEGRAPH_BUILD_WHEELS "Whether we're building a wheel for pypi" OFF)

# ##################################################################################################
# * Base Rapids Options  -----------------------------------------------------

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# default build type
rapids_cmake_build_type(Release)

rapids_cpm_init()

include(../../cpp/cmake/thirdparty/get_raft.cmake)

# ##################################################################################################
# * Dependencies  ------------------------------------------------------------

# use <package>_ROOT here to take precedence over any other package
if(DEFINED ENV{LIBWHOLEGRAPH_DIR})
  set(wholegraph_ROOT "$ENV{LIBWHOLEGRAPH_DIR}")
endif()
find_package(wholegraph "${RAPIDS_VERSION}")
message("WholeGraph")
if(WHOLEGRAPH_FOUND)
  message(STATUS "PYLIBWHOLEGRAPH: using pre-built wholegraph C++ package")
elseif(WHOLEGRAPH_BUILD_WHEELS)
  # statically link dependencies if building wheels
  message(STATUS "PYLIBWHOLEGRAPH: build wheels")
  add_subdirectory(../../cpp/ libwholegraph EXCLUDE_FROM_ALL)
else()
  message(FATAL_ERROR "PYLIBWHOLEGRAPH: could not find wholegraph package in "
                      "cmake prefix ${CMAKE_PREFIX_PATH} or user dir $ENV{LIBWHOLEGRAPH_DIR}"
  )
endif()

include(rapids-cython-core)
rapids_cython_init()

# ##################################################################################################
# * Compiler options ---------------------------------------------------------

message(STATUS "PYLIBWHOLEGRAPH: DEFAULT_CXX_FLAGS='${DEFAULT_CXX_FLAGS}'")
message(STATUS "PYLIBWHOLEGRAPH: CXX_FLAGS='${CXX_FLAGS}'")
message(STATUS "PYLIBWHOLEGRAPH: CXX_DEFINITIONS='${CXX_DEFINITIONS}'")

# ##################################################################################################
# * Cython modules -----------------------------------------------------------

add_subdirectory(pylibwholegraph/binding)
