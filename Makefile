ifeq ($(APP_PARAM),)
APP_PARAM := ../Makefile.param
include $(APP_PARAM)
endif

export LC_ALL=C
SHELL := /bin/bash

MAKEFILE_DIR := $(patsubst %/,%,$(dir $(realpath $(firstword $(MAKEFILE_LIST)))))
SDK_TOP_DIR := $(abspath $(MAKEFILE_DIR)/../../..)
PKG_NAME := recamera-pro-ext-api
PKG_BIN ?= out
PKG_BUILD ?= build

SDK_SOURCE_DIR := $(MAKEFILE_DIR)/sdk
SDK_BUILD_DIR := $(MAKEFILE_DIR)/$(PKG_BUILD)/sdk
OUT_ROOT := $(MAKEFILE_DIR)/$(PKG_BIN)/root
INSTALL_PREFIX := /usr
PYTHON_SITE := $(OUT_ROOT)$(INSTALL_PREFIX)/lib/python3.11/site-packages
RUNTIME_WHEEL_DIR := $(MAKEFILE_DIR)/release/pkg/wheels
RUNTIME_WHEEL_LOCK := $(MAKEFILE_DIR)/runtime/requirements.lock
RKNN_RUNTIME_SOURCE ?= $(RK_APP_MEDIA_LIBS_PATH)/librknnrt.so
RKNN_RUNTIME_LINK := $(OUT_ROOT)$(INSTALL_PREFIX)/lib/librknnrt.so
RKNN_RUNTIME_TARGET := ../../oem/usr/lib/librknnrt.so
PLATFORM_PYTHON_ROOT := $(OUT_ROOT)$(INSTALL_PREFIX)/lib/recamera
OEM_STAGING_ROOT := $(MAKEFILE_DIR)/$(PKG_BIN)

CROSS_GCC := $(shell \
	if command -v $(RK_APP_CROSS)-gcc >/dev/null 2>&1; then \
		command -v $(RK_APP_CROSS)-gcc; \
	else \
		printf '%s' "$(SDK_TOP_DIR)/tools/linux/toolchain/$(RK_APP_CROSS)/bin/$(RK_APP_CROSS)-gcc"; \
	fi)
SDK_SYSROOT := $(shell "$(CROSS_GCC)" -print-sysroot 2>/dev/null)
PROTOBUF_C_HEADER_DIR := $(shell \
	for d in \
		"$(RK_APP_BUILDROOT_STAGING)/usr/include" \
		"$(SDK_SYSROOT)/usr/include" \
		"$(SDK_SYSROOT)/include" ; do \
		if [ -f "$$d/protobuf-c/protobuf-c.h" ]; then printf '%s' "$$d"; break; fi; \
	done)
PROTOBUF_C_LIBRARY := $(shell \
	for f in \
		"$(RK_APP_BUILDROOT_STAGING)/usr/lib/libprotobuf-c.so" \
		"$(SDK_SYSROOT)/usr/lib/libprotobuf-c.so" \
		"$(SDK_SYSROOT)/usr/lib/libprotobuf-c.so.1" ; do \
		if [ -e "$$f" ]; then printf '%s' "$$f"; break; fi; \
	done)

.PHONY: all clean distclean info sdk-configure sdk-build sdk-install python-install \
	rknn-runtime-install platform-services-install verify no-op

ifneq ($(RK_APP_TYPE),RKIPC_RV1126B_RECAMERA2)

all: no-op

no-op:
	@echo "$(PKG_NAME): RK_APP_TYPE=$(RK_APP_TYPE) -> no-op"

info:
	@echo "PKG_NAME=$(PKG_NAME)"
	@echo "RK_APP_TYPE=$(RK_APP_TYPE)"
	@echo "status=no-op"

clean distclean:
	@rm -rf "$(MAKEFILE_DIR)/$(PKG_BUILD)" "$(MAKEFILE_DIR)/$(PKG_BIN)"

else

all: verify
	$(call MAROC_COPY_PKG_TO_APP_OUTPUT, $(RK_APP_OUTPUT), $(PKG_BIN))

