#!/usr/bin/env bash
# build-gst-hwcodec.sh -- 打包按需硬解码运行时 gst-hwcodec-<ver>.tar.gz。
#
# 背景 (RUNTIME_BUNDLE_SPEC §1): 这个运行时分发的是 3 个 .so + 3 个环境变量,
# 不是 wheel。装 capabilities 含 "hwcodec" 的 app 时才取这一包(约 1 MB,
# 比音频那 18 MB 小得多)。
#
# 用法:
#   release/build-gst-hwcodec.sh [--version <x.y.z>] [--src <dir>] [--out <dir>]
#
# --src 默认 release/dist/gst-hwcodec-src/,里面平铺放三个文件(从 wsl2-local 的
# gstreamer-rockchip 构建产物与 RV1126B buildroot sysroot 取回):
#   libgstrockchipmpp.so         RK MPP 插件(gstreamer-rockchip 编出)
#   libgstvideoparsersbad.so     h264parse/h265parse 所在插件(buildroot)
#   libgstcodecparsers-1.0.so.0  上一个插件的依赖库(buildroot)
#
# 产出 (默认 release/dist/,已在 .gitignore 内,不进 git):
#   gst-hwcodec-<ver>.tar.gz
#     gst-hwcodec/README.md
#     gst-hwcodec/files/gstreamer-1.0/libgstrockchipmpp.so
#     gst-hwcodec/files/gstreamer-1.0/libgstvideoparsersbad.so
#     gst-hwcodec/files/libgstcodecparsers-1.0.so.0
#
# files/ 这棵树**镜像 dest**(appmgr RUNTIMES["hwcodec"]["dest"] = /userdata/lib):
# 哪个 .so 进 gstreamer-1.0/、哪个进根,由包决定,不由 appmgr 代码写死。
# 对应关系: GST_PLUGIN_PATH 追加 /userdata/lib/gstreamer-1.0,
#           LD_LIBRARY_PATH 追加 /userdata/lib。
#
# 设备侧 legacy 迁移: 本机 loopback POST /api/appMgr/upload -> /runtime。
# 公网 nginx 对 raw upload 返回 410；新应用应使用 manifest v2 bundled artifact。
# {"name":"hwcodec","path":"<上一步返回的 path>"}。就位判定不是看文件在不在,
# 而是 `gst-inspect-1.0 mppvideodec` 退出码为 0。
#
# ★编码器未验证★: libgstrockchipmpp.so 里同时含 mpph264enc/mpph265enc,包里带着
# 但**未测**。编码会与 rkipc 抢 VEPU,要单独设计测试 (RUNTIME_BUNDLE_SPEC §6)。
# 本包只对解码 (mppvideodec) 作出承诺。
#
# 校验: 三个文件必须是 ELF aarch64 共享库;libgstrockchipmpp.so 的 md5 钉死为
# 已验证过的那一版,不符 FATAL(可用 --expect-mpp-md5 覆盖)。
#
# 确定性: tar 成员按名排序、mtime/uid/gid 归一、gzip -n,同输入 -> 同 md5
# (与 build-voice-runtime.sh 一致)。
#
# zsh 兼容(macOS): 无 bash4 特性。md5 用 md5sum 或 md5 -q。
set -euo pipefail

VERSION="1.0.0"
SRC=""
OUT=""
# L3 真机验证过的那一版 RK MPP 插件。
EXPECT_MPP_MD5="78152ef4982d0fef1ae3d44dc4fc3d7e"
while [ $# -gt 0 ]; do
  case "$1" in
    --version)        VERSION="$2"; shift 2 ;;
    --src)            SRC="$2";     shift 2 ;;
    --out)            OUT="$2";     shift 2 ;;
    --expect-mpp-md5) EXPECT_MPP_MD5="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

SELF=$(cd "$(dirname "$0")" && pwd)      # release/
OUT=${OUT:-$SELF/dist}
SRC=${SRC:-$SELF/dist/gst-hwcodec-src}
PY=${PYTHON:-python3}

PLUGINS="libgstrockchipmpp.so libgstvideoparsersbad.so"   # -> files/gstreamer-1.0/
LIBS="libgstcodecparsers-1.0.so.0"                        # -> files/

md5of() {
  if command -v md5sum >/dev/null 2>&1; then md5sum "$1" | awk '{print $1}'
  else md5 -q "$1"; fi
}

# ELF 头直读: EI_CLASS=2(64位) 且 e_machine=0xB7(EM_AARCH64)。file(1) 在最小化
# 的 CI 镜像里不一定有,这里不依赖它。
is_aarch64_elf() {
  "$PY" - "$1" <<'PYEOF'
import sys
with open(sys.argv[1], "rb") as f:
    hdr = f.read(20)
ok = (hdr[:4] == b"\x7fELF" and hdr[4] == 2
      and int.from_bytes(hdr[18:20], "little") == 0xB7)
sys.exit(0 if ok else 1)
PYEOF
}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/gst-hwcodec.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
ROOT="$WORK/gst-hwcodec"
mkdir -p "$ROOT/files/gstreamer-1.0"

