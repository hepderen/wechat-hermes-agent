from __future__ import annotations

import re
import unicodedata


URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

SEARCH_ACTION_RE = re.compile(
    r"(?:搜一搜|搜搜|搜一下|搜下|再搜(?:一遍|一次|一下)?|"
    r"查资料|查一查|查查|查一下|查下|找一找|找找|找一下|找下|"
    r"(?:请)?(?:优先|先|再)?\s*查\s*(?:独立)?"
    r"(?:实测|评测|测评|官方参数|官方文档|官方资料|原文|来源|证据)|"
    r"(?:搜索|检索|调研|研究)(?:一下|这个|该|关于|最近|最新|今天|国内|国外|\s+)|"
    r"(?:帮我|替我|给我)(?:去\s*)?(?:搜|查|找)|"
    r"(?:帮我|替我|给我)\s*(?:看看|看一下|看下)"
    r"(?:这个|下)?\s*(?:链接|网址|网页|网站|资料|新闻|文档)|"
    r"(?:上网|网上|联网)\s*(?:搜|查|找|看看|看一下|看下)|"
    r"(?:去|到)?\s*(?:x|twitter|推特|微博|知乎|小红书|"
    r"google|谷歌|bing|必应|百度)\s*(?:上)?\s*"
    r"(?:看看|看一下|看下|瞧瞧|搜|查|找|一下)|"
    r"^\s*(?:please\s+)?(?:research|search|look\s+up|find|browse)\b|"
    r"\b(?:search\s+for|look\s+up)\b)",
    re.IGNORECASE,
)

URL_ACTION_RE = re.compile(
    r"(?:帮我|替我|给我)?\s*"
    r"(?:看看|看一下|打开|访问|分析|总结|检查|读一下|查一下)"
    r"(?:这个|一下|下)?\s*(?:链接|网址|网页)?"
    r"|\b(?:open|visit|check|read|review|inspect|analy[sz]e|summari[sz]e)\b",
    re.IGNORECASE,
)

FRESHNESS_RE = re.compile(
    r"(?:最新|最近|近期|今天|今日|刚刚|刚才|现在|当前|目前|现行|实时|"
    r"本周|这周|本月|这个月|今年|截至(?:今天|目前|现在)?|"
    r"latest|recent|current(?:ly)?|today|right\s+now|"
    r"this\s+(?:week|month|year))",
    re.IGNORECASE,
)

TIME_SENSITIVE_FACT_RE = re.compile(
    r"(?:新闻|消息|热点|热搜|快讯|动态|近况|进展|更新|版本|发布|"
    r"公告|政策|法规|规定|价格|报价|现价|行情|汇率|股价|天气|气象|台风|预警|"
    r"比分|赛果|排名|榜单|票房|数据|统计|状态|故障|宕机|"
    r"release(?:\s+notes?)?|news|headline|update|version|announcement|"
    r"policy|regulation|price|quote|market|exchange\s+rate|stock|"
    r"weather|score|ranking|box\s+office|status|outage)",
    re.IGNORECASE,
)

INHERENTLY_LIVE_FACT_RE = re.compile(
    r"(?:新闻(?!行业|专业|学|稿|文案)|热搜|快讯|实时|现价|行情|"
    r"汇率|股价|天气|比分|赛果|"
    r"票房|宕机|news|headlines?|weather|exchange\s+rate|stock\s+price|"
    r"live\s+score|box\s+office|outage)",
    re.IGNORECASE,
)

VERIFICATION_RE = re.compile(
    r"(?:真假|真的假的|是真的吗|真吗|是否属实|属实吗|是不是真的|"
    r"靠谱吗|可信吗|核实|查证|"
    r"辟谣|事实核查|(?:来源|出处|依据)(?:呢|是|在|链接|有吗)|"
    r"官方(?:怎么说|回应|消息)|verify|fact[-\s]?check|is\s+.+\s+true)",
    re.IGNORECASE,
)

RECOMMENDATION_RE = re.compile(
    r"(?:推荐|选购|怎么选|值得买|值不值得|适合|性价比|排行|榜单|哪个好|"
    r"\brecommend(?:ation|ed|s)?\b|\bbest\b|\bworth\b|\bbuying\s+guide\b)",
    re.IGNORECASE,
)

