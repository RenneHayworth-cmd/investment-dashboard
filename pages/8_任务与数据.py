import streamlit as st

from core.cache import list_datasets
from core.db import init_db, list_jobs


st.set_page_config(page_title="任务与数据", layout="wide")
init_db()

st.title("任务与数据")

st.subheader("数据集")
st.dataframe(list_datasets(), use_container_width=True, hide_index=True)

st.subheader("任务记录")
st.dataframe(list_jobs(), use_container_width=True, hide_index=True)
