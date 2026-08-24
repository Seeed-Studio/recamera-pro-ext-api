# librecamera_ext -- reCamera Pro extension SDK (C ABI v1), soname .so.1.
#
# This public repository is the canonical client-side source.  It deliberately
# does not reach into a neighbouring recamera_ipc checkout: a release SDK must
# be reproducible from this tree alone.  The committed protobuf-c files are
# generated from sdk/proto/ and checked for drift in CI.
cmake_minimum_required(VERSION 3.10)
project(librecamera_ext VERSION 1.0.0 LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS ON)

find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
	pkg_check_modules(PROTOBUF_C QUIET libprotobuf-c)
endif()
find_path(PROTOBUF_C_HEADER_DIR protobuf-c/protobuf-c.h
	HINTS ${PROTOBUF_C_INCLUDE_DIRS})
find_library(PROTOBUF_C_LIBRARY NAMES protobuf-c
	HINTS ${PROTOBUF_C_LIBRARY_DIRS})

if(NOT PROTOBUF_C_HEADER_DIR)
	message(FATAL_ERROR
		"protobuf-c headers were not found; set PROTOBUF_C_HEADER_DIR or PKG_CONFIG_PATH")
endif()
if(NOT PROTOBUF_C_LIBRARY)
	message(FATAL_ERROR
		"libprotobuf-c was not found; set PROTOBUF_C_LIBRARY or PKG_CONFIG_PATH")
endif()

set(RECAMERA_EXT_NATIVE_SOURCES
	src/recamera_ext.c
	src/recamera_ext_mask.c
	src/frame_recv.c
	src/probe_recv.c
	src/inference_lease.c
	src/ext_client_common.c
	generated/inference.pb-c.c
	generated/ext_api.pb-c.c
)

add_library(recamera_ext SHARED ${RECAMERA_EXT_NATIVE_SOURCES})

target_include_directories(recamera_ext PUBLIC
	$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
	$<INSTALL_INTERFACE:include>
)
target_include_directories(recamera_ext PRIVATE
	${CMAKE_CURRENT_SOURCE_DIR}/src
	${CMAKE_CURRENT_SOURCE_DIR}/generated
	${PROTOBUF_C_HEADER_DIR}
)

find_package(Threads REQUIRED)
target_link_libraries(recamera_ext PRIVATE ${PROTOBUF_C_LIBRARY} Threads::Threads)
target_compile_options(recamera_ext PRIVATE
	-Wall -Wextra -Wformat=2 -Werror=implicit-function-declaration)

set_target_properties(recamera_ext PROPERTIES
	OUTPUT_NAME recamera_ext
	VERSION ${PROJECT_VERSION}
	SOVERSION 1
)

install(TARGETS recamera_ext LIBRARY DESTINATION lib)
install(FILES include/recamera_ext.h DESTINATION include)

option(RECAMERA_EXT_BUILD_TESTS
	"Build and register native SDK reproducibility/ABI gates" OFF)
set(RECAMERA_EXT_PROTOC_C "" CACHE FILEPATH
	"Path to the pinned protoc-c 1.4.1 executable used by drift tests")

if(RECAMERA_EXT_BUILD_TESTS)
	enable_testing()

	# These tests inspect source/generated files and the target ELF on the build
	# host. They never execute the target library, so the same test set works for
	# native host builds and aarch64 cross builds.
	find_program(RECAMERA_EXT_TEST_PYTHON NAMES python3 python
		NO_CMAKE_FIND_ROOT_PATH)
	if(NOT RECAMERA_EXT_TEST_PYTHON)
		message(FATAL_ERROR "python3 is required for native SDK tests")
	endif()

	if(NOT RECAMERA_EXT_PROTOC_C)
		find_program(RECAMERA_EXT_PROTOC_C NAMES protoc-c
			NO_CMAKE_FIND_ROOT_PATH)
	endif()
	if(NOT RECAMERA_EXT_PROTOC_C)
		message(FATAL_ERROR
			"pinned protoc-c 1.4.1 is required when RECAMERA_EXT_BUILD_TESTS=ON")
	endif()

	if(NOT CMAKE_READELF)
		find_program(CMAKE_READELF NAMES readelf NO_CMAKE_FIND_ROOT_PATH)
	endif()
	if(NOT CMAKE_READELF)
		message(FATAL_ERROR "readelf is required for native SDK ABI tests")
	endif()

	# Compile-time LP64 layout assertions guard structs passed through the frozen
	# C ABI. This object is compiled for the selected target but never executed.
	add_library(recamera_ext_abi_layout OBJECT tests/native_abi_layout.c)
	target_include_directories(recamera_ext_abi_layout PRIVATE
		${CMAKE_CURRENT_SOURCE_DIR}/include)

	add_test(NAME recamera_ext.source_closure
		COMMAND ${RECAMERA_EXT_TEST_PYTHON}
			${CMAKE_CURRENT_SOURCE_DIR}/tools/check_native_sdk.py
			--source-root ${CMAKE_CURRENT_SOURCE_DIR})
	add_test(NAME recamera_ext.generated_drift
		COMMAND ${RECAMERA_EXT_TEST_PYTHON}
			${CMAKE_CURRENT_SOURCE_DIR}/tools/check_generated.py
			--sdk-root ${CMAKE_CURRENT_SOURCE_DIR}
			--protoc-c ${RECAMERA_EXT_PROTOC_C})
	add_test(NAME recamera_ext.elf_abi
		COMMAND ${RECAMERA_EXT_TEST_PYTHON}
			${CMAKE_CURRENT_SOURCE_DIR}/tools/check_native_sdk.py
			--source-root ${CMAKE_CURRENT_SOURCE_DIR}
			--library $<TARGET_FILE:recamera_ext>
			--readelf ${CMAKE_READELF})

	set_tests_properties(
		recamera_ext.source_closure
		recamera_ext.generated_drift
		recamera_ext.elf_abi
		PROPERTIES LABELS "native-sdk")

	add_custom_target(check-generated
		COMMAND ${RECAMERA_EXT_TEST_PYTHON}
			${CMAKE_CURRENT_SOURCE_DIR}/tools/check_generated.py
			--sdk-root ${CMAKE_CURRENT_SOURCE_DIR}
			--protoc-c ${RECAMERA_EXT_PROTOC_C}
		VERBATIM)
endif()
