import pandas as pd
import streamlit as st

from core.cache import list_datasets
from core.db import get_conn, init_db


st.set_page_config(page_title="任务与数据", layout="wide")
init_db()

st.title("任务与数据")

st.subheader("数据集")
st.dataframe(list_datasets(), use_container_width=True, hide_index=True)

st.subheader("任务记录")
conn = get_conn()
jobs = pd.read_sql_query(
    """
    SELECT id, job_name, status, started_at, finished_at, message
    FROM jobs
    ORDER BY id DESC
    """,
    conn,
)
conn.close()
st.dataframe(jobs, use_container_width=True, hide_index=True)
