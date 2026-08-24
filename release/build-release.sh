#!/usr/bin/env bash
# build-release.sh -- 可复现地从源组装 reCamera Pro 扩展 API 的两个发布包。
#
# 用法:
#   release/build-release.sh --rkipc <path> --entry-cgi <path> --version <x.y.z> \
#                            [--factory-md5 <md5>]
#
# 产出(写入 release/):
#   recamera-ext-api-v<ver>.tar        固件 sideload 包(rkipc + entry.cgi + SDK +
#                                      install.sh/rollback.sh/MANIFEST.txt/README.md)
#   recamera-ext-kit-v<ver>.tar.gz     kit 分享包(kit + sdk + examples + INSTALL.sh +
#                                      SHARE-README.md;不含任何固件)
#
# 副作用: 用实际 artifact 的 md5 自动写回
#   release/pkg/install.sh   (RKIPC_MD5 / ENTRY_MD5 / SO_MD5 / VERIFIED_FACTORY_MD5S / KNOWN_EXT_BUILD_MD5S)
#   release/pkg/rollback.sh  (VERIFIED_FACTORY_MD5S / KNOWN_EXT_BUILD_MD5S)
#   release/pkg/MANIFEST.txt (3 个 artifact 的 md5+size / factory md5 / 版本 / 日期)
#   注: 当前 build 的 rkipc md5 会自动并入 KNOWN_EXT_BUILD_MD5S(shipped 的 ext build 永不作为回滚目标)。
#   release/pkg/README.md    (标题版本 / 期望 rkipc md5)
# 消除手工同步漂移。
#
# 确定性打包: tar 成员按 arcname 排序, mtime/uid/gid/mode 归一, gzip 去掉时间戳与
# 文件名(gzip -n 等效), 保证同输入 -> 同 md5(参考 market/packaging/build.py 思路)。
#
# 排除: __pycache__ / .pytest_cache / *.pyc / .DS_Store。
# kit 包绝不含 rkipc / entry.cgi / market/ / models/ / internal/ / 权重。
#
# zsh 兼容(macOS): 无 bash4 特性(无关联数组/mapfile)。md5 用 md5sum 或 md5 -q。
set -euo pipefail

echo "FATAL: legacy artifact assembler is disabled; it does not build a source-pinned release." >&2
echo "Implement the manifest/BOM/source-build gates described in RELEASING.md before producing a new train." >&2
exit 1

# ---- args --------------------------------------------------------------------
RKIPC="" ENTRY="" VERSION="" VERIFIED_MD5S=""
while [ $# -gt 0 ]; do
  case "$1" in
    --rkipc)       RKIPC="$2";        shift 2 ;;
    --entry-cgi)   ENTRY="$2";        shift 2 ;;
    --version)     VERSION="$2";      shift 2 ;;
    --factory-md5) VERIFIED_MD5S="$2"; shift 2 ;;  # verified clean-factory md5(s), space-sep
    -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$RKIPC" ] || [ -z "$ENTRY" ] || [ -z "$VERSION" ]; then
  echo "FATAL: --rkipc, --entry-cgi, --version 均必需 (见 --help)" >&2
  exit 2
fi

# ---- paths -------------------------------------------------------------------
SELF=$(cd "$(dirname "$0")" && pwd)     # release/
REPO=$(cd "$SELF/.." && pwd)
PKG="$SELF/pkg"
SDK_SRC="$REPO/sdk"
KIT_SRC="$REPO/kit"
EX_SRC="$REPO/examples"
KIT_EXTRA="$SELF/kit-extra"
SO_NAME="librecamera_ext.so.1.0.0"

md5of() {
  if command -v md5sum >/dev/null 2>&1; then md5sum "$1" | awk '{print $1}'
  else md5 -q "$1"; fi
}
sizeof() { wc -c < "$1" | tr -d ' '; }

# ---- verify inputs -----------------------------------------------------------
for f in "$RKIPC" "$ENTRY" \
         "$SDK_SRC/lib/$SO_NAME" "$SDK_SRC/include/recamera_ext.h" \
         "$SDK_SRC/python/recamera_ext/__init__.py" "$SDK_SRC/VERSION" \
         "$PKG/install.sh" "$PKG/rollback.sh" "$PKG/MANIFEST.txt" "$PKG/README.md" \
         "$KIT_EXTRA/SHARE-README.md" "$KIT_EXTRA/INSTALL.sh"; do
  [ -f "$f" ] || { echo "FATAL: missing input: $f" >&2; exit 1; }
