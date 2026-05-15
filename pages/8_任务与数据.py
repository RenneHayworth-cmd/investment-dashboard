import os
import signal
import subprocess
import sys
import time
import streamlit as st

from core.cache import list_datasets
from core.db import init_db, list_jobs
from core.paths import BACKGROUND_PID_PATH, ROOT_DIR
from services.background_updater import describe_update_windows, read_running_pid
from services.update_tasks import run_index_ma20_update


st.set_page_config(page_title="任务与数据", layout="wide")
init_db()

st.title("任务与数据")

running_pid = read_running_pid()

with st.sidebar:
    st.subheader("后台更新")
    api_key = st.text_input(
        "API Key",
        value=os.getenv("TICKFLOW_API_KEY", ""),
        type="password",
        placeholder="可选；仅本次启动后台时传入",
    )
    days = st.number_input("报表天数", min_value=10, max_value=365, value=30, step=5)
    interval_minutes = st.number_input("后台间隔（分钟）", min_value=1, max_value=1440, value=60)
    max_workers = st.number_input("并发数", min_value=1, max_value=8, value=4, step=1)
    st.caption(describe_update_windows())
    force_refresh = st.checkbox("强制重新获取", value=False)
    run_once_clicked = st.button("立即运行一次", type="primary")
    loop_button_label = "停止后台循环" if running_pid else "启动后台循环"
    loop_button_type = "secondary" if running_pid else "primary"
    loop_button_clicked = st.button(loop_button_label, type=loop_button_type)

notice = st.session_state.pop("background_loop_notice", None)
if notice:
    level, message = notice
    getattr(st, level)(message)

if running_pid:
    st.info(f"后台更新调度器运行中，PID={running_pid}")
else:
    st.caption("后台更新调度器未运行。")

if run_once_clicked:
    with st.spinner("正在运行指数 MA20 更新任务..."):
        result = run_index_ma20_update(
            api_key=api_key,
            days=int(days),
            cache_source="auto",
            use_fresh_cache=not force_refresh,
            max_workers=int(max_workers),
        )
    if result.status == "success":
        st.success(result.message)
        st.rerun()
    else:
        st.error(result.message)

if loop_button_clicked and not running_pid:
    running_pid = read_running_pid()
    if running_pid:
        st.session_state["background_loop_notice"] = (
            "warning",
            f"已有后台更新调度器正在运行，PID={running_pid}。",
        )
        st.rerun()
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
            "--max-workers",
            str(int(max_workers)),
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
        BACKGROUND_PID_PATH.write_text(str(process.pid), encoding="utf-8")
        st.session_state["background_loop_notice"] = (
            "success",
            f"已启动后台循环，PID={process.pid}。按钮已切换为停止。",
        )
        st.rerun()

if loop_button_clicked and running_pid:
    running_pid = read_running_pid()
    if not running_pid:
        BACKGROUND_PID_PATH.unlink(missing_ok=True)
        st.session_state["background_loop_notice"] = (
            "warning",
            "没有发现正在运行的后台更新调度器。",
        )
        st.rerun()
    else:
        try:
            os.kill(running_pid, signal.SIGTERM)
            stopped = False
            for _ in range(20):
                time.sleep(0.1)
                if read_running_pid() is None:
                    stopped = True
                    break
            if stopped:
                st.session_state["background_loop_notice"] = (
                    "success",
                    f"已停止后台循环，PID={running_pid}。按钮已切换为启动。",
                )
            else:
                st.session_state["background_loop_notice"] = (
                    "warning",
                    f"已发送停止信号，PID={running_pid}，调度器正在退出。",
                )
            st.rerun()
        except ProcessLookupError:
            BACKGROUND_PID_PATH.unlink(missing_ok=True)
            st.session_state["background_loop_notice"] = (
                "warning",
                "后台更新调度器已经退出，已清理 PID 文件。",
            )
            st.rerun()
        except PermissionError as exc:
            st.error(f"没有权限停止 PID={running_pid}：{exc}")

st.subheader("数据集")
st.dataframe(list_datasets(), use_container_width=True, hide_index=True)

st.subheader("任务记录")
st.dataframe(list_jobs(), use_container_width=True, hide_index=True)
