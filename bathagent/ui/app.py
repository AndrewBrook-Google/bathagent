"""BathStuff Customer Operations & FinOps Console.
Enterprise interface powered by Google ADK, MCP Toolbox for Databases, and Wildfire Proxy.
"""

import json
import os
import streamlit as st

from bathagent.agent import BathAgent
from bathagent.config import settings

# Page Setup
st.set_page_config(
    page_title="BathStuff Operations Console",
    page_icon="🛁",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom Enterprise CSS
st.markdown(
    """
    <style>
    /* Global Styles */
    .stApp {
        background-color: #FAFAFA;
    }
    .brand-header {
        font-size: 1.8rem;
        font-weight: 700;
        color: #1A73E8;
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 0.2rem;
    }
    .brand-subtitle {
        font-size: 0.95rem;
        color: #5F6368;
        margin-bottom: 1.2rem;
    }
    .status-card {
        background-color: #FFFFFF;
        border: 1px solid #E0E0E0;
        border-radius: 8px;
        padding: 12px 16px;
        margin-bottom: 12px;
    }
    .badge-active {
        background-color: #E6F4EA;
        color: #137333;
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 0.8rem;
        font-weight: 600;
    }
    .badge-wf {
        background-color: #FCE8E6;
        color: #C5221F;
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 0.8rem;
        font-weight: 600;
    }
    .badge-tb {
        background-color: #E8F0FE;
        color: #1A73E8;
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 0.8rem;
        font-weight: 600;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_agent():
    return BathAgent()


agent = get_agent()

# Sidebar: System Health & Context
with st.sidebar:
    st.markdown("### 🛁 **BathStuff Operations**")
    st.caption("Internal Portal for Support & FinOps")

    st.divider()

    st.markdown("#### 📡 System Status")
    wf_reachable = agent.wildfire.is_server_reachable()
    wf_status_html = '<span class="badge-active">Online</span>' if wf_reachable else '<span class="badge-wf">Offline / Standalone</span>'
    
    st.markdown(
        f"""
        <div class="status-card">
            <div><b>Database</b>: <code>AlloyDB for PostgreSQL</code></div>
            <div><b>MCP Toolbox</b>: <span class="badge-active">Active</span></div>
            <div><b>Wildfire Proxy (:8787)</b>: {wf_status_html}</div>
            <div><b>Agent Brain</b>: <code>{settings.GEMINI_MODEL}</code></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.divider()
    st.markdown("#### ⚙️ Session Settings")
    if st.button("🧹 Clear Chat Session"):
        st.session_state.messages = []
        st.rerun()


# Main Application Interface
st.markdown('<div class="brand-header">🛁 BathAgent Operations Assistant</div>', unsafe_allow_html=True)
st.markdown('<div class="brand-subtitle">Connected to BathStuff AlloyDB via MCP Toolbox (Read/Canned) & Wildfire Proxy (Mutations).</div>', unsafe_allow_html=True)

# Initialize Session Messages
if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": (
                "Welcome to the **BathStuff Operations Console**. I am **BathAgent**.\n\n"
                "You can ask me to look up customer accounts, verify order statuses, perform read-only analytics, "
                "or formulate and submit compliant database changesets via the Wildfire Proxy."
            ),
            "tool_calls": [],
        }
    ]

# Render Chat History
for msg in st.session_state.messages:
    if msg["role"] == "user":
        with st.chat_message("user", avatar="👤"):
            st.write(msg["content"])
    else:
        with st.chat_message("assistant", avatar="🛁"):
            st.markdown(msg["content"])

            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                with st.expander(f"🛠️ System Activity ({len(tool_calls)} operations)", expanded=False):
                    for tc in tool_calls:
                        tool_name = tc.get("tool") or tc.get("name", "Tool")
                        badge = "badge-wf" if "wildfire" in str(tool_name).lower() else "badge-tb"
                        st.markdown(f'<span class="{badge}">{tool_name}</span>', unsafe_allow_html=True)
                        st.json(tc)

# Chat Input
user_input = st.chat_input("Enter customer inquiry, order action, or operational request...")
if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.spinner("Processing request and querying database tools..."):
        result = agent.run_prompt(user_input)
        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": result.get("text", ""),
                "tool_calls": result.get("tool_calls", []),
            }
        )
    st.rerun()
