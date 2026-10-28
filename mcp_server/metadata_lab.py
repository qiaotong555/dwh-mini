"""①-B · 元数据服务：让模型能回答「这张表是什么」「这个指标怎么算的」。

    ..\\..\\.venv\\Scripts\\python.exe metadata_check.py      # 14 项验收（判据）

## 这个文件在做什么

你的数仓已经能被跑通（①-A），但**别人（或模型）不知道怎么用它**：

    「gmv 是怎么算的？」        → 得去翻 dwh_lab.py 的 SQL
    「dws_trade_daily 有哪些字段？」→ 得去看 information_schema
    「哪个表存退款状态？」        → 只能靠猜

把元数据变成**可检索的结构**，再包成 MCP 工具，模型就能自己查。
这就是学习路线文档里「面向数仓元数据的自然语言查询」（选题 A），
也是文档点名「不能砍」的那个「自己写一个 MCP Server」。

## 分工：结构自动、含义人写

    metadata/tables.yaml   ← 结构从 dwh.duckdb 自动生成（make_metadata.py），
                             description / columns[].desc 由你填
    metadata/metrics.yaml  ← 已给，3 个指标的口径

重跑 `make_metadata.py` 不会覆盖你填过的说明（按字段名合并）。

## 五个 TODO

按顺序做，每做完一个跑一次 `metadata_check.py`。

## ⚠ 验收会问这四个问题，你的 yaml 必须能答上来

    ① 「支付转化率怎么算」   → 命中 pay_rate 指标（靠 metrics 的 aliases）
    ② 「哪个字段表示退款」   → 命中 refund_cnt 或 pay_status（靠字段 desc）
    ③ 「拉链 / SCD2 在哪」   → 命中 dim_product（靠表 description）
    ④ 「GMV」                → 命中 gmv 指标

**元数据的价值就是「能被问到」。** 填 description 时把这几件事写进去，
否则检索再准也找不到 —— 这不是为了过验收，是为了这个服务真的有用。
"""
import os
import re

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
META_DIR = os.path.join(ROOT, "metadata")

# 数据流方向，别按字母排：ODS→DIM→DWD→DWS→ADS
LAYER_ORDER = {"ODS": 0, "DIM": 1, "DWD": 2, "DWS": 3, "ADS": 4}

# 同分时的次序：字段 > 指标 > 表。
# 指标名/中文名命中本来就是 10 分起，不需要靠次序赢；同分说明只有描述或 2-gram 撞上，
# 这时更窄的字段说明才是用户要的（问「退款」应命中 pay_status / refund_cnt，
# 而不是指标 gmv 的 caveats 里顺带提了「有退款」）。
KIND_ORDER = {"column": 0, "metric": 1, "table": 2}

_WORD_RE = re.compile(r"[0-9a-z\u4e00-\u9fff]+", re.IGNORECASE)
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def _low(x):
    """统一成小写字符串，顺带吃掉 None 和首尾空白。"""
    return str(x or "").strip().lower()


def _query_terms(query):
    """切词：英文按非字母数字切；中文按 2-gram 切。

        「支付转化率」→ words={支付转化率}  grams={支付, 付转, 转化, 化率}
    """
    q = _low(query)
    words = set(_WORD_RE.findall(q))
    grams = set()
    for w in words:
        for run in _CJK_RUN_RE.findall(w):
            if len(run) == 1:
                grams.add(run)
            for i in range(len(run) - 1):
                grams.add(run[i:i + 2])
    return q, words, grams


_CLAUSE_RE = re.compile(r"[^，,；;。.、\n]+")


def _lead_clause(text):
    """取第一小句（到第一个停顿符为止）。

    一条说明的**首句**通常就是「这东西是什么」，后面的分句是补充和例外。
    所以首句里命中，比在 caveats 里顺带提一句要相关得多：
        refund_cnt：「当日退款订单数，按支付状态 REFUND 统计」  ← 首句就是退款
        gmv.caveats：「…有退款和未支付时两者差几十个百分点」   ← 只是顺带
    """
    m = _CLAUSE_RE.search(text or "")
    return m.group(0) if m else (text or "")


