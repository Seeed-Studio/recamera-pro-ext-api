#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 --host|--aarch64" >&2
  exit 2
}

mode=${1:-}
case "$mode" in
  --host|--aarch64) ;;
  *) usage ;;
esac

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
workspace_root=$(cd "$repo_root/../../.." && pwd)
work_root=$(mktemp -d "${TMPDIR:-/tmp}/recamera-native-sdk.XXXXXX")

cleanup() {
  if [ -n "${work_root:-}" ] && [ -d "$work_root" ]; then
    rm -rf -- "$work_root"
  fi
}
trap cleanup EXIT

source_root="$work_root/sdk"
build_root="$work_root/build"
mkdir -p "$source_root"
cp -a "$repo_root/sdk/." "$source_root/"

buildroot_host=""
for candidate in "$workspace_root"/sysdrv/source/buildroot/buildroot-*/output/host; do
  if [ -x "$candidate/bin/protoc-c" ]; then
    buildroot_host=$candidate
    break
  fi
done

protoc_c=${PROTOC_C:-}
if [ -z "$protoc_c" ] && command -v protoc-c >/dev/null 2>&1; then
  protoc_c=$(command -v protoc-c)
fi
if [ -z "$protoc_c" ] && [ -n "$buildroot_host" ]; then
  protoc_c="$buildroot_host/bin/protoc-c"
fi
if [ -z "$protoc_c" ] || [ ! -x "$protoc_c" ]; then
  echo "pinned protoc-c 1.4.1 not found; set PROTOC_C" >&2
  exit 1
fi

cmake_args=(
  -S "$source_root"
  -B "$build_root"
  -DCMAKE_BUILD_TYPE=Release
  -DRECAMERA_EXT_BUILD_TESTS=ON
  -DRECAMERA_EXT_PROTOC_C="$protoc_c"
)

if [ "$mode" = "--host" ]; then
  protobuf_prefix=${PROTOBUF_C_PREFIX:-}
  if [ -z "$protobuf_prefix" ] && [ -n "$buildroot_host" ] &&
     [ -f "$buildroot_host/include/protobuf-c/protobuf-c.h" ]; then
    protobuf_prefix=$buildroot_host
  fi
  if [ -n "${PROTOBUF_C_HEADER_DIR:-}" ]; then
    cmake_args+=("-DPROTOBUF_C_HEADER_DIR=$PROTOBUF_C_HEADER_DIR")
  elif [ -n "$protobuf_prefix" ]; then
    cmake_args+=("-DPROTOBUF_C_HEADER_DIR=$protobuf_prefix/include")
  fi
  if [ -n "${PROTOBUF_C_LIBRARY:-}" ]; then
    cmake_args+=("-DPROTOBUF_C_LIBRARY=$PROTOBUF_C_LIBRARY")
  elif [ -n "$protobuf_prefix" ]; then
    cmake_args+=("-DPROTOBUF_C_LIBRARY=$protobuf_prefix/lib/libprotobuf-c.so")
  fi
else
  aarch64_cc=${AARCH64_CC:-}
  if [ -z "$aarch64_cc" ]; then
    candidate="$workspace_root/tools/linux/toolchain/aarch64-rockchip1240-linux-gnu/bin/aarch64-rockchip1240-linux-gnu-gcc"
    if [ -x "$candidate" ]; then
      aarch64_cc=$candidate
    fi
  fi

  aarch64_sysroot=${AARCH64_SYSROOT:-}
  if [ -z "$aarch64_sysroot" ] && [ -n "$buildroot_host" ]; then
    candidate="$buildroot_host/arm64-buildroot-linux-gnu/sysroot"
    if [ -d "$candidate" ]; then
      aarch64_sysroot=$candidate
    fi
  fi

  if [ -z "$aarch64_cc" ] || [ ! -x "$aarch64_cc" ]; then
    echo "RV1126B aarch64 compiler not found; set AARCH64_CC" >&2
    exit 1
  fi
  if [ -z "$aarch64_sysroot" ] || [ ! -d "$aarch64_sysroot" ]; then
    echo "RV1126B aarch64 sysroot not found; set AARCH64_SYSROOT" >&2
    exit 1
  fi

  cmake_args+=(
    "-DCMAKE_C_COMPILER=$aarch64_cc"
    "-DCMAKE_SYSROOT=$aarch64_sysroot"
    "-DPROTOBUF_C_HEADER_DIR=$aarch64_sysroot/usr/include"
    "-DPROTOBUF_C_LIBRARY=$aarch64_sysroot/usr/lib/libprotobuf-c.so"
  )
fi

echo "native SDK gate: ${mode#--} (isolated source: $source_root)"
cmake "${cmake_args[@]}"
cmake --build "$build_root"
ctest --test-dir "$build_root" --output-on-failure

python3 - "$build_root/librecamera_ext.so.1.0.0" <<'PY'
import hashlib
from pathlib import Path
import sys

artifact = Path(sys.argv[1])
print("native SDK artifact:", artifact)
print("sha256:", hashlib.sha256(artifact.read_bytes()).hexdigest())
PY
