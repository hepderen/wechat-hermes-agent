from __future__ import annotations

from app.evidence import (
    build_execution_plan,
    effective_tool_call_limit,
    normalize_run_event,
    verify_completion,
)


def completed_event(**changes):
    event = {
        "event_type": "tool.completed",
        "tool_name": "terminal",
        "exit_code": 0,
        "result_summary": "ok",
        "source": "",
        "artifact_id": "",
    }
    event.update(changes)
    return event


def test_execution_plan_separates_text_from_real_work():
    text = build_execution_plan("请把这段话润色一下")
    command = build_execution_plan("在终端运行部署命令")
    research = build_execution_plan("研究这个问题并给出来源")
    image = build_execution_plan("生成一张封面图片")

    assert text["task_type"] == "text_creation"
    assert text["requires_tool_evidence"] is False
    assert command["task_type"] == "command"
    assert command["requires_tool_evidence"] is True
    assert research["task_type"] == "research"
    assert "source_recorded" in research["success_conditions"]
    assert image["task_type"] == "file"
    assert image["delivery_policy"] == "requested_artifacts"
    assert image["expected_artifacts"] == ["image"]


def test_research_about_documentation_does_not_require_a_file_artifact():
    for message in (
        "研究 Python 官方文档并给出来源",
        "Research the official Python documentation and cite sources",
        "Search a PDF specification and summarize the sources",
    ):
        plan = build_execution_plan(message)
        assert plan["task_type"] == "research"
        assert plan["expected_artifacts"] == []
        assert "verified_artifact" not in plan["success_conditions"]
        verdict = verify_completion(
            plan,
            [
                completed_event(
                    tool_name="web_search",
                    source="https://docs.python.org/3/",
                )
            ],
            [],
            output="research result",
        )
        assert verdict["status"] == "succeeded"


def test_colloquial_social_search_requires_research_evidence():
    plan = build_execution_plan("上推特帮我搜一搜今天的 AI 热点新闻")

    assert plan["task_type"] == "research"
    assert plan["required_tools"] == ["research"]
    assert plan["requires_tool_evidence"] is True
    assert "source_recorded" in plan["success_conditions"]
    assert plan["max_tool_calls"] == 12


def test_research_variants_and_url_actions_require_source_evidence():
    for message in (
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
    ):
        plan = build_execution_plan(message)
        assert plan["task_type"] == "research"
        assert plan["required_tools"] == ["research"]
        assert plan["requires_tool_evidence"] is True
        assert plan["max_tool_calls"] == 12


def test_time_sensitive_questions_require_research_evidence_without_search_verbs():
    for message in (
        "今天有什么 AI 新闻",
        "国务院最新人工智能政策",
        "Python 当前最新版本是什么",
        "这个消息是真的吗",
        "What is the latest Python release?",
    ):
        plan = build_execution_plan(message)
        assert plan["task_type"] == "research"
        assert plan["required_tools"] == ["research"]
        assert plan["requires_tool_evidence"] is True
        assert "source_recorded" in plan["success_conditions"]


def test_conceptual_execution_terms_do_not_create_tool_requirements():
    for message in (
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
    ):
        plan = build_execution_plan(message)
        assert plan["task_type"] == "general"
        assert plan["capabilities"] == []
        assert plan["expected_artifacts"] == []
        assert plan["requires_tool_evidence"] is False
        assert plan["max_tool_calls"] is None


def test_plan_tool_limit_never_weakens_global_limit():
    research = build_execution_plan("搜索今天的 AI 新闻")
    command = build_execution_plan("运行服务器命令")

    assert effective_tool_call_limit(research, 80) == 12
    assert effective_tool_call_limit(research, 10) == 10
    assert effective_tool_call_limit(command, 80) == 80
    assert effective_tool_call_limit({"max_tool_calls": "invalid"}, 17) == 17