def _score(q, words, grams, primary, aliases, text):
    """打分规则写死在这里，改规则改这一个函数。

        primary 精确命中（表名 / 指标名 / 指标中文名）  +10
        aliases 命中（指标别名 / 字段名）              +6
        首句里出现完整 query                          +2
        文本里出现完整 query                          +4
        否则按词命中，每个 +2（封顶 4）                +2 x n
        中文 2-gram 命中，每个 +2（封顶 8）            +2 x n
    """
    score = 0.0
    if q and q in primary:
        score += 10
    if q and q in aliases:
        score += 6
    if q and q in _lead_clause(text):
        score += 2                       # 首句命中：这条元数据就是在讲这个
    if q and q in text:
        score += 4
    else:
        score += min(2 * sum(1 for w in words if w in text), 4)
    score += min(2 * sum(1 for g in grams if g in text), 8)
    return score


# ============================================================================
# TODO 1 · 加载元数据
# ============================================================================
def load_metadata(meta_dir=META_DIR):
    """读 tables.yaml 与 metrics.yaml，返回统一的结构。

    返回：
        {"tables": [ {...}, ... ],          # 每项是一张表（原样）
         "metrics": [ {...}, ... ]}         # 每项是一个指标（原样）

    要求：
      · 两个文件都读进来，缺一个就抛 FileNotFoundError（不要静默返回空）
      · 用 `encoding="utf-8"`（有中文）
      · yaml.safe_load

    ⚠ 别在这里做校验（比如检查 TODO 是否填完）—— 校验是 metadata_check.py 的事。
       加载函数只负责「文件 → dict」，职责单一才好测。
    """
    tables_path = os.path.join(meta_dir, "tables.yaml")
    metrics_path = os.path.join(meta_dir, "metrics.yaml")
    for path in (tables_path, metrics_path):
        if not os.path.exists(path):
            # 缺文件必须炸：静默返回空 dict 会让上层「所有表都不存在」，最难查
            raise FileNotFoundError(f"缺少元数据文件：{path}")

    with open(tables_path, encoding="utf-8") as f:
        tables = (yaml.safe_load(f) or {}).get("tables") or []
    with open(metrics_path, encoding="utf-8") as f:
        metrics = (yaml.safe_load(f) or {}).get("metrics") or []

    return {"tables": tables, "metrics": metrics}


# ============================================================================
# TODO 2 · 列出全部表
# ============================================================================
def list_tables(meta):
    """返回 [{"name","layer","rows","description"}, ...]，按 ODS→DIM→DWD→DWS→ADS 排。

    层顺序（不要按字母排，要按数据流方向）：
        ODS=0, DIM=1, DWD=2, DWS=3, ADS=4

    提示：`sorted(tables, key=lambda t: LAYER_ORDER.get(t["layer"], 99))`。
    同层内按 yaml 里的原始顺序（sorted 是稳定排序，自动满足）。

    ⚠ 返回值要**只含这四个键**，别把整个 dict 透出去 —— 列信息在 list 里没用，
       会让模型看到的输出膨胀好几倍。
    """
    rows = [{"name": t.get("name"),
             "layer": t.get("layer"),
             "rows": t.get("rows"),
             "description": t.get("description") or ""}
            for t in meta.get("tables", [])]
    # sorted 是稳定排序，同层内保持 yaml 原始顺序
    return sorted(rows, key=lambda r: LAYER_ORDER.get(r["layer"], 99))


