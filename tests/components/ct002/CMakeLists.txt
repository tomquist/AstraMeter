cmake_minimum_required(VERSION 3.20)
project(ct002_host_tests CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra -Wpedantic -Werror)

include(FetchContent)
FetchContent_Declare(
  googletest
  URL https://github.com/google/googletest/archive/refs/tags/v1.14.0.tar.gz
  URL_HASH SHA256=8ad598c73ad796e0d8280b082cebd82a630d73e73cd3c70057938a6501bba5d7
)
set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(googletest)

# Repo root, two levels up from tests/components/ct002.
get_filename_component(REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../.." ABSOLUTE)

add_executable(host_protocol_test
  host_protocol_test.cpp
  ${REPO_ROOT}/esphome/components/ct002/protocol.cpp
)
target_include_directories(host_protocol_test PRIVATE
  ${REPO_ROOT}
  ${CMAKE_CURRENT_SOURCE_DIR}
)
target_link_libraries(host_protocol_test PRIVATE GTest::gtest_main)

add_executable(host_wrappers_test
  host_wrappers_test.cpp
  ${REPO_ROOT}/esphome/components/ct002/hampel.cpp
  ${REPO_ROOT}/esphome/components/ct002/smoothing.cpp
  ${REPO_ROOT}/esphome/components/ct002/pid.cpp
)
target_include_directories(host_wrappers_test PRIVATE
  ${REPO_ROOT}
  ${CMAKE_CURRENT_SOURCE_DIR}
)
target_compile_definitions(host_wrappers_test PRIVATE CT002_HOST_TEST=1)
target_link_libraries(host_wrappers_test PRIVATE GTest::gtest_main)

add_executable(host_balancer_test
  host_balancer_test.cpp
  ${REPO_ROOT}/esphome/components/ct002/balancer.cpp
)
target_include_directories(host_balancer_test PRIVATE
  ${REPO_ROOT}
  ${CMAKE_CURRENT_SOURCE_DIR}
)
target_link_libraries(host_balancer_test PRIVATE GTest::gtest_main)

# Marstek MQTT responder: pure CSV k=v helpers that mirror
# src/astrameter/mqtt_insights/marstek_mqtt.py. No ESPHome deps — compiles
# clean against host-gcc and validates wire-format parity with Python.
add_executable(host_marstek_responder_test
  host_marstek_responder_test.cpp
  ${REPO_ROOT}/esphome/components/astrameter_mqtt_insights/marstek_responder.cpp
)
target_include_directories(host_marstek_responder_test PRIVATE
  ${REPO_ROOT}
  ${CMAKE_CURRENT_SOURCE_DIR}
)
target_link_libraries(host_marstek_responder_test PRIVATE GTest::gtest_main)

include(GoogleTest)
gtest_discover_tests(host_protocol_test)
gtest_discover_tests(host_wrappers_test)
gtest_discover_tests(host_balancer_test)
gtest_discover_tests(host_marstek_responder_test)