def test_explicit_file_output_still_requires_a_verified_artifact():
    for message in (
        "研究产品并生成 PDF 报告",
        "Create a spreadsheet file from the research",
        "导出结果",
        "Download the report",
    ):
        plan = build_execution_plan(message)
        assert "file" in plan["expected_artifacts"]
        assert "verified_artifact" in plan["success_conditions"]


def test_event_normalization_drops_arguments_and_redacts_secrets():
    event = normalize_run_event(
        {
            "event": "tool.completed",
            "tool": "terminal",
            "arguments": {"command": "cat private-key"},
            "result": {
                "exit_code": 0,
                "source": "https://example.com/result",
            },
            "summary": "Bearer abcdefghijklmnop",
        }
    )
    assert event["tool_name"] == "terminal"
    assert event["exit_code"] == 0
    assert event["source"] == "https://example.com/result"
    assert "arguments" not in event
    assert "abcdefghijklmnop" not in event["result_summary"]


def test_run_completed_without_command_evidence_fails():
    plan = build_execution_plan("运行服务器命令")
    result = verify_completion(
        plan,
        [],
        [],
        output="已经完成",
        run_status="completed",
    )
    assert result["status"] == "failed"
    assert "exit code 0" in result["reason"]


def test_command_requires_zero_exit_code():
    plan = build_execution_plan("运行服务器命令")
    failed = verify_completion(
        plan,
        [completed_event(exit_code=2)],
        [],
        output="done",
    )
    succeeded = verify_completion(
        plan,
        [completed_event(exit_code=0)],
        [],
        output="done",
    )
    assert failed["status"] == "failed"
    assert succeeded["status"] == "succeeded"


def test_research_requires_source_and_browser_requires_action():
    research = build_execution_plan("研究产品并给来源")
    assert verify_completion(
        research,
        [completed_event(tool_name="web_search", source="")],
        [],
        output="report",
    )["status"] == "failed"
    assert verify_completion(
        research,
        [
            completed_event(
                tool_name="web_search",
                source="https://example.com",
            )
        ],
        [],
        output="report",
    )["status"] == "succeeded"

    browser = build_execution_plan("用浏览器打开网页并点击")
    assert verify_completion(
        browser,
        [completed_event(tool_name="terminal")],
        [],
        output="done",
    )["status"] == "failed"
    assert verify_completion(
        browser,
        [completed_event(tool_name="browser_click")],
        [],
        output="done",
    )["status"] == "succeeded"


def test_only_real_hermes_tool_names_satisfy_execution_evidence():
    command = build_execution_plan("运行服务器命令")
    research = build_execution_plan("研究产品并给来源")
    browser = build_execution_plan("用浏览器点击网页")

    for tool_name in ("shell", "command", "exec", "bash", "sh", "powershell"):
        assert verify_completion(
            command,
            [completed_event(tool_name=tool_name, exit_code=0)],
            [],
            output="done",
        )["status"] == "failed"
    assert verify_completion(
        command,
        [completed_event(tool_name="execute_code", exit_code=0)],
        [],
        output="done",
    )["status"] == "succeeded"

    for tool_name in ("research", "search", "retrieval", "browser_search"):
        assert verify_completion(
            research,
            [
                completed_event(
                    tool_name=tool_name,
                    source="https://example.com",
                )
            ],
            [],
            output="report",
        )["status"] == "failed"
    assert verify_completion(
        research,
        [
            completed_event(
                tool_name="web_extract",
                source="https://example.com",
            )
        ],
        [],
        output="report",
    )["status"] == "succeeded"

    for tool_name in ("browser", "playwright", "webpage", "navigate", "click"):
        assert verify_completion(
            browser,
            [completed_event(tool_name=tool_name)],
            [],
            output="done",
        )["status"] == "failed"
    assert verify_completion(
        browser,
        [completed_event(tool_name="browser_snapshot")],
        [],
        output="done",
    )["status"] == "succeeded"