info:
	@echo "PKG_NAME=$(PKG_NAME)"
	@echo "RK_APP_TYPE=$(RK_APP_TYPE)"
	@echo "RK_APP_CROSS=$(RK_APP_CROSS)"
	@echo "CROSS_GCC=$(CROSS_GCC)"
	@echo "RK_APP_BUILDROOT_STAGING=$(RK_APP_BUILDROOT_STAGING)"
	@echo "SDK_SYSROOT=$(SDK_SYSROOT)"
	@echo "PROTOBUF_C_HEADER_DIR=$(PROTOBUF_C_HEADER_DIR)"
	@echo "PROTOBUF_C_LIBRARY=$(PROTOBUF_C_LIBRARY)"
	@echo "OUT_ROOT=$(OUT_ROOT)"
	@echo "PYTHON_SITE=$(PYTHON_SITE)"
	@echo "RUNTIME_WHEEL_LOCK=$(RUNTIME_WHEEL_LOCK)"
	@echo "RKNN_RUNTIME_SOURCE=$(RKNN_RUNTIME_SOURCE)"
	@echo "RKNN_RUNTIME_LINK=$(RKNN_RUNTIME_LINK)"
	@echo "RKNN_RUNTIME_TARGET=$(RKNN_RUNTIME_TARGET)"
	@echo "PLATFORM_PYTHON_ROOT=$(PLATFORM_PYTHON_ROOT)"

sdk-configure:
	@test -n "$(SDK_SYSROOT)" || { echo "missing sysroot from $(RK_APP_CROSS)-gcc"; exit 1; }
	@test -n "$(PROTOBUF_C_HEADER_DIR)" || { echo "protobuf-c header not found"; exit 1; }
	@test -n "$(PROTOBUF_C_LIBRARY)" || { echo "protobuf-c library not found"; exit 1; }
	@mkdir -p "$(SDK_BUILD_DIR)"
	@cmake -S "$(SDK_SOURCE_DIR)" -B "$(SDK_BUILD_DIR)" \
		-DCMAKE_BUILD_TYPE=Release \
		-DCMAKE_SYSTEM_NAME=Linux \
		-DCMAKE_C_COMPILER="$(CROSS_GCC)" \
		-DCMAKE_SYSROOT="$(SDK_SYSROOT)" \
		"-DCMAKE_FIND_ROOT_PATH=$(RK_APP_BUILDROOT_STAGING);$(SDK_SYSROOT)" \
		-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER \
		-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY \
		-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY \
		-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY \
		"-DPROTOBUF_C_HEADER_DIR=$(PROTOBUF_C_HEADER_DIR)" \
		"-DPROTOBUF_C_LIBRARY=$(PROTOBUF_C_LIBRARY)" \
		-DCMAKE_INSTALL_PREFIX="$(INSTALL_PREFIX)"

sdk-build: sdk-configure
	@cmake --build "$(SDK_BUILD_DIR)" -j"$(RK_APP_JOBS)"

sdk-install: sdk-build
	@rm -f "$(OUT_ROOT)$(INSTALL_PREFIX)/lib"/librecamera_ext.so*
	@rm -f "$(OUT_ROOT)$(INSTALL_PREFIX)/include/recamera_ext.h"
	@mkdir -p "$(OUT_ROOT)"
	@DESTDIR="$(OUT_ROOT)" cmake --install "$(SDK_BUILD_DIR)" --prefix "$(INSTALL_PREFIX)"
	@test -f "$(OUT_ROOT)$(INSTALL_PREFIX)/lib/librecamera_ext.so.1.0.0"
	@test -L "$(OUT_ROOT)$(INSTALL_PREFIX)/lib/librecamera_ext.so.1"
	@test -L "$(OUT_ROOT)$(INSTALL_PREFIX)/lib/librecamera_ext.so"
	@test -f "$(OUT_ROOT)$(INSTALL_PREFIX)/include/recamera_ext.h"

python-install:
	@mkdir -p "$(PYTHON_SITE)"
	@python3 "$(MAKEFILE_DIR)/tools/install_site_packages.py" \
		--repo-root "$(MAKEFILE_DIR)" \
		--site-packages "$(PYTHON_SITE)" \
		--wheel-lock "$(RUNTIME_WHEEL_LOCK)" \
		--wheel-dir "$(RUNTIME_WHEEL_DIR)"

rknn-runtime-install:
	@python3 "$(MAKEFILE_DIR)/tools/install_site_packages.py" \
		--stage-rknn-runtime \
		--rootfs "$(OUT_ROOT)" \
		--rknn-runtime-source "$(RKNN_RUNTIME_SOURCE)"

platform-services-install:
	@python3 "$(MAKEFILE_DIR)/tools/install_platform_services.py" \
		--repo-root "$(MAKEFILE_DIR)" \
		--rootfs "$(OUT_ROOT)" \
		--oem "$(OEM_STAGING_ROOT)"

