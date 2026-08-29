#!/usr/bin/env bash
# 把 per-endpoint reasoning effort 的**配置**那一半落地。
#
# 为什么单独一个脚本, 而不是跟代码一起提交: 运行中的 server 进程持有旧的
# model_routes.py, 它的解析器只认 rotate/fallback, 遇到 effort 键会 RuntimeError。
# 路由表是进程级缓存的, 所以文件改了不会立刻被读 —— 但任何一次注册表写入
# (/api/models 的增改) 都会丢缓存, 下一次解析就炸, 而那会让每一个 LLM 调用失败。
# 所以这三处必须和重启同时发生。
set -euo pipefail
cd "$(dirname "$0")/.."

python3 - <<'PY'
import collections, json
p = "model_routes.json"
d = json.load(open(p), object_pairs_hook=collections.OrderedDict)

def eps(route):
    return list(dict.fromkeys(route["rotate"] + route.get("fallback", [])))

# flash: 9 个角色里 7 个本来就写 low, 而 low 是那条截断护栏实测出来的值
# (agent_configs 头部注释: 不设 effort 时 8 轮截断 5 轮, 评审静默不发生)。
# localqwen 也认 low —— 顺带堵掉"不设 effort 就默认 xhigh"那 3.2 倍的开销。
d["flash"]["effort"] = collections.OrderedDict((e, "low") for e in eps(d["flash"]))

# smart: researcher 要 max。DeepSeek 系认; qwen3.8-max 的词汇表里没有 max, 会静默
# 忽略并回落到它自己的最高档 —— xhigh 是同一个意图在它的词汇表里的说法。
d["smart"]["effort"] = collections.OrderedDict(
    (e, "xhigh" if e.startswith("qwen/") else "max") for e in eps(d["smart"]))

json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
open(p, "a").write("\n")
print("model_routes.json: flash.effort / smart.effort 已写入")
PY

python3 - <<'PY'
p = "agent_configs/dpe_default.yaml"
s = open(p).read()
old = '  model: "flash"\n'
i = s.index("researcher:\n")
j = s.index(old, i)
s = s[:j] + '  model: "smart"\n' + s[j + len(old):]
open(p, "w").write(s)
print('agent_configs: researcher -> smart')
PY

echo "--- 复核 ---"
python3 -c "
import json, yaml
d = json.load(open('model_routes.json'))
print(' flash.effort:', d['flash']['effort'])
print(' smart.effort:', d['smart']['effort'])
print(' researcher  :', yaml.safe_load(open('agent_configs/dpe_default.yaml'))['researcher']['model'])"

echo "--- 测试 ---"
.venv/bin/python -m pytest tests/unit/test_model_routes.py tests/unit/test_ai_router.py -q 2>&1 | tail -3

echo
echo "全绿之后再重启, 两件事必须同一个窗口:  docker restart aitelier"
