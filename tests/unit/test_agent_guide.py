"""The agent guide cannot rot: every tool it names is a registered MCP tool and every example call is valid input."""

import asyncio
import json
import re

import jsonschema
import pytest

from split_video import agent_guide
from split_video.mcp_server import create_server

TOOL_REF = re.compile(r"`([a-z][a-z0-9_]*)\(\)`")
CALL_BLOCK = re.compile(r"```call ([a-z_0-9]+)\n(.*?)\n```", re.DOTALL)


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    server = create_server(tmp_path_factory.mktemp("root"))
    return {t.name: t for t in asyncio.run(server.list_tools())}


def schema_of(tool) -> dict:
    return getattr(tool, "input_schema", None) or tool.inputSchema


def test_every_tool_the_guide_names_is_registered(tools):
    named = set(TOOL_REF.findall(agent_guide.guide_text()))
    assert len(named) >= 25, "the guide should name the tools it relies on, written as `tool_name()`"
    assert named - set(tools) == set()


def test_the_prompts_name_only_registered_tools(tools):
    texts = [
        agent_guide.highlights_workflow("a_0123456789abcdef", shorts=2),
        agent_guide.vertical_short("a_0123456789abcdef", 10, 40),
        agent_guide.recover_from_error("x"),
    ]
    for text in texts:
        named = set(TOOL_REF.findall(text))
        assert named and named - set(tools) == set()


def test_every_example_call_is_valid_input_for_its_tool(tools):
    blocks = CALL_BLOCK.findall(agent_guide.guide_text())
    assert len(blocks) >= 10
    for name, body in blocks:
        assert name in tools, f"example calls unknown tool {name}"
        arguments = json.loads(body)
        schema = schema_of(tools[name])
        assert set(arguments) <= set(schema["properties"]), f"{name}: unknown argument in the example"
        jsonschema.validate(arguments, schema)


def test_the_guide_has_skill_front_matter_and_the_required_sections():
    text = agent_guide.guide_text()
    assert text.startswith("---\nname: split-video\ndescription: ")
    for heading in (
        "The order that works",
        "Worked example",
        "Sizes, paging",
        "Costs",
        "Revisions",
        "Errors",
        "Do not",
    ):
        assert f"## {heading}" in text


def test_error_codes_in_the_guide_exist_in_the_catalogue():
    from split_video.error_catalogue import REGISTRY

    families = {code.split(".")[0] for code in REGISTRY}
    mentioned = {c for c in re.findall(r"`([a-z]+\.[a-z_]+)`", agent_guide.guide_text()) if c.split(".")[0] in families}
    assert len(mentioned) >= 10
    assert mentioned - set(REGISTRY) == set()