done
[ -d "$KIT_SRC" ] && [ -d "$EX_SRC" ] || { echo "FATAL: kit/ or examples/ missing" >&2; exit 1; }
# rknnlite python runtime wheels (bundled into both packages, offline install on device)
WHEELS_SRC="$PKG/wheels"
[ -d "$WHEELS_SRC" ] || { echo "FATAL: missing wheels dir: $WHEELS_SRC" >&2; exit 1; }
ls "$WHEELS_SRC"/rknn_toolkit_lite2-*.whl >/dev/null 2>&1 || { echo "FATAL: rknn_toolkit_lite2 wheel missing in $WHEELS_SRC" >&2; exit 1; }

# ---- md5s --------------------------------------------------------------------
RKIPC_MD5=$(md5of "$RKIPC")
ENTRY_MD5=$(md5of "$ENTRY")
SO_MD5=$(md5of "$SDK_SRC/lib/$SO_NAME")
RKIPC_SZ=$(sizeof "$RKIPC")
ENTRY_SZ=$(sizeof "$ENTRY")
SO_SZ=$(sizeof "$SDK_SRC/lib/$SO_NAME")
TODAY=$(date +%F)

# verified clean-factory list: --factory-md5 overrides, else reuse install.sh's.
if [ -z "$VERIFIED_MD5S" ]; then
  VERIFIED_MD5S=$(perl -ne 'print $1 if /^VERIFIED_FACTORY_MD5S="([^"]*)"/' "$PKG/install.sh")
  [ -n "$VERIFIED_MD5S" ] || { echo "FATAL: 无法从 install.sh 读到 VERIFIED_FACTORY_MD5S,请用 --factory-md5" >&2; exit 1; }
  echo "verified factory (沿用现有): $VERIFIED_MD5S"
fi
FACTORY_FIRST=$(echo "$VERIFIED_MD5S" | awk '{print $1}')   # single value for MANIFEST line
# known-ext list: reuse install.sh's + always flag the rkipc we are shipping now (never a rollback target).
KNOWN_EXT=$(perl -ne 'print $1 if /^KNOWN_EXT_BUILD_MD5S="([^"]*)"/' "$PKG/install.sh")
case " $KNOWN_EXT " in *" $RKIPC_MD5 "*) : ;; *) KNOWN_EXT="${KNOWN_EXT:+$KNOWN_EXT }$RKIPC_MD5" ;; esac

echo "=== inputs ==="
echo "  version      $VERSION"
echo "  rkipc        $RKIPC_MD5  ($RKIPC_SZ B)  <- $RKIPC"
echo "  entry.cgi    $ENTRY_MD5  ($ENTRY_SZ B)  <- $ENTRY"
echo "  $SO_NAME  $SO_MD5  ($SO_SZ B)"
echo "  factory(ok)  $VERIFIED_MD5S"
echo "  ext builds   $KNOWN_EXT"

# ---- write back md5s into pkg metadata --------------------------------------
setvar()  { # file VAR value  -- replace `VAR=<token>` keeping trailing comment
  perl -0777 -pi -e "s/^(\Q$2\E=)\S+/\${1}$3/m" "$1"
}
setlist() { # file VAR value  -- replace quoted `VAR="..."` (value may contain spaces), keep comment
  perl -0777 -pi -e "s/^(\Q$2\E=\")[^\"]*(\")/\${1}$3\${2}/m" "$1"
}
echo "=== write-back md5 into pkg/ metadata ==="
setvar  "$PKG/install.sh"  RKIPC_MD5             "$RKIPC_MD5"
setvar  "$PKG/install.sh"  ENTRY_MD5             "$ENTRY_MD5"
setvar  "$PKG/install.sh"  SO_MD5                "$SO_MD5"
setlist "$PKG/install.sh"  VERIFIED_FACTORY_MD5S "$VERIFIED_MD5S"
setlist "$PKG/install.sh"  KNOWN_EXT_BUILD_MD5S  "$KNOWN_EXT"
setlist "$PKG/rollback.sh" VERIFIED_FACTORY_MD5S "$VERIFIED_MD5S"
setlist "$PKG/rollback.sh" KNOWN_EXT_BUILD_MD5S  "$KNOWN_EXT"

