#!/usr/bin/env bash
# build-release.sh -- 可复现地从源组装 reCamera Pro 扩展 API 的两个发布包。
#
# 用法:
#   release/build-release.sh --rkipc <path> --entry-cgi <path> --version <x.y.z> \
#                            [--baseline-md5 <md5>]
#
# 产出(写入 release/):
#   recamera-ext-api-v<ver>.tar        固件 sideload 包(rkipc + entry.cgi + SDK +
#                                      install.sh/rollback.sh/MANIFEST.txt/README.md)
#   recamera-ext-kit-v<ver>.tar.gz     kit 分享包(kit + sdk + examples + INSTALL.sh +
#                                      SHARE-README.md;不含任何固件)
#
# 副作用: 用实际 artifact 的 md5 自动写回
#   release/pkg/install.sh   (RKIPC_MD5 / ENTRY_MD5 / SO_MD5 / VALIDATED_BASELINE_MD5S)
#   release/pkg/MANIFEST.txt (3 个 artifact 的 md5+size / 已验证基线 md5 / 版本 / 日期)
#   注: 回滚目标不再用 md5 白名单判定,改为按内容判定(见 install.sh 顶部注释),
#       所以 rollback.sh 不含任何 md5 列表,这里也不再写回它。
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
RKIPC="" ENTRY="" VERSION="" BASELINE_MD5S=""
while [ $# -gt 0 ]; do
  case "$1" in
    --rkipc)       RKIPC="$2";        shift 2 ;;
    --entry-cgi)   ENTRY="$2";        shift 2 ;;
    --version)     VERSION="$2";      shift 2 ;;
    --baseline-md5|--factory-md5) BASELINE_MD5S="$2"; shift 2 ;;  # validated factory-baseline md5(s), space-sep
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

# validated firmware baselines (advisory only -- install.sh warns on an unlisted
# baseline, it does not refuse). --baseline-md5 overrides, else reuse install.sh's.
if [ -z "$BASELINE_MD5S" ]; then
  BASELINE_MD5S=$(perl -ne 'print $1 if /^VALIDATED_BASELINE_MD5S="([^"]*)"/' "$PKG/install.sh")
  [ -n "$BASELINE_MD5S" ] || { echo "FATAL: 无法从 install.sh 读到 VALIDATED_BASELINE_MD5S,请用 --baseline-md5" >&2; exit 1; }
  echo "validated baselines (沿用现有): $BASELINE_MD5S"
fi
BASELINE_FIRST=$(echo "$BASELINE_MD5S" | awk '{print $1}')   # single value for MANIFEST line

echo "=== inputs ==="
echo "  version      $VERSION"
echo "  rkipc        $RKIPC_MD5  ($RKIPC_SZ B)  <- $RKIPC"
echo "  entry.cgi    $ENTRY_MD5  ($ENTRY_SZ B)  <- $ENTRY"
echo "  $SO_NAME  $SO_MD5  ($SO_SZ B)"
echo "  baselines    $BASELINE_MD5S"

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
setlist "$PKG/install.sh"  VALIDATED_BASELINE_MD5S "$BASELINE_MD5S"

# MANIFEST.txt: version / built date / 3 artifact md5+size / factory md5
perl -0777 -pi -e "
  s/^(recamera-ext-api\s+v)\S+/\${1}$VERSION/m;
  s/^(Built:\s+)\S+/\${1}$TODAY/m;
  s/^(\s*rkipc\s+)[0-9a-f]{32}(\s+)\d+( B)/\${1}$RKIPC_MD5\${2}$RKIPC_SZ\${3}/m;
  s/^(\s*entry\.cgi\s+)[0-9a-f]{32}(\s+)\d+( B)/\${1}$ENTRY_MD5\${2}$ENTRY_SZ\${3}/m;
  s/^(\s*sdk\/lib\/\Q$SO_NAME\E\s+)[0-9a-f]{32}(\s+)\d+( B)/\${1}$SO_MD5\${2}$SO_SZ\${3}/m;
  s/^(\s*validated baselines\s+)[0-9a-f]{32}/\${1}$BASELINE_FIRST/m;
" "$PKG/MANIFEST.txt"