# ============================================================================
# TODO 3 · 描述单张表
# ============================================================================
def describe_table(meta, name):
    """返回一张表的完整结构；表不存在返回 None（**不要抛异常**）。

    返回结构：
        {"name": str, "layer": str, "rows": int, "description": str,
         "upstream": [str], "downstream": [str],
         "columns": [{"name": str, "type": str, "desc": str}, ...]}

    为什么不存在要返回 None 而不是抛：
        这个函数会被 MCP 工具直接调用。模型传错表名（比如写成 `dwd_trade`）
        是常事，抛异常会让整个工具调用失败；返回 None 则能回一句
        「没找到这张表，你是不是想问 …」，体验好得多。
        **接口给机器用的时候，错误要能表达，不要只能崩。**

    提示：表名大小写不敏感更友好（`name.lower()` 比较）。
    """
    key = _low(name)
    if not key:
        return None
    for t in meta.get("tables", []):
        if _low(t.get("name")) != key:
            continue
        return {
            "name": t.get("name"),
            "layer": t.get("layer"),
            "rows": t.get("rows"),
            "description": t.get("description") or "",
            "upstream": list(t.get("upstream") or []),
            "downstream": list(t.get("downstream") or []),
            "columns": [{"name": c.get("name"),
                         "type": c.get("type"),
                         "desc": c.get("desc") or ""}
                        for c in (t.get("columns") or [])],
        }
    return None                       # 表不存在：返回 None，不抛


# ============================================================================
# TODO 4 · 查指标口径
# ============================================================================
def find_metric(meta, name):
    """按指标名或别名查口径；找不到返回 None。

    要能命中的写法（验收会测前两种）：
        find_metric(meta, "gmv")        → 按 name 精确
        find_metric(meta, "支付转化率")  → 按 cn_name / aliases
        find_metric(meta, "GMV")        → 大小写不敏感

    提示：匹配范围 = name + cn_name + aliases 全部，比较前统一 `.lower()`。

    ⚠ 优先级：精确匹配 name 或 cn_name 的排前面，只命中 aliases 的排后面。
       否则「转化率」这种宽词会盖过真正的指标名。
    """
    q = _low(name)
    if not q:
        return None

    exact, by_alias = [], []
    for m in meta.get("metrics", []):
        if q in {_low(m.get("name")), _low(m.get("cn_name"))}:
            exact.append(m)           # 精确命中指标名/中文名，优先级最高
        elif q in {_low(a) for a in (m.get("aliases") or [])}:
            by_alias.append(m)        # 只命中别名，排在后面
    hits = exact + by_alias
    return hits[0] if hits else None