verify: sdk-install python-install rknn-runtime-install platform-services-install
	@readelf -h "$(OUT_ROOT)$(INSTALL_PREFIX)/lib/librecamera_ext.so.1.0.0" | grep -q "Machine:.*AArch64"
	@python3 "$(MAKEFILE_DIR)/tools/install_site_packages.py" \
		--verify-rknn-runtime \
		--rootfs "$(OUT_ROOT)" \
		--rknn-runtime-source "$(RKNN_RUNTIME_SOURCE)"
	@test -L "$(RKNN_RUNTIME_LINK)"
	@test "$$(readlink "$(RKNN_RUNTIME_LINK)")" = "$(RKNN_RUNTIME_TARGET)"
	@test -d "$(PYTHON_SITE)/recamera_ext"
	@test -d "$(PYTHON_SITE)/kit"
	@test ! -e "$(PYTHON_SITE)/kit/setup.py"
	@test ! -e "$(PYTHON_SITE)/kit/pyproject.toml"
	@test -f "$(PYTHON_SITE)/rknnlite/api/rknn_runtime.cpython-311-aarch64-linux-gnu.so"
	@test -f "$(PYTHON_SITE)/psutil/_psutil_linux.abi3.so"
	@test -f "$(PYTHON_SITE)/ruamel/yaml/__init__.py"
	@test -f "$(PYTHON_SITE)/_ruamel_yaml.cpython-311-aarch64-linux-gnu.so"
	@test -f "$(PLATFORM_PYTHON_ROOT)/appmgr/server.py"
	@test -f "$(PLATFORM_PYTHON_ROOT)/appmgr/trust.py"
	@test -f "$(PLATFORM_PYTHON_ROOT)/appmgr/schema/manifest-v2.schema.json"
	@test -f "$(PLATFORM_PYTHON_ROOT)/inferenced/server.py"
	@test -x "$(OUT_ROOT)/etc/init.d/S93inferenced"
	@test -x "$(OUT_ROOT)/etc/init.d/S94appmgr"
	@test -f "$(OEM_STAGING_ROOT)/etc/nginx/ext_appmgr.conf"
	@for native in \
		"$(PYTHON_SITE)/rknnlite/api/rknn_runtime.cpython-311-aarch64-linux-gnu.so" \
		"$(PYTHON_SITE)/psutil/_psutil_linux.abi3.so" \
		"$(PYTHON_SITE)/_ruamel_yaml.cpython-311-aarch64-linux-gnu.so"; do \
		readelf -h "$$native" | grep -q "Machine:.*AArch64" || exit 1; \
	done
	@test -z "$$(find "$(PYTHON_SITE)" \( -path '*/__pycache__*' -o -path '*/tests/*' -o -name 'test_*.py' \) -print -quit)"
	@test -z "$$(find "$(PLATFORM_PYTHON_ROOT)" \( -path '*/__pycache__*' -o -path '*/tests/*' -o -name 'test_*.py' \) -print -quit)"

clean:
	@rm -rf "$(MAKEFILE_DIR)/$(PKG_BUILD)"
	@rm -rf "$(OUT_ROOT)$(INSTALL_PREFIX)/lib/librecamera_ext.so" \
		"$(OUT_ROOT)$(INSTALL_PREFIX)/lib/librecamera_ext.so.1" \
		"$(OUT_ROOT)$(INSTALL_PREFIX)/lib/librecamera_ext.so.1.0.0" \
		"$(RKNN_RUNTIME_LINK)" \
		"$(OUT_ROOT)$(INSTALL_PREFIX)/include/recamera_ext.h" \
		"$(PYTHON_SITE)/recamera_ext" \
		"$(PYTHON_SITE)/kit" \
		"$(PYTHON_SITE)/rknnlite" \
		"$(PYTHON_SITE)/rknn_toolkit_lite2-2.3.2.dist-info" \
		"$(PYTHON_SITE)/psutil" \
		"$(PYTHON_SITE)/psutil-6.1.1.dist-info" \
		"$(PYTHON_SITE)/ruamel" \
		"$(PYTHON_SITE)/ruamel_yaml-0.19.1.dist-info" \
		"$(PYTHON_SITE)/_ruamel_yaml.cpython-311-aarch64-linux-gnu.so" \
		"$(PYTHON_SITE)/ruamel_yaml_clib-0.2.15.dist-info" \
		"$(PLATFORM_PYTHON_ROOT)/appmgr" \
		"$(PLATFORM_PYTHON_ROOT)/inferenced" \
		"$(OUT_ROOT)/etc/init.d/S93inferenced" \
		"$(OUT_ROOT)/etc/init.d/S94appmgr" \
		"$(OEM_STAGING_ROOT)/etc/nginx/ext_appmgr.conf"

distclean: clean
	@rm -rf "$(MAKEFILE_DIR)/$(PKG_BIN)"

endif
