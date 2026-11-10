"""A visible label around the unchanged workbench, using isolated demo config."""
from pathlib import Path
import runpy
import sys
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.demo_local import validate_child_environment
validate_child_environment()
import streamlit as st

st.set_page_config(page_title='售后工作台演示',layout='wide')
st.info('演示模式：合成数据 / FakeLLM固定脚本 / 真实PostgreSQL与只读MCP。固定时间2026年10月9日；仅用ORD-1009体验退货流程，无退款或消息发送。')
original=st.set_page_config
try:
    st.set_page_config=lambda **kwargs:None
    runpy.run_path(str(ROOT/'src/ui/streamlit_app.py'),run_name='__main__')
finally:st.set_page_config=original