echo "=== collect from $SRC ==="
for f in $PLUGINS; do
  [ -f "$SRC/$f" ] || { echo "FATAL: 缺少 $SRC/$f" >&2; exit 1; }
  cp "$SRC/$f" "$ROOT/files/gstreamer-1.0/$f"
done
for f in $LIBS; do
  [ -f "$SRC/$f" ] || { echo "FATAL: 缺少 $SRC/$f" >&2; exit 1; }
  cp "$SRC/$f" "$ROOT/files/$f"
done

echo "=== verify (ELF aarch64 + 钉死 md5) ==="
COUNT=0
for p in "$ROOT/files/gstreamer-1.0"/*.so "$ROOT/files"/*.so.*; do
  base=$(basename "$p")
  is_aarch64_elf "$p" || {
    echo "FATAL: $base 不是 ELF64/aarch64 共享库(装到设备上必然 dlopen 失败)" >&2
    exit 1; }
  echo "  OK  $base  $(wc -c < "$p" | tr -d ' ') B  $(md5of "$p")"
  COUNT=$((COUNT + 1))
done
[ "$COUNT" -eq 3 ] || { echo "FATAL: 期望 3 个 .so,实得 $COUNT" >&2; exit 1; }

MPP_MD5=$(md5of "$ROOT/files/gstreamer-1.0/libgstrockchipmpp.so")
[ "$MPP_MD5" = "$EXPECT_MPP_MD5" ] || {
  echo "FATAL: libgstrockchipmpp.so md5=$MPP_MD5,期望 $EXPECT_MPP_MD5" >&2
  echo "       (重新编过就用 --expect-mpp-md5 显式换掉,别默默放行)" >&2
  exit 1; }

echo "=== write README.md (校验值随包) ==="
{
  echo "# gst-hwcodec v$VERSION"
  echo
  echo "reCamera Pro 按需硬解码运行时 (RUNTIME_BUNDLE_SPEC)。"
  echo "目标: aarch64 / GStreamer 1.22.6 / 解包到 /userdata/lib"
  echo
  echo "设备本机 legacy 迁移安装（公网 raw upload 返回 410）:"
  echo '  POST http://127.0.0.1:8130/api/appMgr/upload   (raw bytes, X-Filename: gst-hwcodec-'"$VERSION"'.tar.gz)'
  echo '  POST /api/appMgr/runtime  {"name":"hwcodec","path":"<上一步返回的 path>"}'
  echo
  echo "就位判定不是看文件,而是 \`gst-inspect-1.0 mppvideodec\` 退出码为 0。"
  echo
  echo "appmgr 只给 manifest 里声明了 capabilities:[\"hwcodec\"] 的 app 注入:"
  echo
  echo '    GST_PLUGIN_PATH  += /userdata/lib/gstreamer-1.0'
  echo '    LD_LIBRARY_PATH  += /userdata/lib      (追加,不能覆盖 /oem/usr/lib)'
  echo '    GST_REGISTRY      = /userdata/gst-registry.bin'
  echo
  echo "## 未验证"
  echo
  echo "mpph264enc / mpph265enc 随插件一起进包,但**未做过任何验证**。编码器会与"
  echo "rkipc 抢 VEPU,需要单独设计测试。本包只对解码 (mppvideodec) 作出承诺。"
  echo
  echo "## files (md5)"
  echo
  for p in "$ROOT/files/gstreamer-1.0"/*.so "$ROOT/files"/*.so.*; do
    printf '    %s  %s  %s B\n' "$(md5of "$p")" \
      "$(cd "$ROOT" && echo "files/${p#"$ROOT/files/"}")" \
      "$(wc -c < "$p" | tr -d ' ')"
  done
} > "$ROOT/README.md"

echo "=== pack ==="
mkdir -p "$OUT"
TARBALL="$OUT/gst-hwcodec-$VERSION.tar.gz"
"$PY" - "$ROOT" "$TARBALL" <<'PYEOF'
import gzip, io, os, sys, tarfile

src, out = sys.argv[1], sys.argv[2]
entries = []
for root, dirs, files in os.walk(src):
    dirs.sort()
    for name in sorted(dirs) + sorted(files):
        p = os.path.join(root, name)
        entries.append((os.path.relpath(p, os.path.dirname(src)), p))
entries.sort()

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w") as tar:
    for arc, p in entries:
        ti = tar.gettarinfo(p, arcname=arc)
        ti.mtime = 0            # 归一: 同输入 -> 同 md5
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        ti.mode = 0o755 if ti.isdir() else 0o644
        if ti.isdir():
            tar.addfile(ti)
        else:
            with open(p, "rb") as f:
                tar.addfile(ti, f)
with open(out, "wb") as f:
    # mtime=0 == gzip -n: 时间戳不进头,产物可复现
    with gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as gz:
        gz.write(buf.getvalue())
PYEOF

echo
echo "=== done ==="
printf '  %s\n  size=%s B\n  md5=%s\n' \
  "$TARBALL" "$(wc -c < "$TARBALL" | tr -d ' ')" "$(md5of "$TARBALL")"
