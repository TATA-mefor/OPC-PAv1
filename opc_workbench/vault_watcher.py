"""vault 周期扫描（M4 第二批）。

裁决边界（用户已批准）：
- 生产启动点在 app.main()，**不在 build_application() 隐式启动**；
  本批不改 app.py，接线留到①分支验收合并后一次完成。
- 关闭必须 stop() + join()，不留线程。
- 单文件失败不得中断整轮扫描，也不得让线程静默死亡。

变更判定：mtime/size 只是候选（diff_snapshots 纯函数），真实是否重嵌入由
KnowledgeService 的正文 hash 复核决定——只改 visibility 不会重嵌入（C6）。
文件消失 → mark_missing（保留索引供恢复），**不是**彻底删除。
"""
from __future__ import annotations

import threading

from . import audit_log, config, vault


class VaultWatcher:
    __slots__ = ("_service", "_interval", "_stop", "_thread", "_snapshot")

    def __init__(self, service, *, interval_s: float | None = None):
        self._service = service
        self._interval = (config.VAULT_SCAN_INTERVAL_S if interval_s is None
                          else interval_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict = {}

    def __getattribute__(self, name):
        # M5-a：watcher 是组合根持有的后台部件，不得成为 KnowledgeService
        # （进而写能力）的外泄出口；内部访问一律越过本钩子。
        if name == "_service":
            raise PermissionError("watcher_service_not_exposed")
        return object.__getattribute__(self, name)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def scan_once(self) -> dict:
        """扫描一轮并处理差异。返回本轮差异与逐文件结果（不含正文）。"""
        new = object.__getattribute__(self, "_service").snapshot()
        diff = vault.diff_snapshots(self._snapshot, new)
        self._snapshot = new
        results: list[dict] = []
        for rel in diff["added"] + diff["changed"]:
            try:
                outcome = object.__getattribute__(self, "_service").ingest(rel)
                results.append({"vault_path": rel, "status": outcome["status"]})
            except Exception as exc:  # noqa: BLE001 — 单文件失败不拖垮整轮
                audit_log.log("ERROR", "watcher_ingest_failed",
                              target_table="knowledge_docs",
                              result_code=type(exc).__name__, source=rel)
                results.append({"vault_path": rel, "status": "failed"})
        for rel in diff["removed"]:
            try:
                object.__getattribute__(self, "_service").mark_missing(rel)
                results.append({"vault_path": rel, "status": "missing"})
            except Exception as exc:  # noqa: BLE001
                audit_log.log("ERROR", "watcher_missing_failed",
                              target_table="knowledge_docs",
                              result_code=type(exc).__name__, source=rel)
        return {"diff": diff, "results": results}

    def start(self) -> None:
        """幂等启动：已在跑则直接返回，绝不产生第二个线程。"""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vault-watcher",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001 — 线程不得静默死亡
                audit_log.log("ERROR", "watcher_scan_failed",
                              result_code=type(exc).__name__)
            self._stop.wait(self._interval)

    def stop(self, *, timeout: float | None = None) -> None:
        """幂等关停：置停止位并 join，返回后无线程存活。"""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout if timeout is not None else self._interval + 5)
            self._thread = None