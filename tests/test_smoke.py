"""M1 smoke test（PRD §12.2/§13.1）：开发第一天验证框架组合可用。

覆盖四件套：Gradio 6 Blocks 构建 + SQLite WAL 多线程写 + mock LLM 调用 + pytest 隔离。
全部零网络、零开发库触碰。
"""
from __future__ import annotations

import threading

import gradio as gr

from opc_workbench import config, db, llm_client


def test_gradio_blocks_builds():
    """Gradio 6.26 Blocks + 组件 + Timer 可构建（不 launch，不占端口）。"""
    with gr.Blocks() as demo:
        gr.Markdown("# PERSONAL AGENT smoke")
        box = gr.Textbox(label="输入")
        out = gr.HTML()
        gr.Timer(5)
        box.submit(lambda t: f"<b>{t}</b>", inputs=box, outputs=out)
    assert demo is not None
    cfg = demo.get_config_file()
    assert cfg["version"]  # 配置可生成即组件树完整


def test_sqlite_wal_multithread_writes(db_conn):
    """WAL 模式下多线程并发写不丢行、不锁死。"""
    assert db_conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    errors: list[Exception] = []

    def worker(n: int):
        try:
            conn = db.get_conn()  # 每线程独立连接
            for i in range(20):
                with conn:
                    conn.execute(
                        "INSERT INTO tasks(title,owner_role,created_at,updated_at) "
                        "VALUES(?,?,?,?)",
                        (f"t{n}-{i}", "sales", db.now_iso(), db.now_iso()),
                    )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close_thread_conns()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert not any(t.is_alive() for t in threads)
    total = db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
    assert total == 80


def test_mock_llm_roundtrip(isolated_env):
    """mock LLM 注入生效：调用走替身、零网络、可编排异常。"""
    fake = isolated_env["fake_llm"]
    fake.responses = ["你好，老板"]
    llm = llm_client.get_llm()
    assert llm is fake
    assert llm.chat([{"role": "user", "content": "hi"}]) == "你好，老板"
    assert len(fake.calls) == 1

    fake.responses = [llm_client.LLMFailure("boom")]
    try:
        llm.chat([{"role": "user", "content": "hi"}])
        raise AssertionError("应抛 LLMFailure")
    except llm_client.LLMFailure:
        pass


def test_no_key_real_client_degrades():
    """无 Key 的真实客户端：available=False，chat 抛 LLMFailure 而非崩溃/联网。"""
    client = llm_client.LLMClient(api_key="")
    assert not client.available
    try:
        client.chat([{"role": "user", "content": "hi"}])
        raise AssertionError("应抛 LLMFailure")
    except llm_client.LLMFailure as exc:
        assert "no_api_key" in str(exc)


def test_isolation_points_to_tmp(isolated_env, tmp_path):
    """隔离验证：config 路径全部落在 tmp_path，绝不指向开发库。"""
    assert str(config.DB_PATH).startswith(str(tmp_path))
    assert str(config.VAULT_PATH).startswith(str(tmp_path))
    assert "OPC-PAv1/data" not in str(config.DB_PATH).replace("\\", "/")


def test_roles_single_source():
    """角色枚举 10 值、默认私密集正确（PRD §1.3）。"""
    assert len(config.ROLES) == 10
    assert config.DEFAULT_PRIVATE_ROLES == {"cfo", "legal", "hr", "personal"}
    assert config.deterministic_route("下周三提醒我体检") == "personal"
    assert config.deterministic_route("这个月成本多少") == "cfo"
    assert config.deterministic_route("随便聊聊") == "coordinator"
