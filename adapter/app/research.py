from __future__ import annotations

import re


COMPARISON_RE = re.compile(
    r"(?:对比|比较|区别|差异|优缺点|哪个好|怎么选|"
    r"\bcompare\b|\bcomparison\b|\bdifference(?:s)?\b|\bversus\b|\bvs\.?\b)",
    re.IGNORECASE,
)
RECOMMENDATION_RE = re.compile(
    r"(?:推荐|选购|怎么选|值得买|值不值得|适合|性价比|排行|榜单|"
    r"\brecommend(?:ation|ed|s)?\b|\bbest\b|\bworth\b|\bbuying guide\b)",
    re.IGNORECASE,
)
FACT_CHECK_RE = re.compile(
    r"(?:真假|真伪|属实|辟谣|核实|查证|事实核查|是否真实|"
    r"\bverify\b|\bfact[-\s]?check\b|\bis (?:it|this|that) true\b)",
    re.IGNORECASE,
)
FRESHNESS_RE = re.compile(
    r"(?:最新|最近|近期|今天|今日|本周|本月|当前|现在|新闻|热点|动态|进展|"
    r"\blatest\b|\brecent\b|\btoday\b|\bcurrent\b|\bnews\b|\bupdate\b)",
    re.IGNORECASE,
)
OFFICIAL_RE = re.compile(
    r"(?:官方|官网|文档|公告|政策|原文|一手资料|"
    r"\bofficial\b|\bdocumentation\b|\bdocs?\b|\bannouncement\b|\bpolicy\b)",
    re.IGNORECASE,
)
DUAL_REGION_RE = re.compile(
    r"(?:国内外|海内外|中外|中国和海外|中国与海外|"
    r"\bchina\s+(?:and|&)\s+(?:global|international|overseas)\b|"
    r"\bdomestic\s+(?:and|&)\s+(?:global|international)\b)",
    re.IGNORECASE,
)
SOCIAL_RE = re.compile(
    r"(?:推特|twitter|(?:去|上|在)\s*x\s*(?:平台|上|搜|查|看|找)|"
    r"x\s*(?:平台|帖子|推文|热搜)|"
    r"微博|知乎|小红书)",
    re.IGNORECASE,
)


def classify_research_modes(message: str) -> tuple[str, ...]:
    text = str(message or "")
    modes = []
    for name, pattern in (
        ("fact_check", FACT_CHECK_RE),
        ("comparison", COMPARISON_RE),
        ("recommendation", RECOMMENDATION_RE),
        ("freshness", FRESHNESS_RE),
        ("official", OFFICIAL_RE),
        ("dual_region", DUAL_REGION_RE),
        ("social", SOCIAL_RE),
    ):
        if pattern.search(text):
            modes.append(name)
    return tuple(modes or ("general",))


def build_research_instructions(
    message: str,
    *,
    research_date: str,
    timezone: str,
    tool_call_limit: int,
    browser_allowed: bool,
) -> str:
    modes = set(classify_research_modes(message))
    guidance = [
        "先在内部列出最多 3 个必须回答的证据问题，不向用户展示计划。",
        "第一条搜索只查核心主体；读完结果标题、日期、域名和摘要后，再针对明确缺口补搜。",
        "查询词用主体、关键限定词和必要日期组成，不复制整段聊天，不只搜年份、‘最新’或‘新闻’。",
        "搜索结果摘要只是选源线索；最终关键事实优先依据已成功提取的原文、官方页面或可相互印证的一手来源。",
        "每个关键结论都要能对应来源。来源冲突时写明冲突，不拿聚合站、百科、论坛或 SEO 页面冒充确认。",
        "最多调用 web_search 4 次、web_extract 3 次；拿到足够证据就停止，不做同义重复搜索。",
        "选定 2-3 个候选页面后，优先在一次 web_extract 调用中并行读取，避免逐页等待。",
        "web_extract 对某页失败后，直接换搜索结果中的同类高质量来源，不重复纠缠同一 URL。",
    ]

    if "fact_check" in modes:
        guidance.append(
            "这是事实核查：拆出可验证主张，先找原始发布方，再主动找一条独立佐证或反证；区分事实、推断和未证实说法。"
        )
    if "comparison" in modes:
        guidance.append(
            "这是比较题：先固定比较对象、版本和共同维度；分别查官方事实，再找独立实测，不用不同版本或不同口径的数据硬比。"
        )
    if "recommendation" in modes:
        guidance.append(
            "这是推荐题：从用户约束提取判断标准，用官方参数加独立实测作答；过滤软文榜单和返利页，给有条件的选择而非堆产品名。"
        )
    if "freshness" in modes:
        guidance.append(
            "这是时效题：核对发布日期和事件发生日期，优先当前日期附近的官方消息与可信媒体，旧背景材料只能补充背景。"
        )
    if "official" in modes:
        guidance.append(
            "用户要官方资料：搜索具体实体、版本和文档主题，选择精确正文页；官网首页只作入口，不当作已经回答问题的证据。"
        )
    if "dual_region" in modes:
        guidance.append(
            "问题要求国内外覆盖：分别用简短中文查询和英文查询各搜一次，最终按同一主题合并，不把两边无关热点拼在一起。"
        )
    if "social" in modes:
        guidance.append(
            "用户明确点名社交平台时才用 x_search 获取平台原帖，再用 web_search 查原始公告或独立来源核实。"
        )
    else:
        guidance.append("本题未点名社交平台，不调用 x_search。")

    if browser_allowed:
        guidance.append(
            "任务另有明确网页交互要求时才使用 browser_*；浏览器不替代 web_search，也不用于绕过读取失败。"
        )
    else:
        guidance.append(
            "本任务没有网页交互要求，不调用 browser_*；页面读取失败时改选其他搜索结果。"
        )

    guidance.extend(
        [
            "最终先给结论，再给真正影响结论的事实和 2-4 个来源链接；标清日期与仍不确定的部分，不汇报搜索过程。",
            "本任务全部工具调用硬上限为 %d 次。" % int(tool_call_limit),
        ]
    )
    return (
        "\n研究日期为 %s（%s）。检索模式：%s。\n- "
        % (research_date, timezone, ",".join(classify_research_modes(message)))
        + "\n- ".join(guidance)
    )
