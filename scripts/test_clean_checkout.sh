#!/usr/bin/env bash
# 在一个干净 clone 上跑单测 —— 也就是别人第一次拿到这个仓库时的样子。
#
# 为什么不能靠在本机跑 pytest: model_routes.json / llm_providers.json 是
# gitignored 的部署配置, 而 config_or_example 优先读它们。本机有配置时套件是绿的,
# 干净 checkout 上曾经 33 条红 —— 32 条来自一个发布样例的自相矛盾
# (model_routes.example.json 引用 localqwen, llm_providers.example.json 没声明它)。
# 那一类只有在没有本机配置的地方才看得见。
#
# 用宿主的 venv 跑, 但把 clone 放在 PYTHONPATH 最前 —— AItelier 是 editable 装的,
# 不这样做 `import core` 会解析回宿主仓库, 整个检查就白做了。脚本会打印
# core.__file__ 让这件事可核对, 而不是假设。
set -euo pipefail
VENV="${VENV:-$HOME/AItelier/.venv}"
SRC="${SRC:-$(cd "$(dirname "$0")/.." && pwd)}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "clone -> $TMP"
git clone --quiet --depth 1 "file://$SRC" "$TMP/repo"
cd "$TMP/repo"

echo "--- 确认跑的是 clone 里的代码, 不是宿主仓库 ---"
PYTHONPATH="$TMP/repo" "$VENV/bin/python" - <<PY
import core, sys
print("  core.__file__ =", core.__file__)
assert core.__file__.startswith("$TMP/repo"), "解析到宿主仓库了, 这次检查无效"
PY

echo "--- 确认没有本机部署配置 ---"
for f in model_routes.json llm_providers.json; do
  [ -e "$f" ] && { echo "  意外: clone 里有 $f"; exit 1; }
done
echo "  只有 example: $(ls *.example.json | tr '\n' ' ')"

echo "--- pytest ---"
PYTHONPATH="$TMP/repo" "$VENV/bin/python" -m pytest tests/unit -q
