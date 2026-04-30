import os
import signal
import subprocess
import sys
import streamlit as st

from core.cache import list_datasets
from core.db import init_db, list_jobs
from core.paths import BACKGROUND_PID_PATH, ROOT_DIR
from services.background_updater import read_running_pid
from services.update_tasks import run_index_ma20_update


st.set_page_config(page_title="任务与数据", layout="wide")
init_db()

st.title("任务与数据")

with st.sidebar:
    st.subheader("后台更新")
    api_key = st.text_input(
        "TickFlow API Key",
        value=os.getenv("TICKFLOW_API_KEY", ""),
        type="password",
    )
    days = st.number_input("报表天数", min_value=10, max_value=365, value=30, step=5)
    interval_minutes = st.number_input("后台间隔（分钟）", min_value=1, max_value=1440, value=60)
    force_refresh = st.checkbox("强制重新获取", value=False)
    run_once_clicked = st.button("立即运行一次", type="primary")
    start_loop_clicked = st.button("启动后台循环")
    stop_loop_clicked = st.button("停止后台循环")

running_pid = read_running_pid()
if running_pid:
    st.info(f"后台更新调度器运行中，PID={running_pid}")

if run_once_clicked:
    with st.spinner("正在运行指数 MA20 更新任务..."):
        result = run_index_ma20_update(
            api_key=api_key,
            days=int(days),
            cache_source="auto",
            use_fresh_cache=not force_refresh,
        )
    if result.status == "success":
        st.success(result.message)
        st.rerun()
    else:
        st.error(result.message)

if start_loop_clicked:
    running_pid = read_running_pid()
    if running_pid:
        st.warning(f"已有后台更新调度器正在运行，PID={running_pid}。")
    else:
        env = os.environ.copy()
        if api_key:
            env["TICKFLOW_API_KEY"] = api_key
        command = [
            sys.executable,
            "-m",
            "services.background_updater",
            "--interval-minutes",
            str(int(interval_minutes)),
            "--days",
            str(int(days)),
        ]
        if force_refresh:
            command.append("--force-refresh")
        process = subprocess.Popen(
            command,
            cwd=str(ROOT_DIR),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        st.success(f"已启动后台循环，PID={process.pid}。刷新本页可查看任务记录。")

if stop_loop_clicked:
    running_pid = read_running_pid()
    if not running_pid:
        BACKGROUND_PID_PATH.unlink(missing_ok=True)
        st.warning("没有发现正在运行的后台更新调度器。")
    else:
        try:
            os.kill(running_pid, signal.SIGTERM)
            st.success(f"已发送停止信号，PID={running_pid}。")
        except ProcessLookupError:
            BACKGROUND_PID_PATH.unlink(missing_ok=True)
            st.warning("后台更新调度器已经退出，已清理 PID 文件。")
        except PermissionError as exc:
            st.error(f"没有权限停止 PID={running_pid}：{exc}")

st.subheader("数据集")
st.dataframe(list_datasets(), use_container_width=True, hide_index=True)

st.subheader("任务记录")
st.dataframe(list_jobs(), use_container_width=True, hide_index=True)