def test_file_task_requires_verified_matching_artifact():
    plan = build_execution_plan("生成一张图片")
    unverified = {
        "mime_type": "image/png",
        "verified": 0,
        "size_bytes": 100,
        "sha256": "a" * 64,
    }
    verified = dict(unverified, verified=1)
    assert verify_completion(
        plan,
        [completed_event(tool_name="files")],
        [unverified],
        output="done",
    )["status"] == "failed"
    assert verify_completion(
        plan,
        [completed_event(tool_name="files")],
        [verified],
        output="done",
    )["status"] == "succeeded"


def test_pure_text_tasks_do_not_require_tool_evidence():
    plan = build_execution_plan("写一段纯文本广告文案")
    assert verify_completion(plan, [], [], output="文案")["status"] == "succeeded"
    assert verify_completion(plan, [], [], output="")["status"] == "failed"


def test_single_blocking_question_releases_worker():
    plan = build_execution_plan("运行部署命令")
    result = verify_completion(
        plan,
        [],
        [],
        output="请提供目标服务器地址",
    )
    assert result["status"] == "blocked_on_input"


def test_adversarial_tool_names_do_not_satisfy_execution_evidence():
    command = build_execution_plan("运行服务器命令")
    research = build_execution_plan("研究产品并给来源")
    browser = build_execution_plan("用浏览器点击网页")

    assert verify_completion(
        command,
        [completed_event(tool_name="fake_terminal", exit_code=0)],
        [],
        output="done",
    )["status"] == "failed"
    assert verify_completion(
        research,
        [
            completed_event(
                tool_name="untrusted_search",
                source="https://example.com",
            )
        ],
        [],
        output="report",
    )["status"] == "failed"
    assert verify_completion(
        browser,
        [completed_event(tool_name="pretend-playwright")],
        [],
        output="done",
    )["status"] == "failed"


def test_all_requested_artifact_types_are_required():
    plan = build_execution_plan("生成图片和视频")
    image = {
        "mime_type": "image/png",
        "verified": 1,
        "size_bytes": 100,
        "sha256": "a" * 64,
    }
    video = {
        "mime_type": "video/mp4",
        "verified": 1,
        "size_bytes": 100,
        "sha256": "b" * 64,
    }
    tool_event = [completed_event(tool_name="files")]

    missing = verify_completion(plan, tool_event, [image], output="done")
    complete = verify_completion(plan, tool_event, [image, video], output="done")

    assert missing["status"] == "failed"
    assert "video" in missing["reason"]
    assert complete["status"] == "succeeded"


def test_compound_task_requires_each_declared_success_condition():
    plan = build_execution_plan("研究这个产品并生成 PDF 文件，给出来源")
    assert plan["task_type"] == "compound"
    assert set(plan["required_tools"]) == {"research", "files"}
    assert set(plan["success_conditions"]) >= {
        "source_recorded",
        "verified_artifact",
    }
    artifact = {
        "mime_type": "application/pdf",
        "verified": 1,
        "size_bytes": 100,
        "sha256": "a" * 64,
    }

    no_source = verify_completion(
        plan,
        [completed_event(tool_name="files")],
        [artifact],
        output="done",
    )
    no_artifact = verify_completion(
        plan,
        [
            completed_event(
                tool_name="web_search",
                source="https://example.com",
            )
        ],
        [],
        output="done",
    )
    complete = verify_completion(
        plan,
        [
            completed_event(
                tool_name="web_search",
                source="https://example.com",
            )
        ],
        [artifact],
        output="done",
    )

    assert no_source["status"] == "failed"
    assert no_artifact["status"] == "failed"
    assert complete["status"] == "succeeded"


def test_generic_external_action_cannot_succeed_without_tool_event():
    plan = build_execution_plan("发送这份内容到外部系统")
    assert plan["task_type"] == "external"
    assert plan["requires_tool_evidence"] is True
    assert verify_completion(plan, [], [], output="已发送")["status"] == "failed"
    assert verify_completion(
        plan,
        [completed_event(tool_name="controlled_sender")],
        [],
        output="已发送",
    )["status"] == "succeeded"
