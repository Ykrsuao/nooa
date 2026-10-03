# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""ACP adapter for the host-neutral NOOA coding agent."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from acp import (
    PROTOCOL_VERSION,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    RequestError,
    run_agent,
    text_block,
    update_agent_message,
    update_user_message,
)
from acp.helpers import update_available_commands
from acp.interfaces import Agent, Client
from acp.schema import (
    AgentCapabilities,
    AvailableCommand,
    AvailableCommandInput,
    CloseSessionResponse,
    HttpMcpServer,
    Implementation,
    ListSessionsResponse,
    McpCapabilities,
    McpServerStdio,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionListCapabilities,
    SseMcpServer,
    UnstructuredCommandInput,
)
from acp.schema import (
    SessionInfo as ACPSessionInfo,
)
from nooa_cli.coding import (
    CodingAgent,
    CodingSlashCommand,
    CodingSlashCommandRegistry,
    load_coding_skills_dirs,
)
from nooa_cli.coding.sandbox_agent import SandboxCodingAgent
from nooa_cli.sessions import (
    InvalidSessionIdError,
    SessionHandle,
    SessionNotFoundError,
    SessionStore,
)

from nooa import Context
from nooa.config import CodeActConfig
from nooa.errors import GenerationError
from nooa.interactive import Done, InteractiveAgent, NeedInput, Waiting
from nooa.mcp import MCPManager, MCPTool
from nooa.paths import get_user_dir
from nooa.runtime.sandbox import SandboxConfig, SandboxSession
from nooa.slash_dispatch import CoercionError
from nooa.strategies.codeact import MAX_ITERATIONS_MESSAGE, OUTPUT_TOKENS_EXHAUSTED_MESSAGE
from nooa.unifiedllm import UnifiedLLM
from nooa_acp._runtime import (
    SessionBusyError,
    SessionRuntime,
    SessionRuntimeClosedError,
    SessionRuntimePool,
)
from nooa_acp._sandbox_namespace import coding_sandbox_globals
from nooa_acp.dispatcher import InteractiveSessionDispatcher
from nooa_acp.event_bridge import ACPEventBridge

logger = logging.getLogger(__name__)

_SESSION_PAGE_SIZE = 50
_GENERATION_LIMIT_PREFIX = MAX_ITERATIONS_MESSAGE.partition("{")[0]


@dataclass(slots=True)
class _ACPSession:
    """Live resources owned by one ACP session runtime."""

    handle: SessionHandle
    agent: InteractiveAgent
    dispatcher: InteractiveSessionDispatcher
    bridge: ACPEventBridge
    commands: CodingSlashCommandRegistry
    sandbox: SandboxSession | None = None
    startup_warnings: tuple[str, ...] = ()
    cancel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancel_complete: asyncio.Event = field(default_factory=asyncio.Event)
    notification_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    commands_sent_on_prompt: bool = False

    def __post_init__(self) -> None:
        self.cancel_complete.set()

    async def close(self) -> None:
        for task in self.notification_tasks:
            task.cancel()
        if self.notification_tasks:
            await asyncio.gather(*self.notification_tasks, return_exceptions=True)
        self.notification_tasks.clear()
        # A failed native close must keep storage and all owner objects alive
        # for an adapter.close() retry. Do not close the handle in a finally.
        await self.dispatcher.cancel()
        if self.sandbox is not None:
            await self.sandbox.aclose()
            self.sandbox = None
        await self.dispatcher.close()
        await self.bridge.close()
        self.commands.close()
        self.handle.close()


@dataclass(slots=True, eq=False)
class _PendingSession:
    """Retain resources even if provisioning fails before runtime registration."""

    handle: SessionHandle
    llm: UnifiedLLM
    agent: InteractiveAgent | None = None
    value: _ACPSession | None = None
    dispatcher: InteractiveSessionDispatcher | None = None
    bridge: ACPEventBridge | None = None
    commands: CodingSlashCommandRegistry | None = None
    sandbox: SandboxSession | None = None
    _owner: SessionRuntime[_PendingSession] = field(init=False)

    def __post_init__(self) -> None:
        self._owner = SessionRuntime(
            self.handle.id, self, close=lambda pending: pending._close_resources()
        )

    async def close(self) -> None:
        await self._owner.close()

    async def _close_resources(self) -> None:
        if self.value is not None:
            await self.value.close()
            return
        if self.dispatcher is not None:
            await self.dispatcher.cancel()
        if self.sandbox is not None:
            await self.sandbox.aclose()
            self.sandbox = None
        if self.dispatcher is not None:
            await self.dispatcher.close()
        elif self.agent is not None:
            await self.agent.close()
        else:
            await self.llm.aclose()
        if self.bridge is not None:
            await self.bridge.close()
        if self.commands is not None:
            self.commands.close()
        self.handle.close()