# ============================================================================
# TODO 5 · 检索（这个服务的主入口）
# ============================================================================
def search(meta, query, k=3):
    """在表名、字段名、描述、指标名/别名里检索，返回 top-k。

    返回：
        [{"kind": "table"|"column"|"metric",
          "name": "dws_trade_daily.gmv" 或 "pay_rate",
          "score": float,
          "snippet": "匹配到的原文片段，给模型看"}, ...]

    ## 打分规则（先定死，别边写边改）

        指标/表名精确命中（忽略大小写）      10 分
        别名 / 字段名命中                    6 分
        描述里出现完整 query                 4 分
        描述里出现 query 的某个词            2 分

    ## 怎么切词

        中文没有空格。query 里的英文按空格切；中文按 **2-gram** 切：
            「支付转化率」→ 支付 / 付转 / 转化 / 化率
        命中任意一个 2-gram 就给 2 分，命中越多分越高（封顶 8 分）。

    ## 别引入 embedding

        元数据只有 8 张表 / 48 个字段 / 3 个指标。关键词 + 2-gram 足够，
        而且 `clone` 下来零依赖、不用下模型。
        **能不上模型就不上** —— 这是工程判断，不是偷懒。

    ## snippet 要能直接给模型看

        不要返回整个字段列表。返回命中的那一行原文，比如：
            "dws_trade_daily.gmv（成交总额）：当日支付成功的实际支付金额之和"
    """
    q, words, grams = _query_terms(query)
    if not q:
        return []

    hits = []

    # 表 + 字段
    for t in meta.get("tables", []):
        tname = t.get("name") or ""
        tdesc = t.get("description") or ""
        hits.append({
            "kind": "table",
            "name": tname,
            "score": _score(q, words, grams, {_low(tname)}, set(), _low(tdesc)),
            "snippet": f"{tname} [{t.get('layer', '')}] {t.get('rows', '?')} 行：{tdesc}",
        })
        for c in (t.get("columns") or []):
            cname = c.get("name") or ""
            cdesc = c.get("desc") or ""
            hits.append({
                "kind": "column",
                "name": f"{tname}.{cname}",
                "score": _score(q, words, grams, set(), {_low(cname)}, _low(cdesc)),
                "snippet": f"{tname}.{cname} ({c.get('type', '')})：{cdesc}",
            })

    # 指标
    for m in meta.get("metrics", []):
        mname = m.get("name") or ""
        text = " ".join(str(m.get(k) or "")
                        for k in ("definition", "formula", "notes", "caveats"))
        hits.append({
            "kind": "metric",
            "name": mname,
            "score": _score(q, words, grams,
                            {_low(mname), _low(m.get("cn_name"))},
                            {_low(a) for a in (m.get("aliases") or [])},
                            _low(text)),
            "snippet": (f"{mname}（{m.get('cn_name', '')}）：{m.get('definition', '')}"
                        f"；公式 {m.get('formula', '')}"),
        })

    # 只差一个 2-gram（2 分）的条目是噪声：问「支付率」不该返回 ods_payment.pay_id，
    # 它只是说明里有「支付」两个字。留下「整串命中 / 名称命中 / 多个 2-gram」的。
    hits = [h for h in hits if h["score"] >= 4]
    hits.sort(key=lambda h: (-h["score"], KIND_ORDER.get(h["kind"], 9)))
    return [{"kind": h["kind"], "name": h["name"],
             "score": round(h["score"], 1), "snippet": h["snippet"]}
            for h in hits[:max(0, int(k))]]


# ============================================================================
# 以下不用改：一个把结果打成文本的 helper（server.py 也用）
# ============================================================================
def to_text(obj):
    """把 dict/list 转成紧凑的可读文本。MCP 工具返回值用这个，别返回裸 JSON。"""
    if obj is None:
        return "（未找到）"
    if isinstance(obj, list):
        return "\n".join(to_text(x) for x in obj) if obj else "（空）"
    if isinstance(obj, dict):
        if "columns" in obj:                      # describe_table 的结果
            head = (f"{obj['name']}  [{obj['layer']}]  {obj['rows']} 行\n"
                    f"说明：{obj['description']}\n"
                    f"上游：{', '.join(obj.get('upstream') or []) or '无'}\n"
                    f"下游：{', '.join(obj.get('downstream') or []) or '无'}\n字段：")
            cols = "\n".join(f"  - {c['name']} ({c['type']}): {c['desc']}"
                             for c in obj["columns"])
            return head + "\n" + cols
        if "formula" in obj:                      # 指标
            return (f"{obj['name']}（{obj.get('cn_name','')}）\n"
                    f"口径：{obj['definition']}\n"
                    f"公式：{obj['formula']}\n"
                    f"来源表：{obj['source_table']} → 指标表：{obj.get('ads_table','')}\n"
                    f"注意：{obj.get('caveats','')}")
        if "kind" in obj and "score" in obj:      # search 的单条结果
            return f"[{obj['kind']}] {obj['name']}  (score {obj['score']:.1f})  {obj['snippet']}"
        if "layer" in obj and "rows" in obj:      # list_tables 的单条
            return f"{obj['name']}  [{obj['layer']}]  {obj['rows']} 行  {obj['description']}"
        return "\n".join(f"{k}: {v}" for k, v in obj.items())
    return str(obj)