# MANIFEST.txt: version / built date / 3 artifact md5+size / factory md5
perl -0777 -pi -e "
  s/^(recamera-ext-api\s+v)\S+/\${1}$VERSION/m;
  s/^(Built:\s+)\S+/\${1}$TODAY/m;
  s/^(\s*rkipc\s+)[0-9a-f]{32}(\s+)\d+( B)/\${1}$RKIPC_MD5\${2}$RKIPC_SZ\${3}/m;
  s/^(\s*entry\.cgi\s+)[0-9a-f]{32}(\s+)\d+( B)/\${1}$ENTRY_MD5\${2}$ENTRY_SZ\${3}/m;
  s/^(\s*sdk\/lib\/\Q$SO_NAME\E\s+)[0-9a-f]{32}(\s+)\d+( B)/\${1}$SO_MD5\${2}$SO_SZ\${3}/m;
  s/^(\s*factory rkipc md5\s+)[0-9a-f]{32}/\${1}$FACTORY_FIRST/m;
" "$PKG/MANIFEST.txt"

# README.md: title version + expected rkipc md5
perl -0777 -pi -e "
  s/^(# reCamera Pro Extension API — v)\S+/\${1}$VERSION/m;
  s/(# expect )[0-9a-f]{32}/\${1}$RKIPC_MD5/;
" "$PKG/README.md"

# ---- staging -----------------------------------------------------------------
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

scrub() { # remove build junk under $1
  find "$1" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$1" -name '.pytest_cache' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$1" \( -name '*.pyc' -o -name '.DS_Store' \) -delete 2>/dev/null || true
}

# firmware pkg staging (layout mirrors current release/pkg/ + sdk flat header)
FW="$STAGE/pkg"
mkdir -p "$FW/sdk/lib" "$FW/sdk/python"
cp "$PKG/MANIFEST.txt" "$PKG/install.sh" "$PKG/rollback.sh" "$PKG/README.md" "$FW/"
cp "$RKIPC" "$FW/rkipc"
cp "$ENTRY" "$FW/entry.cgi"
cp "$SDK_SRC/lib/$SO_NAME" "$FW/sdk/lib/$SO_NAME"
cp "$SDK_SRC/include/recamera_ext.h" "$FW/sdk/recamera_ext.h"
cp -R "$SDK_SRC/python/recamera_ext" "$FW/sdk/python/recamera_ext"
cp -R "$WHEELS_SRC" "$FW/wheels"                 # rknnlite runtime wheels (offline install)
scrub "$FW"

# kit share pkg staging (kit + full sdk w/ symlinks + examples + share files)
KROOT="$STAGE/recamera-ext-kit-v$VERSION"
mkdir -p "$KROOT/sdk/lib" "$KROOT/sdk/include" "$KROOT/sdk/python"
cp "$KIT_EXTRA/SHARE-README.md" "$KROOT/SHARE-README.md"
cp "$KIT_EXTRA/INSTALL.sh" "$KROOT/INSTALL.sh"
cp -R "$KIT_SRC" "$KROOT/kit"
cp -R "$EX_SRC" "$KROOT/examples"
cp "$SDK_SRC/lib/$SO_NAME" "$KROOT/sdk/lib/$SO_NAME"
ln -sf "$SO_NAME"                    "$KROOT/sdk/lib/librecamera_ext.so.1"
ln -sf librecamera_ext.so.1          "$KROOT/sdk/lib/librecamera_ext.so"
cp "$SDK_SRC/include/recamera_ext.h" "$KROOT/sdk/include/recamera_ext.h"
cp -R "$SDK_SRC/python/recamera_ext" "$KROOT/sdk/python/recamera_ext"
cp -R "$WHEELS_SRC" "$KROOT/wheels"                 # rknnlite runtime wheels (offline install)
cp "$SDK_SRC/VERSION" "$KROOT/sdk/VERSION"
[ -f "$SDK_SRC/README.md" ]      && cp "$SDK_SRC/README.md"      "$KROOT/sdk/README.md"
[ -f "$SDK_SRC/CMakeLists.txt" ] && cp "$SDK_SRC/CMakeLists.txt" "$KROOT/sdk/CMakeLists.txt"
scrub "$KROOT"

# guard: kit pkg must not contain firmware / market / models / internal
if find "$KROOT" \( -name rkipc -o -name entry.cgi -o -name '*.rknn' \
                   -o -name '*.onnx' -o -name '*.cvimodel' \) | grep -q .; then
  echo "FATAL: kit pkg 含固件/权重 artifact,拒绝打包" >&2; exit 1
fi
for bad in market models internal; do
  if [ -e "$KROOT/$bad" ]; then echo "FATAL: kit pkg 含 $bad/" >&2; exit 1; fi
done

# ---- deterministic pack ------------------------------------------------------
FW_TAR="$SELF/recamera-ext-api-v$VERSION.tar"
KIT_TGZ="$SELF/recamera-ext-kit-v$VERSION.tar.gz"

pack() { # out mode prefix root
  python3 - "$1" "$2" "$3" "$4" <<'PY'
import sys, os, io, gzip, tarfile, hashlib
out, mode, prefix, root = sys.argv[1:5]

def reset(ti):
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mtime = 0
    if ti.isdir():
        ti.mode = 0o755
    elif ti.issym():
        ti.mode = 0o777
    else:
        ti.mode = 0o755 if (ti.mode & 0o111) else 0o644
    return ti

entries = []
for dp, dirs, files in os.walk(root):
    dirs.sort()
    for d in dirs:
        full = os.path.join(dp, d)
        entries.append((prefix + os.path.relpath(full, root), full))
    for f in files:
        full = os.path.join(dp, f)
        entries.append((prefix + os.path.relpath(full, root), full))
entries.sort(key=lambda e: e[0])

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
    for arc, full in entries:
        ti = tar.gettarinfo(name=full, arcname=arc)  # lstat -> symlinks detected
        reset(ti)
        if ti.isreg():
            with open(full, "rb") as fh:
                tar.addfile(ti, fh)
        else:
            tar.addfile(ti)

data = buf.getvalue()
if mode == "targz":
    g = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=g, mtime=0) as gz:
        gz.write(data)
    data = g.getvalue()
with open(out, "wb") as fo:
    fo.write(data)
print("  %-40s %10d B  md5=%s" % (os.path.basename(out), len(data),
                                  hashlib.md5(data).hexdigest()))
PY
}

echo "=== pack ==="
pack "$FW_TAR"  tar    "./"                        "$FW"
pack "$KIT_TGZ" targz  "recamera-ext-kit-v$VERSION/" "$KROOT"

# ---- self-check: install.sh constants == actual artifact md5s ----------------
echo "=== self-check ==="
check_const() { # file var expected
  got=$(grep -E "^$2=" "$1" | head -1 | cut -d= -f2 | awk '{print $1}')
  [ "$got" = "$3" ] || { echo "FATAL self-check: $1 $2=$got != $3" >&2; exit 1; }
  echo "  OK  $(basename "$1") $2=$got"
}
check_list() { # file var must-contain-md5
  line=$(perl -ne "print \$1 if /^$2=\"([^\"]*)\"/" "$1")
  case " $line " in
    *" $3 "*) echo "  OK  $(basename "$1") $2 contains $3" ;;
    *) echo "FATAL self-check: $1 $2=($line) missing $3" >&2; exit 1 ;;
  esac
}
check_const "$PKG/install.sh"  RKIPC_MD5         "$RKIPC_MD5"
check_const "$PKG/install.sh"  ENTRY_MD5         "$ENTRY_MD5"
check_const "$PKG/install.sh"  SO_MD5            "$SO_MD5"
check_list  "$PKG/install.sh"  VERIFIED_FACTORY_MD5S "$FACTORY_FIRST"
check_list  "$PKG/install.sh"  KNOWN_EXT_BUILD_MD5S  "$RKIPC_MD5"
check_list  "$PKG/rollback.sh" VERIFIED_FACTORY_MD5S "$FACTORY_FIRST"
check_list  "$PKG/rollback.sh" KNOWN_EXT_BUILD_MD5S  "$RKIPC_MD5"

# verify firmware tar actually carries the expected rkipc/entry/so md5s
verify_tar_member() { # tar arcname expected-md5
  got=$(tar xO -f "$FW_TAR" "$2" 2>/dev/null | (md5sum 2>/dev/null || md5) | awk '{print $1}')
  [ "$got" = "$3" ] || { echo "FATAL: $FW_TAR member $2 md5=$got != $3" >&2; exit 1; }
  echo "  OK  tar member $2 md5=$got"
}
verify_tar_member "$FW_TAR" "./rkipc"                    "$RKIPC_MD5"
verify_tar_member "$FW_TAR" "./entry.cgi"                "$ENTRY_MD5"
verify_tar_member "$FW_TAR" "./sdk/lib/$SO_NAME"         "$SO_MD5"

echo "=== done ==="
echo "  firmware sideload : $FW_TAR"
echo "  kit share         : $KIT_TGZ"