EVIDENCE_QUALITY_RE = re.compile(
    r"(?:官方参数|官方文档|官方资料|官方公告|官网|原文|一手资料|"
    r"独立实测|横评|评测|测评|实测|"
    r"行业报告|市场报告|研究报告|白皮书|调研报告|统计数据|数据分析|趋势分析|"
    r"国内外|海内外|中外|中国和海外|中国与海外|"
    r"\bofficial\s+(?:parameters?|documentation|docs?|sources?|announcement)\b|"
    r"\bindependent\s+(?:test|review|benchmark)\b|"
    r"\b(?:industry|market|research)\s+report\b|\bwhite\s+paper\b|"
    r"\bdata\s+analysis\b|\btrend\s+analysis\b|"
    r"\bchina\s+(?:and|&)\s+(?:global|international|overseas)\b|"
    r"\bdomestic\s+(?:and|&)\s+(?:global|international)\b)",
    re.IGNORECASE,
)

CREATIVE_RECOMMENDATION_RE = re.compile(
    r"(?:文案|标题|名字|名称|变量名|函数名|措辞|说法|回复|句子|段落|"
    r"选题|创意|故事|小说|诗|\bcaption\b|\bname\b|\bwording\b|"
    r"\bcopy\b|\bidea\b|\bstory\b|\bpoem\b)",
    re.IGNORECASE,
)

RECOMMENDATION_PRODUCT_RE = re.compile(
    r"(?:手机|电脑|相机|耳机|平板|显卡|汽车|车型|家电|软件|工具|服务|"
    r"套餐|产品|设备|书|电影|餐厅|酒店|景点|预算|价格|价位|\d+\s*元|"
    r"\bphone\b|\bsmartphone\b|\blaptop\b|\bcamera\b|\bheadphones?\b|"
    r"\bsoftware\b|\btool\b|\bservice\b|\bproduct\b|\bdevice\b|"
    r"\bbook\b|\bmovie\b|\brestaurant\b|\bhotel\b|\bbudget\b|\bprice\b)",
    re.IGNORECASE,
)

CONCEPTUAL_QUESTION_RE = re.compile(
    r"^\s*(?:(?:请|please)\s*)?(?:"
    r"什么是|何为|.+?是什么意思|为什么|为何|"
    r"讲讲|聊聊|介绍(?:一下)?|解释(?:一下)?|说明(?:一下)?|科普(?:一下)?|"
    r"如何|怎么|怎样|"
    r"what\s+(?:is|are|does)|why\b|how\b|"
    r"explain\b|describe\b|tell\s+me\s+about\b|"
    r"(?:can|could|would)\s+you\s+(?:explain|describe|tell\s+me\s+about)\b|"
    r".+?(?:是什么|是做什么的|是怎么(?:工作|运行|实现)的|"
    r"有什么区别|原理(?:是什么)?|怎么回事|需要注意什么|"
    r"有哪些(?:常见)?(?:类型|格式|用途)?)\s*[?？]?$"
    r")",
    re.IGNORECASE,
)


def normalize_intent_text(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def is_explicit_research_request(value: str) -> bool:
    text = normalize_intent_text(value)
    if not text:
        return False
    if SEARCH_ACTION_RE.search(text):
        return True
    return bool(URL_RE.search(text) and URL_ACTION_RE.search(text))


def is_current_information_request(value: str) -> bool:
    text = normalize_intent_text(value)
    if not text:
        return False
    if VERIFICATION_RE.search(text):
        return True
    if FRESHNESS_RE.search(text) and TIME_SENSITIVE_FACT_RE.search(text):
        return True
    if CONCEPTUAL_QUESTION_RE.search(text):
        return False
    return bool(INHERENTLY_LIVE_FACT_RE.search(text))


def is_evidence_research_request(value: str) -> bool:
    text = normalize_intent_text(value)
    if not text:
        return False
    if EVIDENCE_QUALITY_RE.search(text):
        return True
    if not RECOMMENDATION_RE.search(text):
        return False
    if CREATIVE_RECOMMENDATION_RE.search(
        text
    ) and not RECOMMENDATION_PRODUCT_RE.search(text):
        return False
    return True


def is_research_request(value: str) -> bool:
    return (
        is_explicit_research_request(value)
        or is_current_information_request(value)
        or is_evidence_research_request(value)
    )


def is_conceptual_question(value: str) -> bool:
    text = normalize_intent_text(value)
    if not text or is_research_request(text):
        return False
    return bool(CONCEPTUAL_QUESTION_RE.search(text))
