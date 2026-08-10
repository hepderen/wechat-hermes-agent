from __future__ import annotations

import pytest

from app.policy import format_task, format_task_list, should_run_async


def test_blocked_task_status_is_visible_in_single_and_list_formats():
    task = {
        "id": "T-12345678",
        "status": "queued",
        "internal_state": "blocked_on_input",
    }

    assert format_task(task) == "任务 T-12345678：等待补充信息"
    assert "等待补充信息" in format_task_list([task])


@pytest.mark.parametrize(
    "message",
    (
        "Research the official Python documentation",
        "Search for the latest AI policy sources",
        "Browse the website and download the report",
        "Run this command and export the result",
        "Create a spreadsheet from these files",
        "Install the requested package",
    ),
)
def test_english_work_requests_use_the_execution_queue(message):
    assert should_run_async(message, "text", []) is True


@pytest.mark.parametrize(
    "message",
    (
        "上推特帮我搜一搜今天的 AI 热点新闻",
        "帮我查一下国务院最新政策",
        "再搜一次今天的 AI 热点",
        "找一下腾讯云官方大模型文档",
    ),
)
def test_colloquial_chinese_search_requests_use_the_execution_queue(message):
    assert should_run_async(message, "text", []) is True


@pytest.mark.parametrize(
    "message",
    (
        "去 X 上看看今天有什么 AI 热点",
        "推特上查查最近的大模型新闻",
        "搜下今天的科技新闻",
        "查查国务院最新政策",
        "找找 Python 3.14 的发布说明",
        "帮我看看这个链接 https://example.com/report",
        "帮我看看这个新闻",
        "上网看看今天的政策",
        "百度一下最近的 AI 新闻",
        "Please look up the latest OpenAI release notes",
    ),
)
def test_additional_explicit_research_phrases_use_the_execution_queue(message):
    assert should_run_async(message, "text", []) is True


@pytest.mark.parametrize(
    "message",
    (
        "今天有什么 AI 新闻",
        "最近的大模型热点有哪些",
        "国务院最新人工智能政策",
        "Python 当前最新版本是什么",
        "比特币现在什么价格",
        "上海今天天气",
        "这个消息是真的吗",
        "官方怎么说这件事",
        "What are today's AI headlines?",
        "What is the latest Python release?",
    ),
)
def test_time_sensitive_facts_use_the_execution_queue_without_search_verbs(
    message,
):
    assert should_run_async(message, "text", []) is True


@pytest.mark.parametrize(
    "message",
    (
        "现在这个方案怎么改",
        "最近心情不太好",
        "当前任务怎么取消",
        "写一段关于新闻行业的文案",
        "什么是新闻",
    ),
)
def test_non_web_uses_of_time_words_stay_in_normal_chat(message):
    assert should_run_async(message, "text", []) is False


@pytest.mark.parametrize(
    "message",
    (
        "什么是搜索引擎",
        "研究是什么意思",
        "讲讲浏览器原理",
        "如何部署",
        "视频编码原理是什么",
        "文件系统是做什么的",
        "浏览器是怎么工作的",
        "部署需要注意什么",
        "图片有哪些常见格式",
        "What is a search engine?",
        "Explain browser architecture",
        "Please explain browser architecture",
        "Could you explain browser architecture?",
        "How do deployments work?",
    ),
)
def test_conceptual_questions_stay_in_normal_chat(message):
    assert should_run_async(message, "text", []) is False


@pytest.mark.parametrize(
    "message",
    (
        "How are you?",
        "Explain the runtime behavior conceptually",
        "What does a test runner do?",
    ),
)
def test_english_conversation_does_not_match_partial_execution_words(message):
    assert should_run_async(message, "text", []) is False