# README.md: title version + expected rkipc md5
perl -0777 -pi -e "
  s/^(# reCamera Pro Extension API — v)\S+/\${1}$VERSION/m;
  s/(# expect )[0-9a-f]{32}/\${1}$RKIPC_MD5/;
" "$PKG/README.md"

# kit/__init__.py: the kit CONTRACT version apps depend on via manifest "kit".
# Written back here so a shipped kit can never disagree with the tarball it came
# out of -- appmgr reads this literal to gate installs (appmgr/kitversion.py).
perl -0777 -pi -e "s/^(__version__ = \")[^\"]*(\")/\${1}$VERSION\${2}/m" "$REPO/kit/__init__.py"
grep -q "^__version__ = \"$VERSION\"\$" "$REPO/kit/__init__.py" || {
    echo "FATAL: failed to write __version__=$VERSION into kit/__init__.py" >&2; exit 1; }

# ---- staging -----------------------------------------------------------------
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

scrub() { # remove build junk under $1
  find "$1" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$1" -name '.pytest_cache' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  # Editor/refactor leftovers live next to the source they shadow. `cp -R` copies
  # the whole tree, so an untracked `engine.py.bak-pre-ctypes` sitting in
  # kit/runtime/ ships to every device unless it is dropped here. Same glob set
  # release/deploy/build-packages.py uses for appmgr.
  find "$1" \( -name '*.pyc' -o -name '.DS_Store' -o -name '._*' \
               -o -name '*.bak' -o -name '*.bak.*' -o -name '*.bak-*' \
               -o -name '*.orig' -o -name '*.rej' -o -name '*.swp' \) \
       -delete 2>/dev/null || true
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
check_list  "$PKG/install.sh"  VALIDATED_BASELINE_MD5S "$BASELINE_FIRST"
# The rollback target is decided by content, not by an md5 list, so assert the
# marker test itself instead: the rkipc/entry.cgi we ship MUST trip it, otherwise
# every "is this a factory build?" check downstream silently answers yes.
check_markers() { # file regex label
  # NOTE: not `strings | grep -q` -- under pipefail, grep -q exiting early
  # SIGPIPEs strings (141) and the whole pipeline reads as failure.
  if grep -qaE "$2" "$1" 2>/dev/null; then
    echo "  OK  $(basename "$1") carries $3 markers"
  else
    echo "FATAL self-check: $1 carries no $3 markers -- rollback guard would be blind" >&2; exit 1
  fi
}
check_markers "$RKIPC" '/run/recamera|rc_ext_' rkipc-ext
check_markers "$ENTRY" 'ExtApiHandler' entry.cgi-ext

# verify firmware tar actually carries the expected rkipc/entry/so md5s
verify_tar_member() { # tar arcname expected-md5
  got=$(tar xO -f "$FW_TAR" "$2" 2>/dev/null | (md5sum 2>/dev/null || md5) | awk '{print $1}')
  [ "$got" = "$3" ] || { echo "FATAL: $FW_TAR member $2 md5=$got != $3" >&2; exit 1; }
  echo "  OK  tar member $2 md5=$got"
}
verify_tar_member "$FW_TAR" "./rkipc"                    "$RKIPC_MD5"
verify_tar_member "$FW_TAR" "./entry.cgi"                "$ENTRY_MD5"
verify_tar_member "$FW_TAR" "./sdk/lib/$SO_NAME"         "$SO_MD5"

# ---- completeness: every git-tracked source file is really inside the kit pkg --
# The kit package is assembled with `cp -R kit/ examples/ ...`, so a NEW source
# file is picked up only as long as it sits under one of those trees. Nothing
# used to assert that, which means a fix living in a file the layout does not
# cover -- or a tree someone later switches to an explicit file list -- ships as
# a package that silently lacks it, and the device keeps running the old code
# without any error. Here the tarball is compared member-by-member against
# `git ls-files`: a tracked file that is missing from the package, or present
# with different bytes, fails the build.
echo "=== completeness (git-tracked sources vs $(basename "$KIT_TGZ")) ==="
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  KIT_PREFIX="recamera-ext-kit-v$VERSION/" REPO="$REPO" KIT_TGZ="$KIT_TGZ" python3 - <<'PY'
import fnmatch, hashlib, os, subprocess, sys, tarfile

repo   = os.environ["REPO"]
tgz    = os.environ["KIT_TGZ"]
prefix = os.environ["KIT_PREFIX"]

# repo-relative source tree -> path inside the package
TREES = {
    "kit":                      "kit",
    "examples":                 "examples",
    "sdk/python/recamera_ext":  "sdk/python/recamera_ext",
}
# mirrors scrub(): these are intentionally not shipped
SKIP = ("*.pyc", ".DS_Store", "._*", "*.bak", "*.bak.*", "*.bak-*",
        "*.orig", "*.rej", "*.swp", "*/__pycache__/*", "*/.pytest_cache/*")

def skipped(rel):
    base = os.path.basename(rel)
    return any(fnmatch.fnmatch(base, g) or fnmatch.fnmatch(rel, g) for g in SKIP)

members = {}
with tarfile.open(tgz, "r:gz") as tar:
    for ti in tar:
        if ti.isreg():
            members[ti.name] = hashlib.md5(tar.extractfile(ti).read()).hexdigest()

missing, differing, checked = [], [], 0
for tree, arcdir in TREES.items():
    out = subprocess.run(["git", "-C", repo, "ls-files", "-z", "--", tree],
                         capture_output=True, text=True, check=True).stdout
    for rel in filter(None, out.split("\0")):
        if skipped(rel):
            continue
        src = os.path.join(repo, rel)
        if not os.path.isfile(src):          # tracked but deleted in the worktree
            continue
        arc = prefix + arcdir + rel[len(tree):]
        checked += 1
        if arc not in members:
            missing.append(rel)
            continue
        got = hashlib.md5(open(src, "rb").read()).hexdigest()
        if got != members[arc]:
            differing.append("%s (src %s != pkg %s)" % (rel, got, members[arc]))

for label, items in (("MISSING from package", missing),
                     ("CONTENT DIFFERS", differing)):
    if items:
        print("FATAL completeness: %d file(s) %s:" % (len(items), label),
              file=sys.stderr)
        for it in items[:20]:
            print("    " + it, file=sys.stderr)
        if len(items) > 20:
            print("    ... and %d more" % (len(items) - 20), file=sys.stderr)

if missing or differing:
    print("HINT: a source tree grew a file the packaging layout does not reach, "
          "or the package predates the working tree. Re-run the build; if it "
          "still fails, fix the staging step above -- do NOT relax this check.",
          file=sys.stderr)
    sys.exit(1)

print("  OK  %d git-tracked files present in the package with matching md5" % checked)
for probe in ("kit/runtime/engine.py", "kit/runtime/ctypes_rknn.py"):
    print("  OK  %-32s md5=%s" % (probe, members.get(prefix + probe, "<absent>")))
PY
else
  echo "  SKIP  not a git worktree -- completeness check needs 'git ls-files'" >&2
fi

echo "=== done ==="
echo "  firmware sideload : $FW_TAR"
echo "  kit share         : $KIT_TGZ"
