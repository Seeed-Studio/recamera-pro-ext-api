#!/usr/bin/env bash
# build-voice-runtime.sh -- 打包按需音频运行时 voice-runtime-<ver>.tar.gz。
#
# 背景 (INSTALL_ASSETS_SPEC §3): voice-transcribe 需要 5 个 aarch64/cp311 wheel
# (约 18 MB)。这些 wheel 不进 kit 包 —— 8 个视觉 app 从不 import 它们,而 kit 包
# 每次更新都要整包重传。装 capabilities 含 "audio" 的 app 时才单独取这一包。
#
# 用法:
#   release/build-voice-runtime.sh [--version <x.y.z>] [--out <dir>]
#
# 产出 (默认 release/dist/,已在 .gitignore 内,不进 git):
#   voice-runtime-<ver>.tar.gz     wheels/ + README.md (含每个 wheel 的 sha256)
#
# 设备侧 legacy 迁移: 本机 loopback POST /api/appMgr/upload -> /runtime。
# 公网 nginx 对 raw upload 返回 410；新应用应使用 manifest v2 bundled artifacts。
# {path}。appmgr 用 pip --no-index --find-links 离线装进 /userdata/rknnenv,
# 与 release/kit-extra/INSTALL.sh 里 rknnlite 的做法同一条路径。
#
# 平台钉死: --platform manylinux2014_aarch64 --python-version 311 --implementation cp
# --only-binary=:all:。下载后逐个核对文件名里的 tag,x86_64 / cp312 / .tar.gz 源码包
# 一律 FATAL —— 装到设备上再发现 ABI 不对的代价是一次 SSH 会话。
# 纯 Python 包 (voxedge) 允许 py3-none-any;sherpa_onnx_core 是纯原生库,tag 为
# py3-none-manylinux2014_aarch64。
#
# 确定性: tar 成员按名排序、mtime/uid/gid 归一、gzip -n,同输入 -> 同 md5
# (与 build-release.sh 一致)。
#
# zsh 兼容(macOS): 无 bash4 特性。md5 用 md5sum 或 md5 -q。
set -euo pipefail

VERSION="1.0.0"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --out)     OUT="$2";     shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

SELF=$(cd "$(dirname "$0")" && pwd)      # release/
OUT=${OUT:-$SELF/dist}
PY=${PYTHON:-python3}

# 名字必须与 appmgr/voiceruntime.py RUNTIMES["voice"]["packages"] 一致 —— 设备上
# pip 是按这些项目名从 wheels/ 里解析的,少一个就是 ModuleNotFoundError。
PKGS="sherpa-onnx sherpa-onnx-core sentencepiece kaldi-native-fbank voxedge"

md5of() {
  if command -v md5sum >/dev/null 2>&1; then md5sum "$1" | awk '{print $1}'
  else md5 -q "$1"; fi
}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/voice-runtime.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
WHEELS="$WORK/voice-runtime/wheels"
mkdir -p "$WHEELS"

echo "=== download wheels (aarch64 / cp311, binary only) ==="
# shellcheck disable=SC2086
"$PY" -m pip download --dest "$WHEELS" \
  --platform manylinux2014_aarch64 \
  --python-version 311 \
  --implementation cp \
  --only-binary=:all: \
  --no-deps \
  $PKGS

echo "=== verify wheel tags ==="
COUNT=0
for w in "$WHEELS"/*; do
  base=$(basename "$w")
  case "$base" in
    *.whl) ;;
    *) echo "FATAL: $base 不是 wheel(源码包会在设备上现场编译,必然失败)" >&2; exit 1 ;;
  esac
  case "$base" in
    *-py3-none-any.whl) ;;                       # 纯 Python: voxedge
    *aarch64*.whl)      ;;                       # 原生: 必须 aarch64
    *) echo "FATAL: $base 既不是 py3-none-any 也不含 aarch64 tag" >&2; exit 1 ;;
  esac
  case "$base" in
    *cp31[0-9]*|*py3-none*) ;;
    *) echo "FATAL: $base 的 Python tag 不是 cp311/py3" >&2; exit 1 ;;
  esac
  case "$base" in
    *cp312*|*cp310*|*cp39*|*x86_64*|*i686*)
      echo "FATAL: $base 平台/版本 tag 与设备 (aarch64, cp311) 不符" >&2; exit 1 ;;
  esac
  echo "  OK  $base"
  COUNT=$((COUNT + 1))
done
[ "$COUNT" -eq 5 ] || { echo "FATAL: 期望 5 个 wheel,实得 $COUNT" >&2; exit 1; }

echo "=== write README.md (校验值随包) ==="
{
  echo "# voice-runtime v$VERSION"
  echo
  echo "reCamera Pro 按需音频运行时 (INSTALL_ASSETS_SPEC §3)。"
  echo "目标: aarch64 / CPython 3.11 / /userdata/rknnenv"
  echo
  echo "设备本机 legacy 迁移安装（公网 raw upload 返回 410）:"
  echo '  POST http://127.0.0.1:8130/api/appMgr/upload   (raw bytes, X-Filename: voice-runtime-'"$VERSION"'.tar.gz)'
  echo '  POST /api/appMgr/runtime  {"name":"voice","path":"<上一步返回的 path>"}'
  echo
  echo "就位判定不是看文件,而是在 venv 里 \`import voxedge, sherpa_onnx\`。"
  echo
  echo "## wheels (sha256)"
  echo
  for w in "$WHEELS"/*.whl; do
    printf '    %s  %s\n' "$("$PY" -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$w")" "$(basename "$w")"
  done
} > "$WORK/voice-runtime/README.md"

echo "=== pack ==="
mkdir -p "$OUT"
TARBALL="$OUT/voice-runtime-$VERSION.tar.gz"
"$PY" - "$WORK/voice-runtime" "$TARBALL" <<'PYEOF'
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
