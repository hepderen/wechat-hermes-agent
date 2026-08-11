from app.research import build_research_instructions, classify_research_modes


def instructions(message: str, *, browser_allowed: bool = False) -> str:
    return build_research_instructions(
        message,
        research_date="2026-08-12",
        timezone="Asia/Shanghai",
        tool_call_limit=12,
        browser_allowed=browser_allowed,
    )


def test_research_modes_can_combine_instead_of_forcing_one_label():
    assert classify_research_modes("核实今天国内外 AI 新闻真假并对比") == (
        "fact_check",
        "comparison",
        "freshness",
        "dual_region",
    )


def test_comparison_prompt_requires_same_versions_and_common_dimensions():
    value = instructions("对比两款折叠屏手机的续航，怎么选")

    assert "固定比较对象、版本和共同维度" in value
    assert "官方事实" in value
    assert "独立实测" in value
    assert "软文榜单和返利页" in value


def test_fact_check_prompt_requires_primary_source_and_counterevidence():
    value = instructions("核实这条消息是否属实")

    assert "原始发布方" in value
    assert "独立佐证或反证" in value
    assert "区分事实、推断和未证实说法" in value


def test_dual_region_prompt_requires_separate_language_queries():
    value = instructions("搜索今天国内外大模型新闻")

    assert "简短中文查询和英文查询各搜一次" in value
    assert "两边无关热点" in value


def test_research_without_browser_capability_never_falls_back_to_browser():
    value = instructions("研究 OpenAI 官方文档")

    assert "不调用 browser_*" in value
    assert "页面读取失败时改选其他搜索结果" in value
    assert "官网首页只作入口" in value


def test_explicit_browser_task_keeps_browser_bounded():
    value = instructions("搜索资料并打开网页检查", browser_allowed=True)

    assert "明确网页交互要求时才使用 browser_*" in value
    assert "浏览器不替代 web_search" in value


def test_social_search_uses_platform_tool_with_web_verification():
    value = instructions("上推特搜一下今天的 AI 热点")

    assert "x_search 获取平台原帖" in value
    assert "web_search 查原始公告或独立来源核实" in value


def test_product_name_with_x_is_not_misrouted_to_social_search():
    modes = classify_research_modes("荣耀 Magic V5 和 vivo X Fold5 续航对比")

    assert "comparison" in modes
    assert "social" not in modes
    assert "social" not in classify_research_modes("查一下在 XML 中配置命名空间")


def test_general_research_does_not_waste_social_calls():
    value = instructions("查一下 Python 类型系统资料")

    assert "未点名社交平台，不调用 x_search" in value
    assert "web_search 4 次" in value
    assert "web_extract 3 次" in value
    assert "一次 web_extract 调用中并行读取" in value
    assert "硬上限为 12 次" in value


def test_report_research_checks_methodology_instead_of_citing_mirrors():
    value = instructions("研究人工智能行业报告和统计数据")

    assert "报告或数据研究题" in value
    assert "发布日期、统计口径、样本和方法" in value
    assert "文档搬运站" in value