class CodingACPAdapter:
    def __init__(
        self,
        llm_factory: Callable[[], UnifiedLLM],
        *,
        sandbox: str = "off",
        sandbox_mode: str = "strict",
        sandbox_network: str = "off",
    ) -> None:
        if sandbox not in ("off", "auto", "linux", "windows"):
            raise ValueError("sandbox must be off, auto, linux or windows")
        if sandbox_mode not in ("code", "strict"):
            raise ValueError("sandbox_mode must be code or strict")
        if sandbox_network not in ("off", "on"):
            raise ValueError("sandbox_network must be off or on")
        if sandbox_network == "on" and (sandbox == "off" or sandbox_mode != "code"):
            raise ValueError("sandbox_network on requires an enabled sandbox in code mode")
        if sandbox != "off":
            native = {"linux": "linux", "win32": "windows"}.get(sys.platform)
            if native is None or sandbox not in ("auto", native):
                raise ValueError("ACP sandbox requires the native Linux or Windows backend")
        self._sandbox = sandbox
        self._sandbox_mode = sandbox_mode
        self._sandbox_network = sandbox_network
        self._llm_factory = llm_factory
        self._client: Client | None = None
        self._sessions: SessionRuntimePool[_ACPSession] = SessionRuntimePool()
        self._pending: list[_PendingSession] = []

    def on_connect(self, conn: Client) -> None:
        self._client = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        del protocol_version, client_capabilities, client_info, kwargs
        try:
            package_version = version("nooa-acp")
        except PackageNotFoundError:
            package_version = "0.0.0"
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                # McpCapabilities defaults to all-false, and a client that
                # honours the handshake then filters its HTTP/SSE servers out of
                # session/new — so the agent receives no MCP servers at all,
                # however the user configured them. _create_mcp_tools connects
                # both transports, so say so. `acp` stays off: it is unstable in
                # the spec and not implemented here.
                mcp_capabilities=McpCapabilities(
                    http=not self._strict_sandbox, sse=not self._strict_sandbox
                ),
                session_capabilities=SessionCapabilities(
                    list=SessionListCapabilities(),
                    close=SessionCloseCapabilities(),
                ),
            ),
            auth_methods=[],
            agent_info=Implementation(
                name="nooa-acp",
                title="NVIDIA Labs Object Oriented Agents (NOOA)",
                version=package_version,
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        del kwargs
        root = self._validate_workspace(cwd, additional_directories)
        self._check_mcp(mcp_servers)
        llm = self._llm_factory()
        try:
            handle = self._store(root).create(
                model=llm.model,
                agent="SandboxCodingAgent" if self._strict_sandbox else "CodingAgent",
                working_directory=str(root),
                origin="acp",
                check_same_thread=False,
            )
        except BaseException:
            await llm.aclose()
            raise
        try:
            runtime = await self._create_runtime(handle, root, mcp_servers, llm=llm)
        except BaseException:
            if not any(pending.handle is handle for pending in self._pending):
                handle.close()
                self._store(root).delete(handle.id)
            raise
        self._defer_bootstrap_updates(runtime.value)
        return NewSessionResponse(session_id=handle.id)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        del kwargs
        root = self._validate_workspace(cwd, additional_directories)
        self._check_mcp(mcp_servers)
        if any(pending.handle.id == session_id for pending in self._pending):
            raise RequestError.invalid_request(
                {"sessionId": session_id, "reason": "Session startup cleanup is still pending"}
            )
        try:
            handle = self._store(root).open(session_id, check_same_thread=False)
        except (InvalidSessionIdError, SessionNotFoundError):
            raise RequestError.resource_not_found(session_id) from None
        runtime: SessionRuntime[_ACPSession] | None = None
        try:
            runtime = await self._create_runtime(handle, root, mcp_servers, available=False)
            # After the replay, never during it: replay writes straight to the
            # client while the bridge pump drains bootstrap updates, so every
            # await here would otherwise let a commands update or an MCP warning
            # land in the middle of the restored conversation.
            await self._replay_session(handle)
            await self._sessions.publish(session_id)
            self._defer_bootstrap_updates(runtime.value)
        except BaseException:
            if runtime is not None:
                with suppress(KeyError):
                    await self._sessions.remove(session_id, include_unavailable=True)
            elif not any(pending.handle is handle for pending in self._pending):
                handle.close()
            raise
        return LoadSessionResponse()

    async def list_sessions(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        del kwargs
        root = self._validate_workspace(cwd or str(Path.cwd()), None)
        try:
            offset = int(cursor) if cursor is not None else 0
        except ValueError:
            raise RequestError.invalid_params(
                {"cursor": cursor, "reason": "Invalid cursor"}
            ) from None
        if offset < 0:
            raise RequestError.invalid_params({"cursor": cursor, "reason": "Invalid cursor"})

        found = self._store(root).list(limit=offset + _SESSION_PAGE_SIZE + 1)
        page = found[offset : offset + _SESSION_PAGE_SIZE]
        sessions = [
            ACPSessionInfo(
                session_id=info.id,
                cwd=info.working_directory or str(root),
                title=info.title,
                updated_at=datetime.fromtimestamp(info.last_active, UTC).isoformat(),
            )
            for info in page
        ]
        next_cursor = str(offset + len(page)) if len(found) > offset + len(page) else None
        return ListSessionsResponse(sessions=sessions, next_cursor=next_cursor)

    async def close_session(self, session_id: str, **kwargs: Any) -> CloseSessionResponse:
        del kwargs
        try:
            await self._sessions.remove(session_id)
        except KeyError:
            raise RequestError.resource_not_found(session_id) from None
        return CloseSessionResponse()

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        del kwargs
        runtime = await self._get_runtime(session_id)
        text = self._prompt_text(prompt)
        try:
            async with runtime.turn():
                session = runtime.value
                session.handle.record_user_message(text)
                session.cancel_complete.clear()
                try:
                    if not session.commands_sent_on_prompt:
                        session.bridge.publish(
                            _available_commands_update(session.commands.commands())
                        )
                        session.commands_sent_on_prompt = True
                    slash = self._slash_invocation(session.commands, text)
                    if slash is None:
                        result = await session.dispatcher.submit(text)
                    else:
                        name, raw_args = slash
                        try:
                            submission = await session.dispatcher.invoke_slash(
                                session.commands,
                                name,
                                raw_args,
                            )
                        except CoercionError as exc:
                            message = f"/{name}: {exc.message}"
                            if exc.hint:
                                message += f"\n\nUsage: `/{name} {exc.hint}`"
                            session.agent.message(message)
                            await session.bridge.flush()
                            return PromptResponse(stop_reason="end_turn")
                        except GenerationError:
                            # Subclasses Exception, so the catch-all below would
                            # swallow it and lose the stop reason the outer
                            # handler maps. Generation limits are the runtime's
                            # to report, not a command failure.
                            raise
                        except Exception as exc:
                            # Command bodies are third-party code from workspace
                            # and installed skills. Letting one raise turns the
                            # whole prompt into a JSON-RPC internal_error, and
                            # the user's turn is already durably recorded — so
                            # the session replays a question with no answer.
                            # The same failure inside execute_python is caught by
                            # the strategy and shown to the model; this path had
                            # no equivalent.
                            logger.warning(
                                "Slash command /%s failed in session %s",
                                name,
                                session_id,
                                exc_info=True,
                            )
                            session.agent.message(f"/{name} failed: {exc}")
                            await session.bridge.flush()
                            return PromptResponse(stop_reason="end_turn")
                        if submission is None:
                            result = None
                        else:
                            slash_result, result = submission
                            if not slash_result.output_to_agent:
                                message = str(slash_result)
                                if message:
                                    session.agent.message(message)
                                await session.bridge.flush()
                                return PromptResponse(stop_reason="end_turn")
                except GenerationError as exc:
                    # The strategy does not guarantee a PythonOutput for a call
                    # it already announced, so a turn ending on a generation
                    # limit can leave its card in_progress. Nothing else closes
                    # it before session close, and a later cancel would retitle
                    # this turn's stale card "Cancelled".
                    await session.bridge.fail_open_tools("Did not finish.", title="Unfinished")
                    await session.bridge.flush()
                    message = str(exc)
                    if message == OUTPUT_TOKENS_EXHAUSTED_MESSAGE:
                        return PromptResponse(stop_reason="max_tokens")
                    # MAX_ITERATIONS_MESSAGE and the max_retries message share
                    # this prefix; the limit name tells them from other errors.
                    if message.startswith(_GENERATION_LIMIT_PREFIX) and (
                        "max_iterations=" in message or "max_retries=" in message
                    ):
                        return PromptResponse(stop_reason="max_turn_requests")
                    raise RequestError(-32603, message, {"details": message}) from exc
                if result is None:
                    await session.cancel_complete.wait()
                    # stop_reason and the tool card both carry the outcome, but
                    # a collapsed card shows nothing and the turn just goes
                    # quiet. Record it as a real message so the conversation —
                    # and the durable transcript on resume — says what happened.
                    session.agent.message("Stopped at your request.")
                    await session.bridge.flush()
                    return PromptResponse(stop_reason="cancelled")
                await session.bridge.flush()
                return PromptResponse(stop_reason="end_turn")
        except SessionBusyError:
            raise RequestError.invalid_request(
                {"sessionId": session_id, "reason": "A prompt is already running"}
            ) from None
        except SessionRuntimeClosedError:
            # The session was closed between _get_runtime and the turn claim.
            # It is gone as far as the client is concerned, so say so rather
            # than letting this escape as an opaque internal error.
            raise RequestError.resource_not_found(session_id) from None

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        runtime = await self._get_runtime(session_id)
        session = runtime.value
        async with session.cancel_lock:
            try:
                if await session.dispatcher.cancel():
                    await session.bridge.fail_open_tools("Cancelled by user.", title="Cancelled")
                    await session.bridge.flush()
            finally:
                session.cancel_complete.set()

    async def _create_runtime(
        self,
        handle: SessionHandle,
        root: Path,
        mcp_servers: list[Any] | None,
        *,
        llm: UnifiedLLM | None = None,
        available: bool = True,
    ) -> SessionRuntime[_ACPSession]:
        llm = llm or self._llm_factory()
        if self._client is None:
            await llm.aclose()
            raise RequestError.internal_error({"reason": "ACP client is not connected"})
        agent: CodingAgent | SandboxCodingAgent | None = None
        commands: CodingSlashCommandRegistry | None = None
        value: _ACPSession | None = None
        pending = _PendingSession(handle, llm)
        self._pending.append(pending)
        try:
            self._check_mcp(mcp_servers)
            registration_warnings: list[str] = []
            if not self._strict_sandbox:
                mcp, mcp_warnings = await self._create_mcp_tools(mcp_servers)
                agent = CodingAgent(
                    llm=llm,
                    cwd=root,
                    storage=handle.storage,
                    libs_dir=root / ".nooa" / "libs",
                    skills_dirs=load_coding_skills_dirs(root),
                )
                pending.agent = agent
                for name, tool in mcp.items():
                    registry_name = f"mcp.{name}"
                    try:
                        agent.skills.register(registry_name, tool)
                        agent.skills.activate([registry_name])
                    except ValueError as exc:
                        registration_warnings.append(
                            f"MCP server {name!r} was not registered: {exc}"
                        )
                native_strategy = None
                if self._sandbox != "off":
                    sandbox = self._code_sandbox()
                    pending.sandbox = sandbox
                    await sandbox.__aenter__()
                    if sandbox.backend == "windows":
                        native_strategy = sandbox.strategy(
                            module_globals=coding_sandbox_globals(),
                            data_types=(Done, NeedInput, Waiting),
                        )
                    else:
                        native_strategy = sandbox.strategy()
                    agent.context["sandbox_execution_mode"] = Context(
                        "Generated Python runs in a native sandbox. Calls through self, including "
                        "shell, skills and MCP, execute on the host with their usual access. "
                        f"Generated Python network access is {self._sandbox_network}; this setting "
                        "does not restrict host tools or the model connection.",
                        prefix=True,
                    )
                dispatcher = InteractiveSessionDispatcher(agent, strategy=native_strategy)
            else:
                mcp_warnings = ()
                agent = SandboxCodingAgent(
                    llm=llm, cwd=root, storage=handle.storage, backend=self._sandbox
                )
                pending.agent = agent
                await agent.start()
                dispatcher = InteractiveSessionDispatcher(agent)
            pending.dispatcher = dispatcher
            bridge = ACPEventBridge(agent, self._client, handle.id)
            pending.bridge = bridge
            commands = CodingSlashCommandRegistry(agent)
            pending.commands = commands
            value = _ACPSession(
                handle,
                agent,
                dispatcher,
                bridge,
                commands,
                sandbox=pending.sandbox,
                startup_warnings=(*mcp_warnings, *registration_warnings),
            )
            pending.value = value
            commands.set_on_change(
                lambda available: bridge.publish(_available_commands_update(available)),
            )
            try:
                runtime = await self._sessions.add(handle.id, value, available=available)
                self._pending.remove(pending)
                return runtime
            except ValueError:
                raise RequestError.invalid_request(
                    {"sessionId": handle.id, "reason": "Session is already loaded"}
                ) from None
        except BaseException:
            await self._close_pending(pending)
            raise

    async def _close_pending(self, pending: _PendingSession) -> None:
        await pending.close()
        if pending in self._pending:
            self._pending.remove(pending)

    def _check_mcp(self, servers: list[Any] | None) -> None:
        if self._strict_sandbox and servers:
            raise RequestError.invalid_params(
                {"reason": "MCP servers are not supported in ACP strict sandbox mode"}
            )

    @property
    def _strict_sandbox(self) -> bool:
        return self._sandbox != "off" and self._sandbox_mode == "strict"

    def _code_sandbox(self) -> SandboxSession:
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            options["application_requirements"] = ("nooa-cli",)
        return SandboxSession(
            SandboxConfig(network=self._sandbox_network == "on", broker_timeout_s=120),
            backend=cast(Any, self._sandbox),
            config=CodeActConfig(cell_timeout=30),
            **options,
        )

    async def _create_mcp_tools(
        self,
        mcp_servers: list[Any] | None,
    ) -> tuple[dict[str, MCPTool], tuple[str, ...]]:
        servers = list(mcp_servers or [])
        supported_types = (McpServerStdio, HttpMcpServer, SseMcpServer)
        tools: dict[str, MCPTool] = {}
        warnings: list[str] = []
        seen_names: set[str] = set()
        for server in servers:
            name = getattr(server, "name", "<unnamed>")
            if not isinstance(server, supported_types):
                warnings.append(
                    f"MCP server {name!r} was not loaded: unsupported ACP server type "
                    f"{type(server).__name__}."
                )
                continue
            if name in seen_names:
                warnings.append(
                    f"MCP server {name!r} was not loaded: another server has the same name."
                )
                continue
            seen_names.add(name)
            try:
                if isinstance(server, McpServerStdio):
                    env = {item.name: item.value for item in server.env}
                    tools[name] = await MCPManager.create_stdio_server(
                        name,
                        command=server.command,
                        args=server.args,
                        env=env,
                    )
                elif isinstance(server, (HttpMcpServer, SseMcpServer)):
                    headers = {item.name: item.value for item in server.headers}
                    tools[name] = await MCPManager.create_url_server(
                        name,
                        server.url,
                        headers=headers,
                        transport=(
                            "streamable-http" if isinstance(server, HttpMcpServer) else "sse"
                        ),
                    )
            except Exception as exc:
                warnings.append(f"MCP server {name!r} was not loaded: {exc}")
        return tools, tuple(warnings)

    async def _get_runtime(self, session_id: str) -> SessionRuntime[_ACPSession]:
        try:
            return await self._sessions.get(session_id)
        except KeyError:
            raise RequestError.resource_not_found(session_id) from None

    @staticmethod
    def _defer_bootstrap_updates(session: _ACPSession) -> None:
        """Publish bootstrap updates after the session response can reach clients such as Zed."""

        async def _publish() -> None:
            await asyncio.sleep(0)
            session.bridge.publish_best_effort(
                _available_commands_update(session.commands.commands())
            )
            if session.startup_warnings:
                details = "\n".join(f"- {warning}" for warning in session.startup_warnings)
                session.bridge.publish_best_effort(
                    update_agent_message(
                        text_block(
                            "NOOA started without one or more MCP servers. The session is still "
                            f"usable.\n\n{details}"
                        )
                    )
                )
            await session.bridge.flush()

        task = asyncio.create_task(_publish(), name="nooa-acp-bootstrap")
        session.notification_tasks.add(task)

        def _finished(done: asyncio.Task[None]) -> None:
            session.notification_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(_finished)

    async def _replay_session(self, handle: SessionHandle) -> None:
        if self._client is None:
            return
        for turn in handle.turns():
            # Each update is a *chunk*, and ACP has no end-of-message marker —
            # a boundary is implied by a different update type arriving. Two
            # turns from the same speaker in a row therefore land in one bubble.
            # That happens whenever a turn produced no reply, as a cancelled one
            # used to, so several stopped prompts replayed as a single run-on
            # line. Terminate each turn so it keeps its own boundary.
            content = turn.content if turn.content.endswith("\n") else turn.content + "\n"
            block = text_block(content)
            update = (
                update_user_message(block) if turn.role == "user" else update_agent_message(block)
            )
            await self._client.session_update(handle.id, update)

    @staticmethod
    def _validate_workspace(cwd: str, additional_directories: list[str] | None) -> Path:
        if additional_directories:
            raise RequestError.invalid_params(
                {"reason": "Additional directories are not supported"}
            )
        root = Path(cwd).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise RequestError.invalid_params(
                {"cwd": cwd, "reason": "cwd must be an existing absolute directory"}
            )
        return root.resolve()

    def _store(self, root: Path) -> SessionStore:
        if self._sandbox == "off":
            return SessionStore(root / ".nooa" / "sessions")
        canonical = root.resolve()
        key = hashlib.sha256(os.path.normcase(str(canonical)).encode("utf-8")).hexdigest()
        storage_kind = (
            "acp-sandbox-sessions" if self._strict_sandbox else "acp-code-sandbox-sessions"
        )
        directory = get_user_dir(storage_kind, key).resolve()
        if directory.is_relative_to(canonical):
            raise RequestError.invalid_params(
                {"reason": "Sandbox session storage must be outside the granted workspace"}
            )
        return SessionStore(directory)

    @staticmethod
    def _prompt_text(prompt: list[Any]) -> str:
        parts: list[str] = []
        for block in prompt:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                parts.append(block.text)
            elif block_type == "resource_link":
                parts.append(f"Resource {block.name}: {block.uri}")
            else:
                raise RequestError.invalid_params(
                    {"reason": f"Unsupported prompt content type: {block_type!r}"}
                )
        text = "\n\n".join(parts)
        if not text.strip():
            raise RequestError.invalid_params({"reason": "Prompt text must not be empty"})
        return text

    @staticmethod
    def _slash_invocation(
        commands: CodingSlashCommandRegistry,
        text: str,
    ) -> tuple[str, str] | None:
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        command_text = stripped[1:]
        parts = command_text.split(maxsplit=1)
        name = parts[0].lower() if parts else ""
        if not name or commands.get(name) is None:
            return None
        return name, parts[1] if len(parts) == 2 else ""

    async def close(self) -> None:
        results = await asyncio.gather(
            self._sessions.close(),
            *(self._close_pending(pending) for pending in tuple(self._pending)),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise BaseExceptionGroup("Failed to close ACP resources", failures)


async def serve(
    llm_factory: Callable[[], UnifiedLLM],
    *,
    sandbox: str = "off",
    sandbox_mode: str = "strict",
    sandbox_network: str = "off",
) -> None:
    adapter = CodingACPAdapter(
        llm_factory,
        sandbox=sandbox,
        sandbox_mode=sandbox_mode,
        sandbox_network=sandbox_network,
    )
    try:
        # session/close is registered by the router as unstable. initialize()
        # advertises the close capability, so without this flag the agent
        # promises a method that answers "method not found", and a client can
        # never release a session. session/list is stable and unaffected.
        await run_agent(cast(Agent, adapter), use_unstable_protocol=True)
    finally:
        with suppress(Exception):
            await adapter.close()


def _available_commands_update(commands: tuple[CodingSlashCommand, ...]):
    available: list[AvailableCommand] = []
    for command in commands:
        input_spec = (
            AvailableCommandInput(
                UnstructuredCommandInput(hint=command.argument_hint),
            )
            if command.argument_hint
            else None
        )
        available.append(
            AvailableCommand(
                name=command.name,
                description=command.description,
                input=input_spec,
            )
        )
    return update_available_commands(available)
