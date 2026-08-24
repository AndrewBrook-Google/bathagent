"""BathStuff Operations Console - Gemini Enterprise Edition.
Visual interface styled to match Google Gemini Enterprise UI.
Powered by Google ADK, MCP Toolbox for Databases, and Wildfire Proxy.
"""

import json
import os
import streamlit as st

from bathagent.agent import BathAgent
from bathagent.config import settings

# Page Setup
st.set_page_config(
    page_title="Gemini Enterprise | BathStuff Operations",
    page_icon="✨",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Gemini Enterprise Custom CSS & Styling
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;600;700&family=Roboto:wght@300;400;500&display=swap');

    /* Global Typography & Background */
    html, body, [class*="css"], .stApp {
        font-family: 'Google Sans', 'Roboto', -apple-system, BlinkMacSystemFont, sans-serif;
        background-color: #F8FAFD;
        color: #1F1F1F;
    }

    /* Top Navigation Bar */
    .top-nav {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 12px 20px;
        background: #FFFFFF;
        border-bottom: 1px solid #E0E3E7;
        border-radius: 12px;
        margin-bottom: 20px;
    }
    .gemini-brand {
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 1.35rem;
        font-weight: 600;
        letter-spacing: -0.3px;
    }
    .gemini-gradient-text {
        background: linear-gradient(90deg, #4285F4 0%, #9B72CB 50%, #D96570 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-weight: 700;
    }
    .tenant-badge {
        font-size: 0.8rem;
        background-color: #EDF2FA;
        color: #444746;
        padding: 4px 10px;
        border-radius: 16px;
        font-weight: 500;
        border: 1px solid #D3E3FD;
    }
    .model-selector {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 0.85rem;
        color: #444746;
        background: #FFFFFF;
        border: 1px solid #C4C7C5;
        padding: 4px 12px;
        border-radius: 20px;
    }

    /* Hero Greeting */
    .hero-container {
        max-width: 780px;
        margin: 40px auto 30px auto;
        text-align: left;
    }
    .hero-greeting {
        font-size: 3rem;
        font-weight: 600;
        letter-spacing: -1px;
        line-height: 1.15;
        margin-bottom: 8px;
    }
    .hero-sub {
        font-size: 1.5rem;
        color: #747775;
        font-weight: 400;
    }

    /* Message Bubbles */
    .user-bubble {
        background-color: #E9EEF6;
        color: #1F1F1F;
        padding: 14px 18px;
        border-radius: 20px 20px 4px 20px;
        max-width: 80%;
        margin-left: auto;
        font-size: 0.95rem;
        line-height: 1.5;
        box-shadow: 0 1px 2px rgba(0,0,0,0.05);
    }
    .gemini-bubble {
        background-color: #FFFFFF;
        border: 1px solid #E0E3E7;
        padding: 18px 22px;
        border-radius: 20px 20px 20px 4px;
        max-width: 95%;
        margin-right: auto;
        font-size: 0.95rem;
        line-height: 1.6;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }

    /* Activity & Tool Calling Inspector */
    .tool-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 0.8rem;
        background: #F0F4F9;
        color: #444746;
        padding: 4px 10px;
        border-radius: 12px;
        margin-right: 6px;
        margin-bottom: 6px;
        border: 1px solid #E0E3E7;
    }
    .tool-chip-wf {
        background: #FCE8E6;
        color: #C5221F;
        border-color: #F6AEA9;
    }
    .tool-chip-tb {
        background: #E8F0FE;
        color: #1A73E8;
        border-color: #D2E3FC;
    }

    /* Wildfire Action Card */
    .wf-card {
        background: #FFFFFF;
        border: 1px solid #F6AEA9;
        border-left: 4px solid #C5221F;
        border-radius: 12px;
        padding: 16px 20px;
        margin-top: 14px;
        box-shadow: 0 2px 6px rgba(197,34,31,0.08);
    }
    .wf-card-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-weight: 600;
        color: #C5221F;
        margin-bottom: 8px;
    }
    .wf-btn {
        display: inline-block;
        background: #1A73E8;
        color: #FFFFFF !important;
        padding: 8px 16px;
        border-radius: 20px;
        text-decoration: none;
        font-weight: 500;
        font-size: 0.85rem;
        margin-top: 10px;
    }
    .wf-btn:hover {
        background: #1557B0;
    }

    /* Sidebar Styling */
    .sidebar-brand {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 1.1rem;
        font-weight: 600;
        color: #1F1F1F;
        margin-bottom: 12px;
    }
    .sidebar-section-title {
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.8px;
        color: #747775;
        font-weight: 600;
        margin-top: 16px;
        margin-bottom: 8px;
    }
    .system-badge-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 6px 0;
        font-size: 0.85rem;
        border-bottom: 1px solid #F0F4F9;
    }
    .status-dot {
        height: 8px;
        width: 8px;
        background-color: #34A853;
        border-radius: 50%;
        display: inline-block;
    }
    .status-dot-amber {
        background-color: #FBBC04;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Gemini Sparkle SVG Icon
GEMINI_SPARKLE_SVG = """
<svg width="24" height="24" viewBox="0 0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg">
  <path d="M14 0C14 7.73199 7.73199 14 0 14C7.73199 14 14 20.268 14 28C14 20.268 20.268 14 28 14C20.268 14 14 7.73199 14 0Z" fill="url(#paint0_linear)"/>
  <defs>
    <linearGradient id="paint0_linear" x1="0" y1="0" x2="28" y2="28" gradientUnits="userSpaceOnUse">
      <stop stop-color="#4285F4"/>
      <stop offset="0.5" stop-color="#9B72CB"/>
      <stop offset="1" stop-color="#D96570"/>
    </linearGradient>
  </defs>
</svg>
"""


@st.cache_resource
def get_agent():
    return BathAgent()


agent = get_agent()

# Sidebar: Enterprise Context & System Connectivity
with st.sidebar:
    st.markdown(
        f"""
        <div class="sidebar-brand">
            {GEMINI_SPARKLE_SVG}
            <span>Gemini Enterprise</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    
    if st.button("➕  New Chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

    st.markdown('<div class="sidebar-section-title">Connected Services</div>', unsafe_allow_html=True)
    
    wf_reachable = agent.wildfire.is_server_reachable()
    wf_dot = "status-dot" if wf_reachable else "status-dot status-dot-amber"
    wf_label = "Online" if wf_reachable else "Standalone"

    st.markdown(
        f"""
        <div class="system-badge-row">
            <span>AlloyDB for PostgreSQL</span>
            <span style="color:#137333; font-size:0.8rem; font-weight:500;">Connected</span>
        </div>
        <div class="system-badge-row">
            <span>MCP Toolbox (Read / Canned)</span>
            <span style="color:#137333; font-size:0.8rem; font-weight:500;">Active</span>
        </div>
        <div class="system-badge-row">
            <span>Wildfire Proxy (:8787)</span>
            <span style="font-size:0.8rem; font-weight:500;"><span class="{wf_dot}"></span> {wf_label}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="sidebar-section-title">Enterprise Tenant</div>', unsafe_allow_html=True)
    st.markdown(
        """
        <div style="font-size:0.85rem; color:#444746; line-height:1.5;">
            <b>Organization</b>: BathStuff Retail Inc.<br>
            <b>Region</b>: <code>us-central1</code><br>
            <b>Policy Profile</b>: <code>FinOps-Strict-v2</code>
        </div>
        """,
        unsafe_allow_html=True,
    )


# Top Navigation Bar
st.markdown(
    f"""
    <div class="top-nav">
        <div class="gemini-brand">
            {GEMINI_SPARKLE_SVG}
            <span class="gemini-gradient-text">Gemini</span>
            <span style="font-size:1.05rem; font-weight:500; color:#444746;">| BathStuff Operations</span>
            <span class="tenant-badge">Enterprise Edition</span>
        </div>
        <div class="model-selector">
            <span>✨ {settings.GEMINI_MODEL}</span>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Initialize Session Messages
if "messages" not in st.session_state:
    st.session_state.messages = []

# Hero State when empty
if not st.session_state.messages:
    st.markdown(
        """
        <div class="hero-container">
            <div class="hero-greeting">
                <span class="gemini-gradient-text">Hello, Andy</span>
            </div>
            <div class="hero-sub">How can I assist with BathStuff operations today?</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# Render Chat History
for msg in st.session_state.messages:
    if msg["role"] == "user":
        with st.chat_message("user", avatar="👤"):
            st.markdown(msg["content"])
    else:
        with st.chat_message("assistant", avatar="✨"):
            st.markdown(msg["content"])

            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                with st.expander(f"✨ Gemini Tools & System Activity ({len(tool_calls)} operations)", expanded=False):
                    for tc in tool_calls:
                        tool_name = tc.get("tool") or tc.get("name", "Tool")
                        chip_class = "tool-chip-wf" if "wildfire" in str(tool_name).lower() else "tool-chip-tb"
                        st.markdown(f'<span class="tool-chip {chip_class}">⚙️ {tool_name}</span>', unsafe_allow_html=True)
                        st.json(tc)

# Chat Input Box
user_input = st.chat_input("Ask Gemini about BathStuff customer accounts, orders, or database adjustments...")
if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.spinner("Gemini is analyzing database context and executing tools..."):
        result = agent.run_prompt(user_input)
        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": result.get("text", ""),
                "tool_calls": result.get("tool_calls", []),
            }
        )
    st.rerun()
