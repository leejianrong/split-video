"""The agent guide through MCP (in memory and over a real stdio subprocess) and the CLI: the same text everywhere."""

import sys

import pytest
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from split_video import agent_guide
from split_video.cli import app
from split_video.mcp_server import create_server

pytestmark = pytest.mark.anyio

PROMPTS = {"highlights_workflow", "vertical_short", "recover_from_error"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def resource_text(result) -> str:
    return "".join(part.text for part in result.contents)


async def check(client):
    prompts = {p.name: p for p in (await client.list_prompts()).prompts}
    assert set(prompts) == PROMPTS
    assert {a.name for a in prompts["vertical_short"].arguments or []} >= {"asset", "start", "end"}
    assert [r.uri for r in (await client.list_resources()).resources] == [agent_guide.GUIDE_URI]
    assert resource_text(await client.read_resource(agent_guide.GUIDE_URI)) == agent_guide.guide_text()

    got = await client.get_prompt("highlights_workflow", {"asset": "a_0123456789abcdef", "shorts": "3"})
    text = got.messages[0].content.text
    assert "a_0123456789abcdef" in text and "queue_renders" in text and "3 vertical shorts" in text
    got = await client.get_prompt("vertical_short", {"asset": "a_0123456789abcdef", "start": "10", "end": "40"})
    assert "seconds 10" in got.messages[0].content.text or "10.0 to 40.0" in got.messages[0].content.text
    got = await client.get_prompt("recover_from_error", {"error": '{"code": "revision.conflict"}'})
    assert "revision.conflict" in got.messages[0].content.text


async def test_prompts_and_resource_in_memory(tmp_path):
    server = create_server(tmp_path)
    assert agent_guide.GUIDE_URI in server.instructions
    async with Client(server) as client:
        await check(client)


async def test_prompts_and_resource_over_real_stdio(tmp_path):
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from split_video.cli import app; app()", "mcp", "--root", str(tmp_path)]
    )
    async with Client(params) as client:
        await check(client)


def test_cli_prints_the_guide():
    result = CliRunner().invoke(app, ["agent-guide"])
    assert result.exit_code == 0
    assert result.output == agent_guide.guide_text()
