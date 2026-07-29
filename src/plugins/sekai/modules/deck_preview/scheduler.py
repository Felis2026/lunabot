from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from ....utils import (
    compress_zstd,
    get_client_session,
    get_exc_desc,
)
from ...common import config, logger

from .protocol import (
    PREVIEW_DATA_SCOPE,
    PREVIEW_NATIVE_REGION,
    PreviewRuntimeState,
    PreviewStatus,
    load_published_result,
)
from .router import OfficialActivationStore


SourceProvider = Callable[[], Awaitable[dict[str, Any]]]


def _lower_posix_process_priority() -> None:
    """尽力降低 Linux builder 优先级；失败时不应阻止子进程启动。"""

    try:
        os.nice(10)
    except OSError:
        pass


def _config_get(path: str, default: Any) -> Any:
    try:
        value = config.get(path)
        return default if value is None else value
    except Exception:
        return default


def _add_payload_segment(parts: list[bytes], data: bytes) -> None:
    parts.append(len(data).to_bytes(4, "big"))
    parts.append(data)


def _build_handshake_payload(metadata: dict[str, Any]) -> bytes:
    parts: list[bytes] = []
    _add_payload_segment(
        parts,
        json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    return compress_zstd(b"".join(parts))


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(data, file, ensure_ascii=False, separators=(",", ":"))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


class PreviewManager:
    """非阻塞构建、同步、节点确认与正式切回的单进程协调器。"""

    def __init__(self) -> None:
        self._state = PreviewRuntimeState()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._source_provider: SourceProvider | None = None
        self._configuration_error = ""
        self._last_source_signature = ""
        self._output_root = Path(
            "data/sekai/deckrec/rulesets/cn_jp_preview_v1"
        )
        self._activation_store = OfficialActivationStore(
            str(self._output_root / "official_active.json")
        )
        self._load_cached_manifest_metadata()

    def _load_cached_manifest_metadata(self) -> None:
        """启动时只读取轻量指针用于 CN_DETECTED 门禁，不据此直接宣告 READY。"""

        try:
            pointer = json.loads(
                (self._output_root / "current.json").read_text(encoding="utf-8")
            )
            manifest_path = (
                self._output_root
                / pointer["version_dir"]
                / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("scope_fingerprint") == pointer.get(
                "scope_fingerprint"
            ):
                self._state.manifest = manifest
                self._state.scope_fingerprint = manifest["scope_fingerprint"]
                self._state.status = PreviewStatus.WAITING_FOR_SOURCE
        except Exception:
            return

    # ================================ 配置与只读状态 ================================ #

    def configure(self, source_provider: SourceProvider) -> None:
        self._source_provider = source_provider
        self._refresh_config_validation()

    def _refresh_config_validation(self) -> bool:
        """
        校验旁路边界，但只让 preview 自身失败关闭。

        Preview 是可选旁路；配置错误必须明显记录，却不能在插件导入阶段拖垮
        正式组卡与 NoneBot 启动。
        """

        try:
            self._validate_enabled_config()
        except Exception as exc:
            self._configuration_error = get_exc_desc(exc)
            self._state.status = PreviewStatus.ERROR
            self._state.message = "预演组卡配置无效"
            self._state.ready_servers = []
            logger.warning(
                f"CN+JP预演组卡配置无效，已单独失败关闭: "
                f"{self._configuration_error}"
            )
            return False
        self._configuration_error = ""
        return True

    def _validate_enabled_config(self) -> None:
        """启用时校验旁路池边界，防止和正式服务共用 URL。"""

        if not self.enabled():
            return
        allowed_types = set(_config_get(
            "deck.cn_jp_preview.allowed_event_types",
            ["marathon", "world_bloom"],
        ))
        if (
            not allowed_types
            or not allowed_types <= {"marathon", "world_bloom"}
        ):
            raise RuntimeError(
                "preview allowed_event_types 只能收窄 marathon/world_bloom"
            )

        preview_servers = _config_get("deck.cn_jp_preview.servers", [])
        preview_urls = [
            str(server.get("url", "")).rstrip("/")
            for server in preview_servers
            if int(server.get("weight", 0)) > 0
        ]
        if not preview_urls or any(not url for url in preview_urls):
            raise RuntimeError("启用 preview 时必须配置独立的正权重服务节点")
        if len(preview_urls) != len(set(preview_urls)):
            raise RuntimeError("preview 服务池存在重复 URL")

        official_urls = {
            str(server.get("url", "")).rstrip("/")
            for server in _config_get("deck.servers", [])
            if int(server.get("weight", 0)) > 0
        }
        overlap = sorted(set(preview_urls) & official_urls)
        if overlap:
            raise RuntimeError(
                "正式与 preview 组卡池不能共用 URL: "
                + ", ".join(overlap)
            )

    def enabled(self) -> bool:
        return bool(_config_get("deck.cn_jp_preview.enabled", False))

    def event_allowed_by_config(self, event_id: int, event_type: str) -> bool:
        allowed_types = set(_config_get(
            "deck.cn_jp_preview.allowed_event_types",
            ["marathon", "world_bloom"],
        ))
        allowlist = {
            int(value)
            for value in _config_get(
                "deck.cn_jp_preview.event_allowlist",
                [],
            )
        }
        return event_type in allowed_types and (
            not allowlist or event_id in allowlist
        )

    def get_state(self) -> PreviewRuntimeState:
        return PreviewRuntimeState(
            status=self._state.status,
            scope_fingerprint=self._state.scope_fingerprint,
            message=self._state.message,
            manifest=dict(self._state.manifest),
            ready_servers=[dict(server) for server in self._state.ready_servers],
        )

    def is_ready(self) -> bool:
        return self._state.status == PreviewStatus.READY

    def is_official_active(self, event_id: int) -> bool:
        return self._activation_store.is_active(event_id)

    def is_cn_detected(self, event_id: int) -> bool:
        return self._activation_store.is_detected(event_id)

    def mark_cn_detected(self, event_id: int, event_type: str) -> None:
        """先原子记录 CN_DETECTED，再允许调用方进入闭包与正式节点核验。"""

        if self._activation_store.mark_detected(event_id, {
            "event_type": event_type,
        }):
            logger.info(f"活动 CN-{event_id} 已进入 CN_DETECTED")

    def tracked_event_ids(self) -> set[int]:
        """返回当前 preview 活动与已检测活动的并集，供重建后继续完成切回。"""

        return {
            int(event_id)
            for event_id in self._state.manifest.get("event_ids", [])
        } | self._activation_store.detected_ids()

    def official_entry(self, event_id: int) -> dict[str, Any] | None:
        return self._activation_store.get(event_id)

    def future_card_ids(self) -> set[int]:
        if not bool(_config_get(
            "deck.cn_jp_preview.future_card_catalog.enabled",
            True,
        )):
            return set()
        return {
            int(card_id)
            for card_id in self._state.manifest.get("future_cards", {}).get(
                "ids",
                [],
            )
        }

    def ready_servers(self) -> list[dict[str, Any]]:
        return [dict(server) for server in self._state.ready_servers]

    # ================================ 非阻塞唤醒 ================================ #

    def wake(self) -> None:
        """只登记一个后台任务；调用命令不会等待构建或大文件同步。"""

        if not self.enabled():
            self._state = PreviewRuntimeState(status=PreviewStatus.DISABLED)
            return
        if not self._refresh_config_validation():
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run_once(),
            name="sekai-deck-preview-initialize",
        )

    async def wait_for_current_task(self) -> None:
        """仅供离线测试/管理命令等待；玩家请求不得调用。"""

        task = self._task
        if task is not None:
            await task

    def _set_refresh_progress(
        self,
        status: PreviewStatus,
        message: str,
    ) -> None:
        """首次初始化时公开阶段状态；已有可用版本刷新时继续服务旧版本。"""

        if self._state.status == PreviewStatus.READY:
            return
        self._state.status = status
        self._state.message = message

    async def _run_once(self) -> None:
        async with self._lock:
            previous_ready_state: PreviewRuntimeState | None = None
            try:
                if not self.enabled():
                    self._state = PreviewRuntimeState(status=PreviewStatus.DISABLED)
                    return
                if not self._refresh_config_validation():
                    return
                if self._source_provider is None:
                    raise RuntimeError("preview source provider 尚未配置")
                if not self._activation_store.healthy():
                    raise RuntimeError("正式切回激活记录损坏，preview 已失败关闭")
                if self._state.status == PreviewStatus.READY:
                    previous_ready_state = self.get_state()

                self._set_refresh_progress(
                    PreviewStatus.WAITING_FOR_SOURCE,
                    "等待 CN/JP MasterData",
                )
                source = await self._source_provider()
                source_signature = str(source.get("refresh_fingerprint") or "")

                servers = [
                    dict(server)
                    for server in _config_get(
                        "deck.cn_jp_preview.servers",
                        [],
                    )
                    if int(server.get("weight", 0)) > 0
                ]
                if not servers:
                    raise RuntimeError("未配置独立 preview 组卡服务")

                # 来源和正式切回候选均未变化时，只确认当前节点仍加载同一份规则。
                # 这一步必须位于 builder 之前，否则“规则集复用”仍会每 30 秒完整
                # 读取、解析并哈希两区 MasterData。
                if (
                    source_signature
                    and source_signature == self._last_source_signature
                    and previous_ready_state is not None
                ):
                    ready_servers = await self._reuse_current_servers(
                        servers,
                        previous_ready_state.scope_fingerprint,
                        int(source["builder_request"]["musicmetas_update_ts"]),
                    )
                    if ready_servers is not None:
                        self._state.ready_servers = ready_servers
                        logger.debug(
                            "CN+JP预演来源未变化，跳过 builder: "
                            f"source={source_signature[:12]} "
                            f"nodes={len(ready_servers)}"
                        )
                        return

                self._set_refresh_progress(
                    PreviewStatus.BUILDING,
                    "正在构建并校验预演规则",
                )
                result = await self._run_builder(source["builder_request"])

                # ================================ 正式活动粘性切回 ================================ #
                # 闭包、正式节点指纹和子进程原生计算三者全部确认后才先落盘再切路由。
                official_canary = result.get("official_canary", {})
                confirmed_official_ids = {
                    int(event_id)
                    for event_id in official_canary.get(
                        "confirmed_event_ids",
                        [],
                    )
                }
                confirmed_source_fingerprint = official_canary.get(
                    "source_fingerprint"
                )
                for activation in source.get(
                    "official_activation_candidates",
                    [],
                ):
                    event_id = int(activation["event_id"])
                    if (
                        event_id not in confirmed_official_ids
                        or activation.get("cn_fingerprint")
                        != confirmed_source_fingerprint
                    ):
                        continue
                    acked_servers = await self._revalidate_official_servers(
                        activation
                    )
                    if not acked_servers:
                        logger.warning(
                            f"活动 CN-{event_id} 原生探针通过，"
                            "但正式节点指纹确认已失效，暂不切回"
                        )
                        continue
                    activation = {
                        **activation,
                        "acked_servers": acked_servers,
                        "native_canary": "passed",
                    }
                    if self._activation_store.activate(event_id, activation):
                        logger.info(f"活动 CN-{event_id} 已粘性切回正式组卡")
                for event_id, detail in official_canary.get(
                    "failures",
                    {},
                ).items():
                    logger.warning(
                        f"活动 CN-{event_id} 正式切回探针未通过: {detail}"
                    )
                preview_error = result.get("preview_error")
                if preview_error:
                    raise RuntimeError(
                        "preview builder 失败: "
                        + str(preview_error.get("error", "内部错误"))
                    )

                # 同一内容指纹的周期刷新只做轻量健康确认，不能让可用服务每
                # 30 秒进入一次维护窗口，否则恰好经过该窗口的玩家请求会误报
                # “规则发生更新”。节点重启或指纹不一致时仍走完整同步。
                ready_servers = await self._reuse_current_servers(
                    servers,
                    result["scope_fingerprint"],
                    int(result["sync_metadata"]["musicmetas_update_ts"]),
                )
                if ready_servers is not None:
                    self._state.manifest = result["sync_metadata"]["manifest"]
                    self._state.ready_servers = ready_servers
                    self._state.message = ""
                    self._last_source_signature = source_signature
                    logger.debug(
                        "CN+JP预演规则未变化，复用已就绪节点: "
                        f"fp={result['scope_fingerprint'][:15]} "
                        f"nodes={len(ready_servers)}"
                    )
                    return

                self._state.status = PreviewStatus.SYNCING
                self._state.message = "正在同步 preview 组卡节点"
                payload = await asyncio.to_thread(
                    Path(result["sync_payload_path"]).read_bytes
                )
                ready_servers = []
                for server in servers:
                    ack = await self._sync_server(
                        server,
                        result["sync_metadata"],
                        payload,
                    )
                    ready_servers.append({**server, "ack": ack})

                self._state = PreviewRuntimeState(
                    status=PreviewStatus.READY,
                    scope_fingerprint=result["scope_fingerprint"],
                    message="",
                    manifest=result["sync_metadata"]["manifest"],
                    ready_servers=ready_servers,
                )
                self._last_source_signature = source_signature
                logger.info(
                    "CN+JP预演组卡已就绪: "
                    f"fp={result['scope_fingerprint'][:15]} "
                    f"events={len(result['event_ids'])} nodes={len(ready_servers)}"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if previous_ready_state is not None:
                    # 刷新失败不能主动清空 last-known-good；CN_DETECTED 门禁仍会
                    # 单独阻止已发现正式数据的活动继续使用旧 JP 规则。
                    self._state = previous_ready_state
                    logger.warning(
                        "预演组卡刷新失败，继续使用上一可用版本: "
                        f"{get_exc_desc(exc)}"
                    )
                else:
                    self._state.status = PreviewStatus.ERROR
                    self._state.message = "预演组卡数据初始化失败"
                    self._state.ready_servers = []
                    logger.warning(f"预演组卡初始化失败: {get_exc_desc(exc)}")

    async def _revalidate_official_servers(
        self,
        activation: dict[str, Any],
    ) -> list[str]:
        """正式探针完成后再次确认节点，关闭探针运行期间的数据变更窗口。"""

        expected = activation.get("cn_fingerprint")

        async def check(raw_url: str) -> str | None:
            url = str(raw_url).rstrip("/")
            try:
                async with get_client_session().get(
                    url + "/data_status",
                    params={"region": PREVIEW_NATIVE_REGION},
                ) as response:
                    if response.status != 200:
                        return None
                    status = await response.json()
                loaded = status.get("loaded_fingerprints")
                if (
                    status.get("instance_role") != "official"
                    or status.get("maintenance")
                    or status.get("data_healthy") is not True
                    or status.get("stored_fingerprint") != expected
                    or not isinstance(loaded, dict)
                    or not loaded
                    or set(loaded.values()) != {expected}
                ):
                    return None
                return url
            except Exception:
                return None

        results = await asyncio.gather(*[
            check(url)
            for url in activation.get("acked_servers", [])
        ])
        return sorted({url for url in results if url})

    # ================================ Builder子进程 ================================ #

    async def _run_builder(self, request: dict[str, Any]) -> dict[str, Any]:
        if not bool(_config_get("deck.cn_jp_preview.builder_owner", True)):
            # 多 Bot 部署必须给非 owner 节点挂载同一发布目录；这里严格校验
            # 来源、策略、manifest 与 payload，绝不自行构建或消费旧版本。
            return await asyncio.to_thread(
                load_published_result,
                request["output_root"],
                request,
            )

        output_root = Path(request["output_root"]).resolve()
        jobs_dir = output_root / "jobs"
        job_id = uuid.uuid4().hex
        request_path = jobs_dir / f"{job_id}.request.json"
        result_path = jobs_dir / f"{job_id}.result.json"
        await asyncio.to_thread(_atomic_write_json, request_path, request)

        worker_path = Path(__file__).with_name("builder_worker.py").resolve()
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW
                | subprocess.BELOW_NORMAL_PRIORITY_CLASS
            )
        else:
            kwargs["preexec_fn"] = _lower_posix_process_priority
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(worker_path),
            str(request_path),
            str(result_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        try:
            stdout, stderr = await process.communicate()
            if not result_path.is_file():
                detail = stderr.decode("utf-8", errors="replace")[-2000:]
                raise RuntimeError(
                    f"preview builder 未生成结果 rc={process.returncode}: {detail}"
                )
            response = await asyncio.to_thread(
                lambda: json.loads(result_path.read_text(encoding="utf-8"))
            )
            if process.returncode != 0 or not response.get("ok"):
                raise RuntimeError(
                    response.get("error")
                    or stderr.decode("utf-8", errors="replace")[-2000:]
                )
            if stdout:
                logger.debug(
                    "preview builder: "
                    + stdout.decode("utf-8", errors="replace")[-1000:]
                )
            return response["result"]
        finally:
            for path in (request_path, result_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    # ================================ Preview节点同步 ================================ #

    async def _reuse_current_servers(
        self,
        servers: list[dict[str, Any]],
        expected_fingerprint: str,
        expected_musicmetas_update_ts: int,
    ) -> list[dict[str, Any]] | None:
        """确认全部节点仍加载当前指纹；成功时跳过无意义的重复发布。"""

        if (
            self._state.status != PreviewStatus.READY
            or self._state.scope_fingerprint != expected_fingerprint
        ):
            return None

        acknowledgements = await asyncio.gather(*[
            self._get_server_status_ack(
                server,
                expected_fingerprint,
                expected_musicmetas_update_ts,
            )
            for server in servers
        ])
        if any(ack is None for ack in acknowledgements):
            return None
        return [
            {**server, "ack": ack}
            for server, ack in zip(servers, acknowledgements)
        ]

    async def _get_server_status_ack(
        self,
        server: dict[str, Any],
        expected_fingerprint: str,
        expected_musicmetas_update_ts: int,
    ) -> dict[str, Any] | None:
        """轻量核验 preview 进程、磁盘激活指针与全部 worker 的加载指纹。"""

        base_url = str(server["url"]).rstrip("/")
        try:
            session = get_client_session()
            async with session.get(base_url + "/health") as response:
                if response.status != 200:
                    return None
                health = await response.json()
            async with session.get(
                base_url + "/data_status",
                params={"region": PREVIEW_NATIVE_REGION},
            ) as response:
                if response.status != 200:
                    return None
                status = await response.json()
        except Exception as exc:
            logger.debug(
                f"preview节点轻量核验失败 {base_url}: {get_exc_desc(exc)}"
            )
            return None

        worker_num = status.get("worker_num")
        loaded = status.get("loaded_fingerprints")
        if (
            health.get("status") != "ok"
            or health.get("instance_role") != "preview"
            or health.get("data_scope") != PREVIEW_DATA_SCOPE
            or health.get("maintenance") is not False
            or health.get("data_healthy") is not True
            or health.get("workers_alive") is not True
            or status.get("instance_role") != "preview"
            or status.get("data_scope") != PREVIEW_DATA_SCOPE
            or status.get("native_region") != PREVIEW_NATIVE_REGION
            or status.get("region") != PREVIEW_NATIVE_REGION
            or status.get("maintenance") is not False
            or status.get("data_healthy") is not True
            or status.get("stored_fingerprint") != expected_fingerprint
            or status.get("active_fingerprint") != expected_fingerprint
            or status.get("musicmetas_update_ts")
            != expected_musicmetas_update_ts
            or not isinstance(worker_num, int)
            or worker_num <= 0
            or not isinstance(loaded, dict)
            or len(loaded) != worker_num
            or set(loaded.values()) != {expected_fingerprint}
        ):
            return None

        return {
            "protocol_version": 2,
            "instance_role": "preview",
            "data_scope": PREVIEW_DATA_SCOPE,
            "region": PREVIEW_NATIVE_REGION,
            "stored_fingerprint": expected_fingerprint,
            "loaded_fingerprint": expected_fingerprint,
            "worker_fingerprints": loaded,
            "worker_num": worker_num,
            "data_healthy": True,
        }

    async def _sync_server(
        self,
        server: dict[str, Any],
        metadata: dict[str, Any],
        full_payload: bytes,
    ) -> dict[str, Any]:
        base_url = str(server["url"]).rstrip("/")
        expected = metadata["masterdata_fingerprint"]
        handshake = _build_handshake_payload(metadata)

        async def post(payload: bytes):
            return await get_client_session().post(
                base_url + "/update_data",
                data=payload,
            )

        async with await post(handshake) as response:
            if response.status == 426:
                await response.read()
            elif response.status == 200:
                ack = await response.json()
                return self._validate_ack(base_url, expected, ack)
            else:
                detail = (await response.text())[:1000]
                raise RuntimeError(
                    f"preview节点握手失败 {base_url} ({response.status}): {detail}"
                )

        async with await post(full_payload) as response:
            if response.status != 200:
                detail = (await response.text())[:1000]
                raise RuntimeError(
                    f"preview节点同步失败 {base_url} ({response.status}): {detail}"
                )
            ack = await response.json()
            return self._validate_ack(base_url, expected, ack)

    @staticmethod
    def _validate_ack(
        server_url: str,
        expected_fingerprint: str,
        ack: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            ack.get("protocol_version") != 2
            or ack.get("instance_role") != "preview"
            or ack.get("data_scope") != PREVIEW_DATA_SCOPE
            or ack.get("region") != PREVIEW_NATIVE_REGION
            or ack.get("stored_fingerprint") != expected_fingerprint
            or ack.get("loaded_fingerprint") != expected_fingerprint
            or ack.get("data_healthy") is not True
        ):
            raise RuntimeError(f"preview节点未确认目标指纹: {server_url}")
        worker_fingerprints = ack.get("worker_fingerprints")
        if (
            not isinstance(worker_fingerprints, dict)
            or not worker_fingerprints
            or ack.get("worker_num") != len(worker_fingerprints)
            or set(worker_fingerprints.values()) != {expected_fingerprint}
        ):
            raise RuntimeError(f"preview节点 worker 指纹不完整: {server_url}")
        return ack


preview_manager = PreviewManager()
